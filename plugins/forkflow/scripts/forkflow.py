#!/usr/bin/env python3
r"""forkflow - work in a fork of a moving project without touching the mirror or the trunk.

Two remotes, two long-lived branches:

    upstream/main ----o-------o-------o          theirs; read-only (push URL disabled)
                       \
    main (mirror) ------o-------o-------o        pristine copy of upstream/main,
                                         \       fast-forward only, pushed to origin
    develop (trunk) ----------------------o--o--o    upstream + our work; protected, MR-only

Hard rules, enforced by this script and by the pre-push hook it installs:

  1. never push to `upstream`
  2. never push the trunk - only merge requests reach it
  3. never rebase the trunk; upstream comes in by merge only
  4. force-push only a feature branch, only --force-with-lease, only after a backup
     branch is confirmed on origin
  5. sync MRs are merged as merges, ship MRs fast-forward / rebase
  6. never commit on the mirror; it is only ever fast-forwarded to upstream and pushed

Sequence rule: sync first, then ship; rebase locally, merge globally.

Usage:
    forkflow.py status [--fetch | --offline] [-C DIR]
    forkflow.py check [-C DIR]
    forkflow.py sync [--continue] [--mr] [--merge] [--title T] [-C DIR] [--dry-run] [--force]
    forkflow.py ship [--continue] [--mr] [--merge] [--title T] [--message-file F] [-C DIR] [--dry-run] [--force]
    forkflow.py land [--force] [-C DIR] [--dry-run]
    forkflow.py setup [--upstream NAME] [--upstream-url URL] [--trunk NAME] [--mirror NAME]
                      [-C DIR] [--dry-run] [--force]
    forkflow.py --test                        # run the embedded test suite

Common flags (-C DIR, --dry-run, --force) may be given before or after the subcommand.

Exit codes:
    0   done, dry run, or nothing to do
    1   --test had failures
    2   precondition (dirty tree, missing remote, diverged mirror, ...)
    3   invariant checked by `check` (gate command failed, branch not on the trunk's tip)
    4   conflicts - resolve, then rerun with --continue
    5   rewrite safety (backup not confirmed, tree hash mismatch, push rejected)
    6   merge request not created or not merged (--merge only) - the branch is pushed;
        merge by hand, then forkflow land
    130 interrupted

`.forkflow.toml` is optional; reading it needs Python 3.11+ (tomllib). A config file that is
present but cannot be read is exit 2 for every subcommand - it carries the branch names.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence, Tuple

CONFIG_FILE = ".forkflow.toml"
STATE_FILE = "forkflow-state.json"   # in .git: ties a `--continue` run to the backup it belongs to
HOOK_MARK = "# forkflow pre-push hook"
DEFAULT_TRUNK = "develop"
DEFAULT_SYNC_PREFIX = "sync/"
DEFAULT_BACKUP_PREFIX = "backup/"
README_POINTER = ("See *Adopting forkflow in an existing fork* in the project README "
                  "(github.com/cloud-simple/ai-thingz, section `forkflow`)")
TAIL_LINES = 12                      # last lines of a failed command shown (a gate, the MR tool)
MERGE_TREE_GIT = (2, 38)             # `git merge-tree --write-tree` - older git skips the simulation
BOTH_SIDES_SNIFF = 8192              # bytes of a blob looked at for a NUL before it is "binary"
BOTH_SIDES_WIDTH = 48                # column the both-sides table pads paths to
MAX_SYNC_RERUNS = 100                # `--force` reruns: `<name>-2` .. `<name>-99` before it gives up
EXIT_INTERRUPTED = 130               # Ctrl-C, as the module docstring publishes it
EXIT_NOT_MERGED = 6                  # --merge: the branch is pushed, the merge request is
                                     # not merged (or not created) - all before it stands
PUBLISHED_KEEP = 100                 # remembered (branch, commit) pushes - see record_published

# The port each scheme reaches without being told: `ssh://host:22/o/r` and `host:o/r` are one
# repository, and rule 1 is about the repository. Kept in step with `ff_repo_id` in the hook.
DEFAULT_PORTS = {"ssh": "22", "git+ssh": "22", "git": "9418", "http": "80", "https": "443"}

# The names git resolves *before* `refs/heads/<name>`: `git rev-parse HEAD` is the pseudo-ref in
# $GIT_DIR, never the branch, and `origin/HEAD` is the remote's default branch. A trunk or mirror
# called one of these makes every unqualified name in this script mean something else.
PSEUDO_REFS = frozenset(("HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "REBASE_HEAD",
                         "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_HEAD", "AUTO_MERGE", "@"))

# What each backup is for, printed with the rollback line so the restore point is understood.
BACKUP_PURPOSE = {
    "pre-sync": ("a restore point of the trunk from before this sync - it rewrites nothing; "
                 "if the sync turns out wrong, revert it through a new branch and an MR, "
                 "never by force-pushing the trunk"),
    "pre-ship": "the feature branch as it was before the rebase and the squash",
}


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #

class Fail(Exception):
    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


def git(*args: str, cwd: Optional[str] = None, check: bool = True) -> str:
    """Run git; raise Fail(..., 2) on failure unless check=False (then return "")."""
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if p.returncode != 0:
        if check:
            raise Fail(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
        return ""
    return p.stdout.decode("utf-8", "replace").strip()


def git_ok(*args: str, cwd: Optional[str] = None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def git_rc(*args: str, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """(returncode, stdout, stderr) - for every call whose failure has its own exit code."""
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def shell(cmd: str, cwd: str) -> Tuple[int, str]:
    """Run a configured gate command with `sh -c`; output is stdout and stderr interleaved."""
    p = subprocess.run(["sh", "-c", cmd], cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def tail_lines(text: str, count: int) -> list:
    """The last `count` non-blank lines - what a failing gate command is judged by."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[-count:]


def write_temp(text: str, name: str) -> str:
    """A file for `-F <file>`: commit and merge messages are never `-m` with embedded newlines."""
    fd, path = tempfile.mkstemp(prefix="forkflow-", suffix="-" + name)
    with os.fdopen(fd, "w") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return path


def utc_stamp(fmt: str = "%Y%m%d") -> str:
    """Every timestamp forkflow puts in a name or a title - one clock, one place to freeze."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(fmt)


def _parse_version(text: str) -> Tuple[int, int]:
    """First two numeric components of `git version 2.39.5 (Apple Git-154)`."""
    nums = re.findall(r"\d+", text)
    if len(nums) < 2:
        return (0, 0)
    return (int(nums[0]), int(nums[1]))


def git_version() -> Tuple[int, int]:
    return _parse_version(git("version", check=False))


def has_ref(root: str, ref: str) -> bool:
    """True when the fully qualified ref exists."""
    return git_ok("show-ref", "--verify", "--quiet", ref, cwd=root)


def rev(root: str, spec: str) -> str:
    """Full SHA of spec, or "" when it does not resolve."""
    return git("rev-parse", "--verify", "-q", spec + "^{commit}", cwd=root, check=False)


def git_path(root: str, name: str) -> str:
    """Absolute path of `name` inside this worktree's git directory, "" when git cannot say.

    `--git-path` answers relatively for the main worktree and absolutely for a linked one, so
    every caller has to join the relative answer with the root before it can be opened."""
    p = git("rev-parse", "--git-path", name, cwd=root, check=False)
    if not p:
        return ""
    return p if os.path.isabs(p) else os.path.join(root, p)


def short(sha: str) -> str:
    return sha[:8] if sha else "-"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

CONFIG_STRINGS = ("upstream", "upstream_branch", "mirror", "trunk",
                  "sync_prefix", "backup_prefix", "merge")
# `merge`: who merges this fork's merge requests. "manual" (the default) means a person does,
# through the platform; "self" means whoever opened them - the solo fork - and is what lets
# `sync --merge` / `ship --merge` merge the request they have just opened.
MERGE_MODES = ("manual", "self")


def have_tomllib() -> bool:
    """Whether `.forkflow.toml` can be read at all - Python 3.11+ (or a backport on the path)."""
    try:
        import tomllib  # noqa: F401   Python 3.11+
    except ImportError:
        return False
    return True


def configures_nothing(text: str) -> bool:
    """True when the file holds nothing but blank lines and `#` comments."""
    return all(not line.strip() or line.strip().startswith("#") for line in text.splitlines())


def parse_config(text: str, where: str) -> dict:
    """The parsed `.forkflow.toml`, wherever the bytes came from - the working tree, or a
    `git show <rev>:.forkflow.toml` of what a branch carried before a merge. A value of the
    wrong type is a hard failure: it names the branches every safety check depends on."""
    if not have_tomllib():
        # a file of nothing but comments configures nothing: reading it as `{}` is exactly what
        # tomllib would have said, and refusing it would brick every subcommand over the
        # commented template `setup` used to leave behind
        if configures_nothing(text):
            return {}
        raise Fail(f"{where} needs Python 3.11+ (tomllib) to be read; "
                   f"this is Python {sys.version_info[0]}.{sys.version_info[1]}. "
                   f"Remove the file or run forkflow with a newer Python.")
    import tomllib  # Python 3.11+, guarded by have_tomllib() above
    try:
        cfg = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Fail(f"{where}: {exc}")
    # every value ends up in a git command line: a wrong type is a traceback, not a workflow
    for key in CONFIG_STRINGS:
        if key in cfg and not isinstance(cfg[key], str):
            raise Fail(f"{where}: `{key}` must be a string, "
                       f"not {type(cfg[key]).__name__}")
    gate = cfg.get("gate")
    if gate is not None and (not isinstance(gate, list)
                             or any(not isinstance(c, str) for c in gate)):
        raise Fail(f"{where}: `gate` must be a list of shell commands")
    merge = cfg.get("merge")
    if merge is not None and merge not in MERGE_MODES:
        raise Fail(f"{where}: `merge` must be "
                   + " or ".join(f'"{mode}"' for mode in MERGE_MODES)
                   + f', not "{merge}"')
    return cfg


def load_config(root: str) -> dict:
    """{} when absent. A config that is there but cannot be read is a hard failure."""
    path = os.path.join(root, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise Fail(f"{CONFIG_FILE} cannot be read: {exc}")
    return parse_config(text, CONFIG_FILE)


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #

def detect_platform(url: str) -> str:
    if not url:
        return "unknown"
    host = url
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?([^/:]+)", url)
    if m:
        host = m.group(1)
    else:
        m = re.match(r"^(?:[^@/]+@)?([^/:]+):", url)          # scp-like: git@host:path
        if m:
            host = m.group(1)
        elif url.startswith("/") or url.startswith("."):       # local path
            return "unknown"
    host = host.lower()
    if "gitlab" in host:
        return "gitlab"
    if "github" in host:
        return "github"
    return "unknown"


@dataclass
class Ctx:
    root: str
    cfg: dict = field(default_factory=dict)
    origin: str = "origin"
    origin_url: str = ""
    upstream: str = "upstream"
    upstream_url: str = ""
    upstream_branch: str = "main"
    trunk: str = DEFAULT_TRUNK
    mirror: str = "main"
    platform: str = "unknown"
    dry_run: bool = False
    sync_prefix: str = DEFAULT_SYNC_PREFIX
    backup_prefix: str = DEFAULT_BACKUP_PREFIX
    merge: str = MERGE_MODES[0]          # "manual": see CONFIG_STRINGS

    def up(self) -> str:
        return f"{self.upstream}/{self.upstream_branch}"


def no_upstream_message(name: Optional[str], others: Sequence[str]) -> str:
    """One wording for the two places that find no remote for the original project."""
    what = (f"remote `{name}` does not exist" if name else
            ("several non-origin remotes (" + ", ".join(others) + ")" if others
             else "no remote for the original project"))
    return (f"{what}; run `forkflow setup --upstream-url <URL>` "
            f"(or `forkflow setup --upstream <NAME>` for an existing remote)")


def valid_branch_name(name: str) -> bool:
    """git's own verdict, plus the names git's own parsers read as something else.

    The names reach refs, shell commands and the generated hook, so a name git would refuse is
    refused here rather than interpolated anywhere. `check-ref-format` accepts `+foo` and
    `-foo`, but a refspec beginning with `+` means *force* and an argument beginning with `-`
    is an option: such a name turns an ordinary command into a different one.

    It also accepts `refs/heads/HEAD`, and that is worse: `HEAD` as the trunk makes
    `git rev-parse HEAD` the checked-out commit rather than the branch, and `origin/HEAD` the
    remote's default branch - so `ship` stops recognising the trunk it must never push."""
    if not name or name[0] in "+-" or name in PSEUDO_REFS:
        return False
    return git_ok("check-ref-format", f"refs/heads/{name}")


def mirror_divergence(ctx: Ctx) -> Tuple[bool, bool]:
    """(diverged, ahead) of the local mirror against `upstream/<branch>` as last fetched.

    (False, False) when there is no local mirror, or when either ref is missing - "cannot tell"
    is not "diverged". Recomputed after a fetch: what this answers is only ever as true as the
    upstream ref it was asked about."""
    if not has_ref(ctx.root, f"refs/heads/{ctx.mirror}"):
        return (False, False)
    rc, _, _ = git_rc("merge-base", "--is-ancestor", ctx.mirror, ctx.up(), cwd=ctx.root)
    if rc != 1:
        return (False, False)
    ahead, _, _ = git_rc("merge-base", "--is-ancestor", ctx.up(), ctx.mirror, cwd=ctx.root)
    return (True, ahead == 0)


def mirror_from_origin(ctx: Ctx) -> bool:
    """True when everything the local mirror carries is also on `origin/<mirror>`.

    That is the ordinary team state, not divergence: a teammate's `sync` advanced
    `origin/<mirror>` to an upstream commit this clone has not fetched from `upstream` yet, and
    `git fetch origin` fast-forwarded the local mirror onto it. Only `push_mirror()` and
    `advance_mirror()`, which run after the command's own fetch, can tell that apart from work
    committed on the mirror - and they both refuse it. Refusing here instead would fail `sync`,
    `ship` and `check` in a state every fork with two people in it reaches."""
    remote = f"refs/remotes/{ctx.origin}/{ctx.mirror}"
    if not has_ref(ctx.root, remote):
        return False
    rc, _, _ = git_rc("merge-base", "--is-ancestor", ctx.mirror, remote, cwd=ctx.root)
    return rc == 0


def push_urls(root: str, remote: str) -> list:
    """Every URL `git push <remote>` would reach.

    `remote.<name>.pushurl` is multi-valued and each one is pushed to; with none configured
    git pushes to the fetch URL. `get-url --push --all` answers with the fetch URL in that
    case, so this is the whole picture either way."""
    out = git("remote", "get-url", "--push", "--all", remote, cwd=root, check=False)
    return [u.strip() for u in out.splitlines() if u.strip()]


def canonical_path(path: str, base: Optional[str] = None) -> str:
    """`path` with `.`, `..` and symlinks resolved - when it names a directory that is there.

    A fork whose upstream is a local path or a `file:` URL is an ordinary case (every fixture
    the test suite builds is one), and `../upstream.git`, `/srv/upstream.git/.` and a symlink
    to it all reach the repository rule 1 is about. The hook does the same with
    `CDPATH= cd -P -- "$r" && pwd -P`, which can only succeed on a directory that exists - so
    a path that does not resolve keeps the spelling it was given, on both sides.

    `base` is what a relative path is resolved against: the hook runs with git's own working
    directory, the top of the working tree, so every caller here passes the repository root."""
    if not path:
        return path
    try:
        full = path if os.path.isabs(path) else os.path.join(base or os.getcwd(), path)
        if os.path.isdir(full):
            return os.path.realpath(full)
    except (OSError, ValueError):                         # a NUL, a name too long, a loop
        pass
    return path


def split_remote(url: str) -> Tuple[str, str, str]:
    """(scheme, authority, path) of a git URL - one parser for both readers of one.

    `https://git@host:443/o/r.git`, `ssh://host:22/o/r` and the scp-like `git@host:o/r.git`
    all split the same way: the authority is the host as written, without a `user@` and
    without a port its scheme implies. It is "" when the URL names no host at all - a local
    path, however it is spelled, `file://localhost/p` included - and the path is then the
    whole URL. `repo_id` folds what this returns into one comparable id; `forge_path` reads
    the project out of it."""
    u = (url or "").strip().rstrip("/")
    scheme, host = "", False
    if "://" in u:
        scheme, _, u = u.partition("://")
        scheme, host = scheme.lower(), True
    elif ":" in u and "/" not in u.split(":", 1)[0]:      # scp-like [user@]host:path
        head, _, path = u.partition(":")
        u, scheme, host = head + "/" + path.lstrip("/"), "ssh", True
    if not host:
        return "", "", u
    first, sep, rest = u.partition("/")
    authority = first.split("@")[-1]                      # a `user@` names no repository
    name, _, port = authority.rpartition(":")
    if name and port.isdigit() and port == DEFAULT_PORTS.get(scheme, ""):
        authority = name                                  # `host:443` under https is `host`
    # RFC 3986 §3.2.2: a host is case-insensitive and may carry a root-label dot, and git
    # accepts every one of these spellings - `file://LOCALHOST/p` really did reach the
    # original project while this compared the authority as it was written
    if scheme == "file" and authority.lower() in ("localhost", "localhost."):
        authority = ""                                    # `file://localhost/p` is the path
    return scheme, authority, sep + rest


def repo_id(url: str, base: Optional[str] = None) -> str:
    """`host/path` of a git URL, so two spellings of one repository compare equal.

    `https://git@host/o/r.git`, `ssh://host:22/o/r` and `git@host:o/r.git` are the same
    project. A URL with a host is folded whole: GitHub and GitLab both resolve `Owner/Repo`
    case-insensitively, so a spelling that differs only in case is a live alias onto the same
    repository. A local path - `/srv/repo.git`, `file:///srv/repo.git`, `file://localhost/…`
    or a relative `../repo.git` - keeps its case, because a path is not case-folded the way a
    host name is, but is canonicalised (see `canonical_path`) so `..`, `/.` and a symlink do
    not each read as a repository of their own.

    The generated hook's `ff_repo_id` applies the same rules, so what this script refuses and
    what the hook refuses are the same set of URLs."""
    _, authority, path = split_remote(url)
    if not authority:                                     # a local path, however it is spelled
        u = canonical_path(path, base)
    else:
        u = (authority + path).lower()
    return u[:-4] if u.endswith(".git") else u


def forge_path(url: str) -> str:
    """The project a hosted URL names - `owner/repo`, or a GitLab `group/subgroup/project`
    with every segment kept - as it is spelled; "" when the URL names no such project.

    The platform report builds every path it reads or prints out of this, rather than out of
    gh's `{owner}/{repo}` or glab's `:fullpath`. Those two are resolved by the tool from the
    repository it is run in, and both answer with the remote named `upstream` when there is
    one - the original project, which `setup` itself adds. A fix command carrying them reads
    upstream's settings and, pasted by somebody who has admin there too, writes them: rule 1
    undone by this tool's own advice.

    The spelling is kept rather than folded the way `repo_id` folds it: a repository
    addressed in the wrong case answers a GET with a redirect, and a redirect is not followed
    for the PATCH and PUT these commands are."""
    _, authority, path = split_remote(url)
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    if not authority or len(parts) < 2 or not all(parts):
        return ""                                         # no host, or no project under it
    return "/".join(parts)


def origin_pushes_to_upstream(root: str, origin: str, upstream: str) -> Optional[str]:
    """The origin push URL that reaches the original project, or None.

    `git push origin` sends to `remote.origin.pushurl` when there is one, and nothing else in
    this script looks at that key: an origin whose push URL is the upstream repository turns
    every `push()` here - and the trunk bootstrap `setup` runs before the hook exists - into a
    push to the project we must never write to (rule 1)."""
    theirs = {repo_id(u, root) for u in push_urls(root, upstream)}
    theirs.add(repo_id(git("remote", "get-url", upstream, cwd=root, check=False), root))
    theirs.discard("")
    theirs.discard(repo_id("DISABLED", root))   # what `setup` sets, and not a repository
    for url in push_urls(root, origin):
        if repo_id(url, root) in theirs:
            return url
    return None


def resolve_upstream_remote(root: str, args: Optional[argparse.Namespace], cfg: dict,
                            remotes: Sequence[str], need_upstream: bool,
                            named: Optional[str] = None) -> Tuple[str, str]:
    """(remote name, URL) of the original project: `named`, the `--upstream` flag, the config,
    or the one non-origin remote there is.

    With none of those, `need_upstream` decides between a failure and a placeholder name -
    `setup` has to resolve a Ctx before the remote it is about to add exists."""
    name = named or getattr(args, "upstream", None) or cfg.get("upstream")
    others = [r for r in remotes if r != "origin"]
    if name == "origin":
        raise Fail("`origin` is the fork; the upstream remote is the original project - "
                   "they cannot be the same remote")
    if not name:
        name = others[0] if len(others) == 1 else None
    if not name or name not in remotes:
        if need_upstream:
            raise Fail(no_upstream_message(name, others))
        return (name or "upstream", "")
    return (name, git("remote", "get-url", name, cwd=root, check=False))


def resolve_upstream_branch(root: str, upstream: str, cfg: dict) -> str:
    """The branch of the original project the mirror copies: the config, then
    `<upstream>/HEAD`, then whichever of `main`/`master` is fetched, then `main`."""
    ub = cfg.get("upstream_branch")
    if not ub:
        head = git("symbolic-ref", "-q", f"refs/remotes/{upstream}/HEAD", cwd=root, check=False)
        prefix = f"refs/remotes/{upstream}/"
        if head.startswith(prefix):
            ub = head[len(prefix):]
    if not ub:
        for cand in ("main", "master"):
            if has_ref(root, f"refs/remotes/{upstream}/{cand}"):
                ub = cand
                break
    return ub or "main"


def resolve_ctx(cwd: str, args: Optional[argparse.Namespace] = None, need_upstream: bool = True,
                need_trunk: bool = True, strict_mirror: bool = True,
                upstream: Optional[str] = None) -> Ctx:
    """Build the Ctx and enforce the preconditions each subcommand needs.

    `upstream` names the remote outright, for the one caller that has just added it and must
    not wait for the flag or the config to catch up (`setup`)."""
    cwd = os.path.abspath(cwd)
    if not git_ok("rev-parse", "--is-inside-work-tree", cwd=cwd):
        raise Fail(f"{cwd} is not inside a git repository")
    root = git("rev-parse", "--show-toplevel", cwd=cwd)
    cfg = load_config(root)
    remotes = git("remote", cwd=root).split()
    if "origin" not in remotes:
        raise Fail("no `origin` remote: forkflow expects the fork to be `origin`")

    upstream, upstream_url = resolve_upstream_remote(root, args, cfg, remotes,
                                                     need_upstream, upstream)
    ub = resolve_upstream_branch(root, upstream, cfg)

    if upstream in remotes:
        # rule 1 is about a repository, not a remote name: `git push origin` obeys
        # `remote.origin.pushurl`, so an origin aliased onto the original project has to be
        # refused before anything here pushes - `setup` bootstraps the trunk before the hook
        aliased = origin_pushes_to_upstream(root, "origin", upstream)
        if aliased:
            raise Fail(f"`origin` pushes to {aliased}, which is `{upstream}` - the original "
                       f"project: every push here would go to it (rule 1). Remove the alias "
                       f"with `git config --unset-all remote.origin.pushurl`, then point "
                       f"`origin` at the fork")

    mirror = getattr(args, "mirror", None) or cfg.get("mirror") or ub
    trunk = getattr(args, "trunk", None) or cfg.get("trunk") or DEFAULT_TRUNK
    if trunk == mirror:
        raise Fail(f"trunk and mirror are both `{trunk}`: the trunk carries our work, "
                   f"the mirror is a pure copy of upstream - they cannot be the same branch")
    sync_prefix = cfg.get("sync_prefix") or DEFAULT_SYNC_PREFIX
    backup_prefix = cfg.get("backup_prefix") or DEFAULT_BACKUP_PREFIX
    for role, value in (("trunk", trunk), ("mirror", mirror),
                        ("upstream_branch", ub),
                        ("sync_prefix", sync_prefix + "x"),
                        ("backup_prefix", backup_prefix + "x")):
        if not valid_branch_name(value):
            raise Fail(f"`{role}` would make the branch name `{value}`, which git refuses "
                       f"(`git check-ref-format {sh_arg(f'refs/heads/{value}')}`)")

    ctx = Ctx(root=root, cfg=cfg, origin="origin",
              origin_url=git("remote", "get-url", "origin", cwd=root, check=False),
              upstream=upstream, upstream_url=upstream_url, upstream_branch=ub,
              trunk=trunk, mirror=mirror,
              dry_run=bool(getattr(args, "dry_run", False)),
              sync_prefix=sync_prefix, backup_prefix=backup_prefix,
              merge=cfg.get("merge") or MERGE_MODES[0])
    ctx.platform = detect_platform(ctx.origin_url)

    local_mirror = has_ref(root, f"refs/heads/{ctx.mirror}")
    remote_mirror = has_ref(root, f"refs/remotes/{ctx.origin}/{ctx.mirror}")
    if strict_mirror and not (local_mirror or remote_mirror):
        # "not in this clone" is all a remote-tracking ref can say: a narrowed fetch refspec
        # hides an `origin/<mirror>` that is really there, and `setup` is what asks origin
        raise Fail(f"mirror `{mirror}` is in this clone neither as a branch nor as "
                   f"`{ctx.origin}/{mirror}` (a narrowed fetch refspec can hide one that is "
                   f"on origin) - run `forkflow setup`")
    if local_mirror:
        if not has_ref(root, f"refs/remotes/{upstream}/{ub}"):
            if strict_mirror:
                raise Fail(f"`{ctx.up()}` is not fetched yet: run `git fetch {sh_arg(upstream)}`")
        else:
            diverged, _ = mirror_divergence(ctx)
            if diverged and strict_mirror and not mirror_from_origin(ctx):
                raise Fail(f"`{ctx.mirror}` carries commits that are in neither `{ctx.up()}` "
                           f"as last fetched nor `{ctx.origin}/{ctx.mirror}`: run "
                           f"`git fetch {sh_arg(upstream)}` and try again; if it is still ahead "
                           f"afterwards it cannot be the mirror. {README_POINTER}")

    if need_trunk and not has_ref(root, f"refs/remotes/{ctx.origin}/{trunk}"):
        raise Fail(f"trunk `{trunk}` is not on origin - run `forkflow setup`")
    return ctx


# --------------------------------------------------------------------------- #
# the run state - what `--continue` has to know about the run it resumes
#
# A backup is only a restore point if the run that resumes knows *which* backup is its own.
# Guessing "the newest backup/*-<reason> branch" points a rollback at someone else's branch,
# so `sync` and `ship` record theirs here and `--continue` reads it back.
# --------------------------------------------------------------------------- #

def state_path(ctx: Ctx) -> str:
    return git_path(ctx.root, STATE_FILE)


def read_state(ctx: Ctx) -> dict:
    path = state_path(ctx)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(ctx: Ctx, data: dict) -> None:
    path = state_path(ctx)
    if not path:
        return
    try:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
    except OSError:
        pass                    # a restore point that cannot be recorded is still a restore point


def write_state(ctx: Ctx, reason: str, entry: Optional[dict]) -> None:
    """Record (or, with entry=None, forget) what a `--continue` of this kind may resume."""
    if ctx.dry_run:
        return
    data = read_state(ctx)
    if entry is None:
        data.pop(reason, None)
    else:
        data[reason] = entry
    save_state(ctx, data)


def record_published(ctx: Ctx, branch: str, commit: str) -> None:
    """Remember that this clone put `commit` on `origin/<branch>`.

    This is the only trustworthy answer to "may `ship` replace `origin/<branch>`?". The branch
    reflog is not one: a teammate's commit reaches `refs/heads/<branch>`'s reflog through an
    ordinary `git pull` or `git checkout`, and could then be reset away and force-pushed away
    on the strength of having once been there.

    Backups are left out - they are written once and never rewritten, so nothing ever has to
    prove one is ours - and the list keeps only the newest `PUBLISHED_KEEP` entries, so the
    state file cannot grow without bound."""
    if ctx.dry_run or not commit or branch.startswith(ctx.backup_prefix):
        return
    data = read_state(ctx)
    entries = data.get("published")
    kept = [e for e in (entries if isinstance(entries, list) else [])
            if isinstance(e, list) and len(e) == 2 and e != [branch, commit]]
    kept.append([branch, commit])
    data["published"] = kept[-PUBLISHED_KEEP:]
    save_state(ctx, data)


def published_here(ctx: Ctx, branch: str, commit: str) -> bool:
    """True when `record_published` saw this clone put `commit` on `origin/<branch>`."""
    entries = read_state(ctx).get("published")
    return [branch, commit] in (entries if isinstance(entries, list) else [])


def resumable(ctx: Ctx, reason: str, branch: str) -> dict:
    """The recorded entry for this branch, {} when this run has no parent to resume."""
    entry = read_state(ctx).get(reason)
    if isinstance(entry, dict) and entry.get("branch") == branch:
        return entry
    return {}


PENDING_FIELDS = ("kind", "branch", "commit", "base")   # what `land` needs; `mr` is display


def record_pending(ctx: Ctx, kind: str, branch: str, base: str, url: str = "") -> None:
    """What is waiting to land, for `land` and `status`: the tip this run pushed on `branch`
    and the `origin/<trunk>` it was built on, with the merge request's URL once known.

    Written right after the push succeeds and before the merge request step, so a merge
    request that fails to open still leaves a landable record, then rewritten with the URL.
    Only the most recent run is kept: a second ship before the first lands overwrites it,
    and `land` handles one entry. A dry run writes nothing (`write_state`)."""
    write_state(ctx, "pending", {"kind": kind, "branch": branch,
                                 "commit": rev(ctx.root, f"refs/heads/{branch}"),
                                 "base": base, "mr": url})


def pending_entry(ctx: Ctx) -> dict:
    """The recorded `pending` entry, {} unless it has the shape `record_pending` writes.

    A state file edited by hand, or written by an older version, is no state rather than
    a crash - the same defensive read as `resumable`."""
    entry = read_state(ctx).get("pending")
    if (isinstance(entry, dict)
            and all(isinstance(entry.get(k), str) for k in PENDING_FIELDS)
            and isinstance(entry.get("mr", ""), str)):
        return entry
    return {}


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def diff_names(ctx: Ctx, *args: str) -> list:
    """`git diff -z --name-only ...`, split on NUL.

    Without -z git C-quotes any path that is not plain ASCII (`"caf\\303\\251.txt"`), and
    every consumer here feeds the result straight back to git as a pathspec."""
    rc, out, _ = git_rc("diff", "-z", "--name-only", *args, cwd=ctx.root)
    if rc != 0:
        return []
    return [f for f in out.split("\0") if f]


def name_status(ctx: Ctx, *args: str) -> list:
    """`git diff -z --name-status ...` as (status, path, other) triples.

    -z for the same reason as `diff_names`. A rename or a copy carries two paths
    (`R100 <old> <new>`); everything else carries one and `other` is "". A git that cannot
    answer reports nothing: this is advisory reading, not a precondition."""
    rc, out, _ = git_rc("diff", "-z", "--name-status", *args, cwd=ctx.root)
    if rc != 0:
        return []
    fields = out.split("\0")
    records, i = [], 0
    while i < len(fields):
        code = fields[i]
        if not code:
            i += 1
            continue
        width = 3 if code[0] in ("R", "C") else 2
        rest = fields[i + 1:i + width]
        records.append((code, rest[0] if rest else "", rest[1] if len(rest) > 1 else ""))
        i += width
    return records


def step(label: str, cmd: str, result: str, dry: bool = False) -> None:
    prefix = "  would: " if dry else "  "
    print(f"{prefix}{label}  $ {cmd}  -> {result}")


def report_paths(label: str, cmd: str, paths: Sequence[str], noun: str) -> None:
    """One `step()` line counting the paths, then one line per path - the shape every
    "these files are in the way" report in this script has."""
    step(label, cmd, f"{len(paths)} {noun}")
    for path in paths:
        print(f"    {path}")


def clean_tree(ctx: Ctx) -> bool:
    """Staged or unstaged changes to tracked files block; untracked files do not."""
    return git("status", "--porcelain", "--untracked-files=no", cwd=ctx.root) == ""


def ahead_behind(ctx: Ctx, a: str, b: str) -> Tuple[Optional[int], Optional[int]]:
    """(commits in a but not b, commits in b but not a); (None, None) if either is missing."""
    rc, out, _ = git_rc("rev-list", "--left-right", "--count", f"{a}...{b}", cwd=ctx.root)
    if rc != 0:
        return (None, None)
    parts = out.split()
    if len(parts) != 2:
        return (None, None)
    return (int(parts[0]), int(parts[1]))


def upstream_tracked(ctx: Ctx, files: Sequence[str]) -> list:
    """The subset of files that exists in upstream's branch - editing those costs merges.

    One `ls-tree` for the whole tree, not one `cat-file` per file: this runs from `header()`
    on every subcommand, and a fork with a few hundred changed files would pay for each."""
    files = list(files)
    if not files:
        return []
    rc, out, _ = git_rc("ls-tree", "-r", "-z", "--name-only", ctx.up(), cwd=ctx.root)
    if rc != 0:
        return []
    tracked = set(f for f in out.split("\0") if f)
    return [f for f in files if f in tracked]


def divergence(ctx: Ctx) -> Tuple[Optional[int], Optional[int]]:
    """(files changed on origin/<trunk> since it left upstream, how many are upstream-tracked)."""
    trunk_ref = f"{ctx.origin}/{ctx.trunk}"
    if not rev(ctx.root, trunk_ref) or not rev(ctx.root, ctx.up()):
        return (None, None)
    mb = git("merge-base", ctx.up(), trunk_ref, cwd=ctx.root, check=False)
    if not mb:
        return (None, None)
    files = diff_names(ctx, mb, trunk_ref)
    return (len(files), len(upstream_tracked(ctx, files)))


def branch_files(ctx: Ctx) -> list:
    """Files the current branch changed since it left the trunk (upstream when there is no trunk)."""
    if not rev(ctx.root, "HEAD"):
        return []
    for base in (f"refs/remotes/{ctx.origin}/{ctx.trunk}", ctx.up()):
        if not rev(ctx.root, base):
            continue
        mb = git("merge-base", base, "HEAD", cwd=ctx.root, check=False)
        if not mb:
            continue
        return diff_names(ctx, mb, "HEAD")
    return []


def current_branch(ctx: Ctx) -> str:
    """Short name of the checked-out branch, "" when HEAD is detached."""
    return git("symbolic-ref", "-q", "--short", "HEAD", cwd=ctx.root, check=False)


def warn_upstream_tracked(ctx: Ctx, touched: Optional[Sequence[str]] = None) -> list:
    """Print (and return) the upstream-tracked files this branch touches. Never a failure:
    editing them is a permanent merge cost that is sometimes the right call.

    Silent on a sync branch: there the diff since the trunk *is* upstream's own delta, so
    the list would name upstream's files rather than our edits to them."""
    if current_branch(ctx).startswith(ctx.sync_prefix):
        return []
    touched = list(touched) if touched is not None else upstream_tracked(ctx, branch_files(ctx))
    if touched:
        print(f"  touches upstream-tracked files (WARNING, {len(touched)}):")
        for f in touched:
            print(f"    {f}")
    return touched


def hooks_path(root: str) -> str:
    """Absolute hooks directory of this worktree (follows core.hooksPath)."""
    return git_path(root, "hooks")


def hook_state(ctx: Ctx) -> str:
    """installed (ours) | foreign (someone else's) | missing."""
    d = hooks_path(ctx.root)
    path = os.path.join(d, "pre-push") if d else ""
    if not path or not os.path.exists(path):
        return "missing"
    try:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return "missing"
    return "installed" if HOOK_MARK in text else "foreign"


def ff_only(ctx: Ctx, branch: str) -> bool:
    return "--ff-only" in git("config", "--get", f"branch.{branch}.mergeOptions",
                              cwd=ctx.root, check=False)


def upstream_push(ctx: Ctx) -> str:
    """`DISABLED` when setup has disabled it, `<url> (LIVE)` while a push could still land."""
    url = git("remote", "get-url", "--push", ctx.upstream, cwd=ctx.root, check=False)
    return "DISABLED" if url == "DISABLED" else f"{url or '-'} (LIVE)"


def backups(ctx: Ctx) -> list:
    """Backup branches known from remote-tracking refs - newest (by name) first, no network."""
    pattern = f"refs/remotes/{ctx.origin}/{ctx.backup_prefix}*"
    out = git("for-each-ref", "--format=%(refname:short)", pattern, cwd=ctx.root, check=False)
    return sorted([line for line in out.splitlines() if line], reverse=True)


def _rel_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 90 * 60:
        return f"{int(seconds / 60)}m ago"
    if seconds < 36 * 3600:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def last_fetch(ctx: Ctx) -> str:
    """Age of the last `git fetch`, and which of the two remotes it reached.

    FETCH_HEAD is rewritten by every fetch, so its age alone only says "something was
    fetched": right after `ship`'s `git fetch origin` it reads seconds old while the upstream
    ref may be a day stale. Its lines name the remote each ref came from - in git's own
    spelling of the URL, without `user@` or `.git` - so `repo_id` tells which remotes the
    last fetch actually covered, and the header can say `(origin only - upstream not in it)`."""
    path = git_path(ctx.root, "FETCH_HEAD")
    if not path or not os.path.exists(path):
        return "never"
    age = _rel_age(max(0.0, time.time() - os.path.getmtime(path)))
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return age
    wanted = [(name, repo_id(url, ctx.root)) for name, url in
              ((ctx.origin, ctx.origin_url), (ctx.upstream, ctx.upstream_url)) if name and url]
    reached = set()
    for ln in lines:
        head, sep, src = ln.rpartition(" of ")
        if not sep:
            continue
        rid = repo_id(src.strip(), ctx.root)
        reached.update(name for name, want in wanted if rid == want)
    if not reached:
        return age
    names = ", ".join(name for name, _ in wanted if name in reached)
    missing = [name for name, _ in wanted if name not in reached]
    if missing:
        return f"{age} ({names} only - {', '.join(missing)} not in it)"
    return f"{age} ({names})"


def plus_minus(ctx: Ctx, a: str, b: str) -> str:
    """`+<ahead>/-<behind>` of a against b - the header's two count cells."""
    ahead, behind = ahead_behind(ctx, a, b)
    return f"+{ahead}/-{behind}"


def header(ctx: Ctx, sub: str, server: Optional[str] = None) -> None:
    """The common header. `server` is the upstream branch's tip as `status` just read it
    from the server, when it did: a fetched ref that no longer matches it is reported as
    such in the mirror line, so `(=)` never quietly means "= as of some earlier fetch"."""
    print(f"forkflow {sub}  origin={ctx.origin_url or '-'}  "
          f"upstream={ctx.upstream_url or '-'}  platform={ctx.platform}")
    print(f"  as of last fetch: {last_fetch(ctx)}")

    up_sha = rev(ctx.root, ctx.up())
    local_m = rev(ctx.root, f"refs/heads/{ctx.mirror}")
    origin_m = rev(ctx.root, f"refs/remotes/{ctx.origin}/{ctx.mirror}")
    ours = (f"refs/heads/{ctx.mirror}" if local_m
            else f"refs/remotes/{ctx.origin}/{ctx.mirror}")
    if not up_sha:
        vs_up = "unfetched"
    elif not (local_m or origin_m):
        vs_up = "no mirror"
    else:
        ahead, behind = ahead_behind(ctx, ours, ctx.up())
        if ahead is None:
            vs_up = "?"
        elif ahead:
            vs_up = "DIVERGED"
        elif behind:
            vs_up = f"mirror behind by {behind}"
        else:
            vs_up = "="
    if server and server != up_sha:
        vs_up = f"{vs_up} as fetched, server moved" if up_sha else "unfetched, on the server"
    if not origin_m or not local_m:
        vs_origin = "-"
    elif origin_m != local_m:
        ahead, behind = ahead_behind(ctx, f"refs/heads/{ctx.mirror}",
                                     f"refs/remotes/{ctx.origin}/{ctx.mirror}")
        vs_origin = f"unpushed {ahead}" if ahead else f"behind {behind}"
    else:
        vs_origin = "="
    print(f"  mirror  {ctx.mirror} {short(local_m)}   "
          f"{ctx.origin}/{ctx.mirror} {short(origin_m)} ({vs_origin})   "
          f"{ctx.up()} {short(up_sha) if up_sha else 'unfetched'} ({vs_up})")

    local_t = rev(ctx.root, f"refs/heads/{ctx.trunk}")
    origin_t = rev(ctx.root, f"refs/remotes/{ctx.origin}/{ctx.trunk}")
    if not origin_t:
        vs_origin_t = "missing"
    elif not local_t:
        vs_origin_t = "-"
    elif local_t == origin_t:
        vs_origin_t = "="
    else:
        vs_origin_t = plus_minus(ctx, f"refs/heads/{ctx.trunk}",
                                 f"refs/remotes/{ctx.origin}/{ctx.trunk}")
    if origin_t and up_sha:
        vs_up_t = (plus_minus(ctx, ctx.up(), f"refs/remotes/{ctx.origin}/{ctx.trunk}")
                   + f" vs {ctx.origin}/{ctx.trunk}")
    else:
        vs_up_t = f"vs {ctx.origin}/{ctx.trunk}: unknown"
    print(f"  trunk   {ctx.trunk} {short(local_t)}   "
          f"{ctx.origin}/{ctx.trunk} {short(origin_t) if origin_t else 'missing'} "
          f"({vs_origin_t})   {ctx.up()} ({vs_up_t})")

    n_files, n_tracked = divergence(ctx)
    if n_files is None:
        print("  divergence: unknown (fetch upstream and create the trunk first)")
    else:
        print(f"  divergence: {n_files} files, {n_tracked} upstream-tracked")


def fetch(ctx: Ctx, remotes: Sequence[str],
          refs: Sequence[str] = ()) -> Tuple[int, str, str, str]:
    """Refresh `remotes` and say what moved: (returncode, command, result, stderr).

    `result` is the `step()` line a successful fetch deserves - one `<ref> <old>..<new>` (or
    `<ref> unchanged`) per named ref, or "remote-tracking refs refreshed" when the caller
    names none. Nothing is printed and nothing is raised here: whether a failure stops the run
    or is only reported is the caller's, and `status` still has the refs on disk to report."""
    args = ["fetch"] + (["--multiple"] if len(remotes) > 1 else []) + list(remotes)
    cmd = "git " + " ".join(sh_arg(a) for a in args)
    before = dict((r, rev(ctx.root, r)) for r in refs)
    rc, _, err = git_rc(*args, cwd=ctx.root)
    if rc != 0:
        return (rc, cmd, "", err)
    moved = []
    for r in refs:
        now = rev(ctx.root, r)
        moved.append(f"{r} unchanged" if now == before[r]
                     else f"{r} {short(before[r])}..{short(now)}")
    return (0, cmd, ", ".join(moved) if moved else "remote-tracking refs refreshed", err)


def upstream_server_tip(ctx: Ctx) -> Tuple[Optional[str], str]:
    """The upstream branch's tip on the server, without fetching: (sha or None, why not).

    `git ls-remote` is one round-trip and writes nothing - no refs, no objects - which is
    what lets a read-only `status` still say whether upstream has moved since the last
    fetch. What it cannot say is by how many commits: those objects are not here until
    a fetch, so the answer is "moved" and a pointer at `--fetch`, never a count."""
    ref = f"refs/heads/{ctx.upstream_branch}"
    rc, out, err = git_rc("ls-remote", "--heads", ctx.upstream, ref, cwd=ctx.root)
    if rc != 0:
        tail = err.strip().splitlines()[-1] if err.strip() else f"exit {rc}"
        return (None, tail)
    for ln in out.splitlines():
        sha, _, name = ln.partition("\t")
        if name.strip() == ref:
            return (sha.strip(), "")
    return (None, f"{ref} is not on the server")


# --------------------------------------------------------------------------- #
# the three push helpers - nothing else in this script pushes
# --------------------------------------------------------------------------- #

def push(ctx: Ctx, branch: str, lease: Optional[str] = None, backup_ref: str = "") -> str:
    """Push a feature/sync/backup branch. Never the trunk, never the mirror, never --force.

    A lease means a rewrite of published history, and rule 4 allows that only behind a
    backup that is already confirmed on origin - so the caller has to name it.

    Both sides of the refspec are fully qualified: `<branch>:refs/heads/<branch>` with a branch
    named `+x` is read by git as *force* with the source `x`, which is a silent unconditional
    force-push of a different branch - no `--force` anywhere in sight."""
    if not valid_branch_name(branch):
        raise Fail(f"refusing to push `{branch}`: git's refspec grammar does not read that as "
                   f"one branch (a leading `+` means force, a leading `-` an option)")
    if branch == ctx.trunk:
        raise Fail(f"refusing to push the trunk `{branch}`: it is only ever reached "
                   f"through a merge request")
    if branch == ctx.mirror:
        raise Fail(f"refusing to push the mirror `{branch}` from here: only `sync` moves it, "
                   f"and only forward to {ctx.up()}")
    args = ["push"]
    if lease:
        if not backup_ref:
            raise Fail(f"refusing to force-push `{branch}` without a confirmed backup: "
                       f"rule 4 allows a rewrite only behind a restore point on "
                       f"{ctx.origin}", 5)
        args.append(f"--force-with-lease={branch}:{lease}")
    if not has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{branch}"):
        args.append("-u")
    args += [ctx.origin, f"refs/heads/{branch}:refs/heads/{branch}"]
    cmd = "git " + " ".join(sh_arg(a) for a in args)
    if ctx.dry_run:
        step("push", cmd, "not run (dry run)", dry=True)
        return cmd
    if lease:                     # the remote's own answer, not a remote-tracking ref we own
        rc, out, _ = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{backup_ref}",
                            cwd=ctx.root)
        if rc != 0 or f"refs/heads/{backup_ref}" not in out:
            raise Fail(f"the backup `{backup_ref}` is not on {ctx.origin}: refusing to "
                       f"force-push `{branch}` without a restore point", 5)
    rc, _, err = git_rc(*args, cwd=ctx.root)
    if rc != 0:
        step("push", cmd, "REJECTED")
        raise Fail(f"push of `{branch}` was rejected by {ctx.origin}:\n{err.strip()}", 5)
    # what a later `ship` reads back to know this tip is ours to replace, and not a teammate's
    record_published(ctx, branch, rev(ctx.root, f"refs/heads/{branch}"))
    step("push", cmd, "pushed")
    return cmd


def push_mirror(ctx: Ctx, target: str = "") -> str:
    """The only way the mirror reaches origin: never forced, only a pure copy of upstream.

    `target` is the commit this run moves the mirror to. A dry run has not moved the local
    mirror, so without it the ancestry check would judge a tip the real push never sends and
    refuse the ordinary state a teammate's sync leaves behind."""
    m = ctx.mirror
    refspec = f"refs/heads/{m}:refs/heads/{m}"
    shown = f"git push {sh_arg(ctx.origin)} {sh_arg(refspec)}"
    if target and rev(ctx.root, f"refs/remotes/{ctx.origin}/{m}") == target:
        # nothing to send: `origin/<mirror>` is already the commit this run moves it to
        step("mirror push", shown, "up to date")
        return shown
    if not has_ref(ctx.root, f"refs/heads/{m}"):
        if ctx.dry_run:              # advance_mirror would have created it; nothing to inspect
            cmd = shown
            step("mirror push", cmd, f"not run (dry run: `{m}` is created by this run)", dry=True)
            return cmd
        raise Fail(f"no local `{m}` to push")
    rc, _, _ = git_rc("merge-base", "--is-ancestor", m, ctx.up(), cwd=ctx.root)
    if rc == 1:
        raise Fail(f"`{m}` is not a pure copy of `{ctx.up()}`: refusing to push the mirror. "
                   f"{README_POINTER}")
    if rc != 0:
        raise Fail(f"cannot verify `{m}` against `{ctx.up()}`: "
                   f"run `git fetch {sh_arg(ctx.upstream)}`")
    if has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{m}"):
        # what this push sends: the local mirror, or the target a dry run has not moved it to
        sending = target or m
        what = short(target) if target else f"the local `{m}`"
        rc, _, _ = git_rc("merge-base", "--is-ancestor", f"{ctx.origin}/{m}", sending,
                          cwd=ctx.root)
        if rc != 0:
            raise Fail(f"`{ctx.origin}/{m}` is not an ancestor of {what}: fetch and "
                       f"fast-forward first - the mirror is never forced")
    args = ["push", ctx.origin, refspec]
    cmd = "git " + " ".join(sh_arg(a) for a in args)
    if ctx.dry_run:
        step("mirror push", cmd, "not run (dry run)", dry=True)
        return cmd
    rc, _, err = git_rc(*args, cwd=ctx.root)
    if rc != 0:
        step("mirror push", cmd, "REJECTED")
        raise Fail(f"push of the mirror `{m}` was rejected by {ctx.origin}:\n{err.strip()}", 5)
    step("mirror push", cmd, "pushed")
    return cmd


def branch_worktree(ctx: Ctx, branch: str) -> str:
    """Path of the worktree that has `branch` checked out, "" when none."""
    rc, out, _ = git_rc("for-each-ref", "--format=%(worktreepath)",
                        f"refs/heads/{branch}", cwd=ctx.root)
    if rc != 0:                     # git without %(worktreepath): only this worktree is visible
        cur = git("symbolic-ref", "-q", "--short", "HEAD", cwd=ctx.root, check=False)
        return ctx.root if cur == branch else ""
    return out.strip()


def mirror_worktree(ctx: Ctx) -> str:
    """Path of the worktree that has the mirror checked out, "" when none."""
    return branch_worktree(ctx, ctx.mirror)


def advance_mirror(ctx: Ctx, target: str) -> Tuple[str, str]:
    """Fast-forward the local mirror to target. Returns (old sha, new sha)."""
    m = ctx.mirror
    old = rev(ctx.root, f"refs/heads/{m}")
    if old and old != target:
        rc, _, _ = git_rc("merge-base", "--is-ancestor", old, target, cwd=ctx.root)
        if rc == 1:
            raise Fail(f"`{m}` is not an ancestor of {short(target)}: the mirror only ever "
                       f"moves forward. {README_POINTER}")
        if rc != 0:
            raise Fail(f"cannot compare `{m}` with {short(target)}: "
                       f"run `git fetch {sh_arg(ctx.upstream)}`")
    wt = mirror_worktree(ctx)
    here = wt and os.path.realpath(wt) == os.path.realpath(ctx.root)
    if wt and not here:
        raise Fail(f"mirror `{m}` is checked out in {wt}: advance it there, "
                   f"or remove that worktree")
    if old == target:
        step("mirror", f"git rev-parse {sh_arg(m)}", f"up to date at {short(target)}")
        return (old, old)
    if not old:
        cmd = f"git branch --no-track {sh_arg(m)} {short(target)}"
    elif here:
        cmd = f"git merge --ff-only {short(target)}"
    else:
        cmd = f"git update-ref {sh_arg(f'refs/heads/{m}')} {short(target)}"
    if ctx.dry_run:
        step("mirror", cmd, f"{short(old)} -> {short(target)}", dry=True)
        return (old, target)
    if not old:
        git("branch", "--no-track", m, target, cwd=ctx.root)
    elif here:
        rc, out, err = git_rc("merge", "--ff-only", target, cwd=ctx.root)
        if rc != 0:
            raise Fail(f"cannot fast-forward `{m}`:\n{(err or out).strip()}")
    else:
        git("update-ref", f"refs/heads/{m}", target, old, cwd=ctx.root)
    step("mirror", cmd, f"{short(old)} -> {short(target)}")
    return (old, target)


def bootstrap_trunk(ctx: Ctx, target: str) -> None:
    """setup only: create the trunk on a fresh fork, where it equals upstream."""
    t = ctx.trunk
    if has_ref(ctx.root, f"refs/heads/{t}") or has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{t}"):
        raise Fail(f"trunk `{t}` already exists: bootstrap only creates it on a fresh fork")
    cmd = (f"git branch --no-track {sh_arg(t)} {short(target)} && "
           f"git push {sh_arg(ctx.origin)} {sh_arg(f'refs/heads/{t}:refs/heads/{t}')}")
    if ctx.dry_run:
        step("trunk", cmd, f"would create {t} at {short(target)} and push it", dry=True)
        return
    git("branch", "--no-track", t, target, cwd=ctx.root)
    rc, _, err = git_rc("push", ctx.origin, f"refs/heads/{t}:refs/heads/{t}", cwd=ctx.root)
    if rc != 0:
        git("branch", "-D", t, cwd=ctx.root, check=False)
        step("trunk", cmd, "REJECTED")
        raise Fail(f"push of the new trunk `{t}` was rejected by {ctx.origin}:\n{err.strip()}", 5)
    step("trunk", cmd, f"created at {short(target)} and pushed")


# --------------------------------------------------------------------------- #
# backups and the merge simulation
# --------------------------------------------------------------------------- #

def backup_name(ctx: Ctx, reason: str) -> str:
    """`backup/<UTC YYYYMMDD-HHMMSS>-<reason>` - sortable, so `status` lists the newest first."""
    return f"{ctx.backup_prefix}{utc_stamp('%Y%m%d-%H%M%S')}-{reason}"


def backup(ctx: Ctx, reason: str, from_ref: str) -> str:
    """Create a restore point and prove it reached origin before anything is rewritten.

    Not confirmed by `ls-remote` means not a backup: the local branch is removed again and
    the caller stops with exit 5 rather than rewriting on the strength of a failed push."""
    name = backup_name(ctx, reason)
    src = rev(ctx.root, from_ref)
    if not src:
        raise Fail(f"cannot back up `{from_ref}`: it does not resolve to a commit")
    create = f"git branch {sh_arg(name)} {sh_arg(from_ref)}"
    purpose = BACKUP_PURPOSE.get(reason, "")

    if ctx.dry_run:
        step("backup", create, f"would keep {short(src)} as {name}", dry=True)
        print(f"    rollback: git reset --hard {sh_arg(f'{ctx.origin}/{name}')}")
        if purpose:
            print(f"    {purpose}")
        return name

    existing = rev(ctx.root, f"refs/heads/{name}")
    if existing and existing != src:
        raise Fail(f"backup branch `{name}` already exists at {short(existing)}: "
                   f"delete it, or retry in a second (the name carries a UTC timestamp)")
    if not existing:
        git("branch", name, from_ref, cwd=ctx.root)
    step("backup", create, f"{name} at {short(src)}")

    try:
        push(ctx, name)
    except Fail:
        git("branch", "-D", name, cwd=ctx.root, check=False)   # a backup nobody has is noise
        raise

    confirm = f"git ls-remote --heads {sh_arg(ctx.origin)} {sh_arg(f'refs/heads/{name}')}"
    rc, out, err = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{name}", cwd=ctx.root)
    if rc != 0 or f"refs/heads/{name}" not in out:
        step("backup", confirm, "NOT CONFIRMED")
        git("branch", "-D", name, cwd=ctx.root, check=False)
        raise Fail(f"backup `{name}` is not on {ctx.origin} after the push"
                   f"{': ' + err.strip() if err.strip() else ''}: refusing to go on "
                   f"without a confirmed restore point", 5)
    step("backup", confirm, "confirmed on origin")
    print(f"    rollback: git reset --hard {sh_arg(f'{ctx.origin}/{name}')}")
    if purpose:
        print(f"    {purpose}")
    return name


def simulate_merge(ctx: Ctx, target: str) -> Optional[Tuple[bool, list]]:
    """Predict the sync merge without touching the worktree: (clean, conflicting paths).

    None when git is too old to simulate - the merge itself is then the first answer.

    The only path-reading site that is not `-z` (see `diff_names`): `merge-tree`'s `-z` also
    changes how the sections themselves are terminated, and these paths are printed and put in
    the merge-request body, never handed back to git - so a C-quoted non-ASCII name is a
    display wart here, not a wrong pathspec."""
    shown_trunk = sh_arg(f"{ctx.origin}/{ctx.trunk}")
    cmd = f"git merge-tree --write-tree --name-only {shown_trunk} {short(target)}"
    if git_version() < MERGE_TREE_GIT:
        step("simulate", cmd, "simulation needs git 2.38+, skipping")
        return None
    rc, out, err = git_rc("merge-tree", "--write-tree", "--name-only",
                          f"{ctx.origin}/{ctx.trunk}", target, cwd=ctx.root)
    if rc == 0:
        step("simulate", cmd, "merges clean")
        return (True, [])
    lines = out.splitlines()
    # a conflict prints the merged tree's OID, the conflicting paths, a blank line, then messages;
    # any other failure (a ref that does not resolve) prints no OID at all
    if rc == 1 and lines and re.fullmatch(r"[0-9a-f]{40,64}", lines[0].strip()):
        conflicts = []
        for line in lines[1:]:
            if not line.strip():
                break
            conflicts.append(line.strip())
        report_paths("simulate", cmd, conflicts, "conflicting file(s)")
        return (False, conflicts)
    raise Fail(f"merge simulation failed: {(err or out).strip()}")


# --------------------------------------------------------------------------- #
# merge requests
# --------------------------------------------------------------------------- #

def merge_button(ctx: Ctx, kind: str) -> str:
    """How the MR must be merged. Squashing or rebasing a sync MR rewrites upstream's SHAs
    out of the trunk's ancestry and every later sync re-conflicts on the same hunks."""
    if kind == "sync":
        if ctx.platform == "gitlab":
            return ("merge it fast-forward - its tip is the merge commit; "
                    "never squash it, never rebase it")
        if ctx.platform == "github":
            return 'merge it with "Create a merge commit"; never squash it, never rebase it'
        return "merge it as a merge commit; never squash it, never rebase it"
    if ctx.platform == "gitlab":
        return "merge it fast-forward"
    if ctx.platform == "github":
        return ('merge it with "Rebase and merge", then delete the local branch - '
                "GitHub rewrites the commit's SHA")
    return "merge it fast-forward"


def mr_target(ctx: Ctx) -> Tuple[str, str]:
    """(the fork, spelled as `--repo` takes it; why it cannot be named) - one or the other.

    Both tools work out the repository they act on from the remotes when no repository flag
    is given, and both answer with the remote named `upstream` when there is one - the remote
    `setup` itself adds. A merge request command without `--repo` therefore opens the merge
    request on the ORIGINAL project: printed, it does that to whoever pastes it; with `--mr`
    this script does it itself. Rule 1, performed rather than merely advised.

    The value carries the host as well as the project - `https://github.com/acme/widget`,
    `ssh://gitlab.example.com/acme/team/widget` - because a bare `owner/repo` is resolved
    against the tool's *default* host and not against the one the fork is on: `glab --repo
    acme/team/widget` in a clone of a self-hosted GitLab asks gitlab.com, and `gh --repo
    acme/widget` asks github.com. Both accept a URL and read the host out of it (`glab mr
    create -R/--repo`: "OWNER/REPO or GROUP/NAMESPACE/REPO. The full URL or Git URL is also
    accepted"; `gh pr create -R/--repo`: "[HOST/]OWNER/REPO", a URL too). The scheme is the
    origin's own, so an ssh port stays an ssh port rather than becoming a web one.

    `forge_path` reads the project and `split_remote` the host - the same two readers the
    platform report's paths are built from, and no second URL parser. When the origin names
    no project there is nothing to aim at and no command to print: a local path, an
    unrecognised URL, or more than `owner/repo` on GitHub, which gh refuses outright
    ("invalid path"). `open_mr` says which URL it could not address instead."""
    if ctx.platform not in ("gitlab", "github"):
        return "", "unknown host"
    project = forge_path(ctx.origin_url)
    if not project or (ctx.platform == "github" and project.count("/") != 1):
        return "", f"{ctx.platform}, but `{ctx.origin_url or '-'}` names no project there"
    scheme, authority, _ = split_remote(ctx.origin_url)
    return f"{scheme or 'https'}://{authority}/{project}", ""


def mr_command(ctx: Ctx, branch: str, title: str, body_file: str) -> list:
    """The platform's MR command, or [] when the fork cannot be named (see `mr_target`).

    `--repo` is not optional and not a nicety: without it both tools aim at the original
    project. Anything that edits this command keeps it."""
    target, _ = mr_target(ctx)
    if not target:
        return []
    if ctx.platform == "gitlab":
        # `--yes` skips glab's "create this merge request?" confirmation: with --mr there is
        # no terminal to answer it, and on the first real fork the printed command was run
        # by hand every time for exactly that reason. gh needs nothing: --title and
        # --body-file already make it non-interactive, and it has no --yes to give.
        return ["glab", "mr", "create", "--repo", target,
                "--source-branch", branch, "--target-branch", ctx.trunk,
                "--title", title, "--description-file", body_file, "--remove-source-branch",
                "--yes"]
    if ctx.platform == "github":
        return ["gh", "pr", "create", "--repo", target,
                "--head", branch, "--base", ctx.trunk,
                "--title", title, "--body-file", body_file]
    return []


def run_tool(ctx: Ctx, cmd: Sequence[str]) -> Optional[subprocess.CompletedProcess]:
    """Run a platform tool (glab, gh) and answer with its process; None when it cannot run.

    stdin is closed: a prompt the flags did not cover fails fast as a non-zero exit, which
    the caller reports with the tool's stderr, instead of waiting on a terminal nobody is
    at. A tool that is not installed, or not executable, is an OSError rather than an exit
    code; the callers say "unavailable" for it and still print the command, which is worth
    running by hand. The one place the platform tools are run from - `open_mr` and the
    merge step share it, and `TestSourceInvariants` pins it."""
    try:
        return subprocess.run(list(cmd), cwd=ctx.root, capture_output=True,
                              stdin=subprocess.DEVNULL)
    except OSError:
        return None


def open_mr(ctx: Ctx, branch: str, title: str, body: str, run_it: bool) -> Tuple[str, str]:
    """Print the MR command and, with --mr, run it. Never a failure: the branch is pushed,
    the merge request is the only thing left to do.

    Answers (the command as shown, the merge request's URL). The URL is the first stdout
    line of the tool that starts with `http` - what both glab and gh print - and "" when
    the tool was not run, could not run or failed. It is kept for the `pending` record and
    for display only: nothing parses it, the merge step addresses the MR by its branch."""
    path = "<description file>" if ctx.dry_run else write_temp(body, "mr-body.md")
    cmd = mr_command(ctx, branch, title, path)
    if not cmd:
        # the same wording the platform report uses for the same origin URL
        step("mr", f"# origin {ctx.origin_url or '-'}",
             f"{mr_target(ctx)[1]} - open the merge request manually: "
             f"{branch} -> {ctx.trunk}", dry=ctx.dry_run)
        print(f"    title: {title}")
        if not ctx.dry_run:
            print(f"    description: {path}")
        return "", ""
    shown = " ".join(shlex.quote(c) for c in cmd)
    if ctx.dry_run:
        step("mr", shown, "not run (dry run)", dry=True)
        return shown, ""
    if not run_it:
        step("mr", shown, "not run (add --mr to run it)")
        print(f"    description: {path}")
        return shown, ""
    p = run_tool(ctx, cmd)
    if p is None:
        step("mr", shown, f"{cmd[0]} unavailable (not installed, or not runnable)")
        print(f"    description: {path}")
        return shown, ""
    out = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        step("mr", shown, f"FAILED (exit {p.returncode}) - open it yourself")
        for line in tail_lines(p.stderr.decode("utf-8", "replace"), TAIL_LINES):
            print(f"      {line}")
        print(f"    description: {path}")
        return shown, ""
    step("mr", shown, "created")
    for line in out.splitlines():
        print(f"    {line}")
    try:
        os.unlink(path)          # the description is on the platform now; nobody needs the file
    except OSError:
        pass
    url = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith("http")), "")
    return shown, url


def merge_command(ctx: Ctx, kind: str, branch: str, head: str) -> list:
    """The platform's merge command for the merge request `branch` opened, [] when the fork
    cannot be named (see `mr_target`).

    The merge request is addressed by its source branch - both tools accept that in place
    of a number, and the run already has it; the URL is never parsed. `--repo` names the
    fork for the same reason `mr_command` gives it. The method is the one rule 5 requires:
    on GitLab the project's own `merge_method` decides (`setup`'s report insists on `ff`), so
    glab gets neither `--squash` nor `--rebase`; GitHub has no project-level method, so gh is
    told per call - `--merge` for a sync (its tip is the merge commit, the "merge" button
    keeps it) and `--rebase` for a ship (the "rebase and merge" button, a fast-forward of one
    commit - a gh flag, not a git verb). The head-commit guard is always on: `--sha` /
    `--match-head-commit` merge only the exact commit this run pushed, never whatever the
    branch points at by then - rule 4's spirit. `--auto-merge=false` is what makes glab
    merge *now*: by default it arms merge-when-pipeline-succeeds and `land` finds nothing."""
    target, _ = mr_target(ctx)
    if not target:
        return []
    if ctx.platform == "gitlab":
        return ["glab", "mr", "merge", branch, "--repo", target, "--sha", head,
                "--auto-merge=false", "--remove-source-branch", "--yes"]
    if ctx.platform == "github":
        return ["gh", "pr", "merge", branch, "--repo", target, "--match-head-commit", head,
                "--merge" if kind == "sync" else "--rebase"]
    return []


def merge_mr(ctx: Ctx, kind: str, branch: str, url: str) -> None:
    """Merge the merge request `open_mr` just opened on `branch`, or exit 6.

    Runs after the push and after the `pending` record has the merge request's URL, so any
    failure here leaves a landable state: the branch is on origin, the merge request is
    open, and `forkflow land` finishes the job once it is merged by hand - which is what
    the exit-6 message says. A merge request that was never created (the tool failed, was
    missing, or printed no URL) is the same exit: there is nothing to merge. A dry run
    shows the merge and the landing it would chain into and runs neither."""
    head = "<pushed head>" if ctx.dry_run else rev(ctx.root, f"refs/heads/{branch}")
    cmd = merge_command(ctx, kind, branch, head)
    shown = " ".join(shlex.quote(c) for c in cmd)
    if ctx.dry_run:
        step("merge", shown or f"# {mr_target(ctx)[1]}", "not run (dry run)", dry=True)
        step("land", "forkflow land", "not run (dry run)", dry=True)
        return
    if not url or not cmd:
        raise Fail("the merge request was not created - the branch is pushed; open and "
                   "merge it by hand, then: forkflow land", EXIT_NOT_MERGED)
    p = run_tool(ctx, cmd)
    if p is None:
        step("merge", shown, f"NOT MERGED ({cmd[0]} unavailable: not installed, or not runnable)")
    elif p.returncode != 0:
        step("merge", shown, f"NOT MERGED (exit {p.returncode})")
        for line in tail_lines(p.stderr.decode("utf-8", "replace"), TAIL_LINES):
            print(f"      {line}")
    else:
        step("merge", shown, "merged")
        for line in p.stdout.decode("utf-8", "replace").strip().splitlines():
            print(f"    {line}")
        return
    raise Fail(f"the merge request was not merged - the branch is pushed; merge it by hand "
               f"({url}), then: forkflow land", EXIT_NOT_MERGED)


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_status(args: argparse.Namespace) -> int:
    """Read-only: writes nothing. Without --fetch the numbers are as of the last fetch,
    and one `ls-remote` says whether the upstream server has moved since - the stale
    `(=)` that read as "in sync" for a day on the first real fork is what that round-trip
    buys. --offline skips it and makes no network call at all."""
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=False, strict_mirror=False)
    fetch_step = None
    server = None
    server_step = None
    if getattr(args, "fetch", False):
        rc, cmd, result, err = fetch(ctx, (ctx.origin, ctx.upstream))
        tail = err.strip().splitlines()[-1] if err.strip() else "see git output"
        fetch_step = (cmd, result if rc == 0
                      else f"FAILED, reporting the refs on disk: {tail}")
    elif getattr(args, "offline", False):
        server_step = ("-", "not asked (--offline): the numbers are as of the last fetch")
    else:
        ref = f"refs/heads/{ctx.upstream_branch}"
        cmd = f"git ls-remote --heads {sh_arg(ctx.upstream)} {sh_arg(ref)}"
        sha, why = upstream_server_tip(ctx)
        fetched = rev(ctx.root, ctx.up())
        if sha is None:
            server_step = (cmd, f"server not reachable ({why}); the numbers are as of the last fetch")
        elif sha == fetched:
            server_step = (cmd, f"server at {short(sha)} = fetched")
        elif fetched:
            server = sha
            server_step = (cmd, f"server at {short(sha)}, fetched {short(fetched)} - upstream moved "
                                f"since the last fetch: `status --fetch` for the numbers, "
                                f"`forkflow sync` to take it")
        else:
            server = sha
            server_step = (cmd, f"server at {short(sha)}, not fetched here yet: `status --fetch`")

    header(ctx, "status", server=server)
    if fetch_step:
        step("fetch", fetch_step[0], fetch_step[1])
    if server_step:
        step("upstream", server_step[0], server_step[1])

    branch = current_branch(ctx)
    modified = [ln for ln in git("status", "--porcelain", "--untracked-files=no",
                                 cwd=ctx.root).splitlines() if ln]
    tree = "clean" if not modified else f"{len(modified)} modified"
    if not branch:
        print(f"  branch   (detached at {short(rev(ctx.root, 'HEAD'))})  tree: {tree}")
    else:
        if has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{branch}"):
            ahead, _ = ahead_behind(ctx, f"refs/heads/{branch}",
                                    f"refs/remotes/{ctx.origin}/{branch}")
            where = f"{ahead if ahead is not None else '?'} unpushed (vs {ctx.origin}/{branch})"
        else:
            where = "not on origin"
        print(f"  branch   {branch}  {where}  tree: {tree}")

    warn_upstream_tracked(ctx)

    found = backups(ctx)
    line = f"  backups  {len(found)} (refs/remotes/{ctx.origin}/{ctx.backup_prefix}*)"
    if found:
        line += ":  " + ", ".join(found[:3])
    print(line)

    hook = hook_state(ctx)
    push_url = upstream_push(ctx)
    ff_trunk, ff_mirror = ff_only(ctx, ctx.trunk), ff_only(ctx, ctx.mirror)
    print(f"  setup    upstream push: {push_url}   pre-push hook: {hook}   "
          f"ff-only: {ctx.trunk} {'yes' if ff_trunk else 'no'}, "
          f"{ctx.mirror} {'yes' if ff_mirror else 'no'}")

    todo = []
    if not has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{ctx.trunk}"):
        todo.append(f"trunk `{ctx.trunk}` is not on origin")
    if hook != "installed":
        todo.append(f"pre-push hook {hook}")
    if not push_url.startswith("DISABLED"):
        todo.append(f"`{ctx.upstream}` still has a live push URL")
    if not (ff_trunk and ff_mirror):
        todo.append("ff-only merge config not set")
    if todo:
        print("  hint     run `forkflow setup`: " + "; ".join(todo))
    # judged here rather than carried on the Ctx: with --fetch the refs `resolve_ctx` read
    # are a fetch old, and this verdict has to agree with the table printed above
    diverged, ahead = mirror_divergence(ctx)
    if ahead:
        print(f"  hint     mirror `{ctx.mirror}` is ahead of `{ctx.up()}` as last fetched: "
              f"run `git fetch {sh_arg(ctx.upstream)}`; if it is still ahead afterwards it carries "
              f"commits that are not upstream's. {README_POINTER}")
    elif diverged:
        print(f"  hint     mirror `{ctx.mirror}` has commits that are not in `{ctx.up()}`. "
              f"{README_POINTER}")
    return 0


def gate_commands(ctx: Ctx) -> list:
    """`gate = [...]` from the config - load_config has already refused every other shape."""
    return [c for c in (ctx.cfg.get("gate") or []) if c.strip()]


def config_text(ctx: Ctx, revision: Optional[str] = None) -> Optional[str]:
    """`.forkflow.toml` as of `revision`, or from the working tree when `revision` is None.
    None when it is not there at all."""
    if revision is None:
        try:
            with open(os.path.join(ctx.root, CONFIG_FILE), "r", encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError):
            return None
    rc, out, _ = git_rc("show", f"{revision}:{CONFIG_FILE}", cwd=ctx.root)
    return out if rc == 0 else None


def gate_at(ctx: Ctx, revision: str) -> Optional[list]:
    """The `gate` `.forkflow.toml` carried at `revision`; None when it cannot be read.

    Unreadable is not `[]`: an unknown gate is treated as a changed one, which shows rather
    than runs - the safe direction for a key that is arbitrary shell."""
    text = config_text(ctx, revision)
    if text is None:
        return []                         # no file there: no gate
    try:
        cfg = parse_config(text, f"{revision}:{CONFIG_FILE}")
    except Fail:
        return None
    return [c for c in (cfg.get("gate") or []) if c.strip()]


def config_changed_in_merge(ctx: Ctx, merge_sha: str) -> bool:
    """True when `.forkflow.toml` as it now stands is not what this branch carried before
    the sync merge - the reviewer of the MR has to see that, whatever key changed.

    The merge is not necessarily at HEAD: fixing what `check` refused means a commit on top
    of it, so the comparison is `<merge>^1` against `HEAD` rather than `HEAD^1` against
    `HEAD`. Both sides are commits, because the line this prints names a diff of two commits
    - and because `setup` leaves the file untracked: an untracked file is in no tree at all,
    so measuring it against a blob called every ordinary sync a config change and pointed
    the reviewer at a diff that prints nothing."""
    if not merge_sha:
        return False
    before, now = config_text(ctx, f"{merge_sha}^1"), config_text(ctx, "HEAD")
    return (before or "").strip() != (now or "").strip()


def gate_arrived_in_merge(ctx: Ctx, merge_sha: str) -> bool:
    """True when the `gate` this run would obey is not the one the branch had before the
    sync merge.

    A `gate` is arbitrary shell run with `sh -c`, and `.forkflow.toml` is a tracked file a
    sync is designed to bring in from the original project - so the one run that must not
    obey it is the one whose own merge changed it. Keyed on the `gate`, not on the file:
    upstream editing an unrelated line must not suppress this fork's own gate, and a commit
    made on top of the merge must not hide one that did change.

    Committed to committed. `setup` leaves `.forkflow.toml` untracked and tells the user to
    edit it, so on the ordinary post-setup path the working tree holds this fork's own gate
    and no commit holds any: reading one side from the tree and the other from a blob made
    that read as "the merge changed it" and never ran the fork's own preflight again."""
    if not merge_sha:
        return False
    before, after = gate_at(ctx, f"{merge_sha}^1"), gate_at(ctx, "HEAD")
    if before is None or after is None or after != before:
        return True
    # what actually runs is the working tree's `gate`. Untracked, it is this fork's own and
    # no merge can have written it; tracked, a clean tree makes it `after` - anything else is
    # an uncommitted edit, and unreviewed shell is shown rather than run either way
    return config_text(ctx, "HEAD") is not None and gate_commands(ctx) != before


def run_check(ctx: Ctx, touched: Optional[Sequence[str]] = None,
              config_merged: bool = False) -> int:
    """The preflight `sync` and `ship` run, and what `status` surfaces for humans.

    Read-only. 0 when every invariant holds, 3 when one does not; the upstream-tracked
    warning is advisory and never changes the code. `config_merged` says the `gate` this
    ran with is one the merge this run just made changed - it is then upstream's shell
    command, not the one this branch started with, and it is shown rather than run.
    `ctx.cfg` must already be the config of the merged tree: see `finish_sync`."""
    failures = []
    warn_upstream_tracked(ctx, touched)

    gate = gate_commands(ctx)
    if gate and upstream_tracked(ctx, [CONFIG_FILE]):
        # not a failure: a fork whose upstream uses forkflow too inherits the file honestly.
        # It is said out loud because `gate` is the one key that is run rather than read
        step("gate", "-", f"WARNING: `{CONFIG_FILE}` is tracked by `{ctx.up()}` too - a sync "
                          f"can change what these commands are")
    if gate and config_merged:
        step("gate", "-", f"NOT RUN: the merge this run made changed the `gate` "
                          f"in `{CONFIG_FILE}`")
        for cmd in gate:
            print(f"      would have run: sh -c '{cmd}'")
        print(f"      a `gate` is arbitrary shell run by every `check`, `sync` and `ship`: "
              f"read it, and once it is what you want: forkflow check")
        gate = []
    elif not gate:
        step("gate", "-", f"none configured ({CONFIG_FILE} gate = [...])")
    for cmd in gate:
        shown = f"sh -c '{cmd}'"
        if ctx.dry_run:                       # a gate is arbitrary shell: never run in a dry run
            step("gate", shown, "not run (dry run)", dry=True)
            continue
        rc, out = shell(cmd, ctx.root)
        if rc == 0:
            step("gate", shown, "passed")
            continue
        step("gate", shown, f"FAILED (exit {rc})")
        for line in tail_lines(out, TAIL_LINES):
            print(f"      {line}")
        failures.append(f"gate `{cmd}` exited {rc}")
        break                                 # the first failure is the one to fix

    trunk_ref = f"{ctx.origin}/{ctx.trunk}"
    tip_cmd = f"git merge-base --is-ancestor {sh_arg(trunk_ref)} HEAD"
    if current_branch(ctx) == ctx.trunk:
        step("tip", tip_cmd, f"skipped (on the trunk `{ctx.trunk}`)")
    else:
        rc, _, err = git_rc("merge-base", "--is-ancestor", trunk_ref, "HEAD", cwd=ctx.root)
        if rc == 0:
            step("tip", tip_cmd, f"on {trunk_ref}'s tip")
        elif rc == 1:
            ahead, behind = ahead_behind(ctx, "HEAD", trunk_ref)
            where = f"behind by {behind}, ahead by {ahead}"
            step("tip", tip_cmd, f"not on {trunk_ref}'s tip ({where})")
            failures.append(f"not on {trunk_ref}'s tip ({where})")
        else:
            raise Fail(f"cannot compare HEAD with {trunk_ref}: {err.strip()}")

    if failures:
        print("  check    FAILED: " + "; ".join(failures))
        return 3
    print("  check    ok")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=True)
    header(ctx, "check")
    return run_check(ctx)


def sync_branch(ctx: Ctx) -> str:
    """`sync/<upstream remote>-<UTC YYYYMMDD>` - a non-standard remote name shows in the name."""
    return f"{ctx.sync_prefix}{ctx.upstream}-{utc_stamp()}"


def fetch_both(ctx: Ctx) -> None:
    """Refresh both remotes and report what moved; every later step reads these refs."""
    rc, cmd, result, err = fetch(ctx, (ctx.origin, ctx.upstream),
                                 [ctx.up(), f"{ctx.origin}/{ctx.trunk}",
                                  f"{ctx.origin}/{ctx.mirror}"])
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    step("fetch", cmd, result)


def blob(ctx: Ctx, ref: str, path: str) -> Optional[str]:
    """Content of `<ref>:<path>`, None when that tree does not carry the path."""
    rc, out, _ = git_rc("show", f"{ref}:{path}", cwd=ctx.root)
    return None if rc != 0 else out


def line_counts(text: str) -> dict:
    """How often each non-blank stripped line occurs.

    Removals are judged by count, not by presence: a `}` that one side deleted has not
    survived just because the file still has other closing braces."""
    seen = {}
    for line in text.splitlines():
        s = line.strip()
        if s:
            seen[s] = seen.get(s, 0) + 1
    return seen


def side_lines(ctx: Ctx, mb: str, side: str, path: str) -> Tuple[list, list]:
    """(added, removed) non-blank stripped lines of `git diff <mb> <side> -- <path>`.

    Only what is inside a hunk counts, and the preamble is recognised by position, not by
    shape: a content line is `+`/`-` plus the line itself, so a real addition of `++x` reads
    as `+++x` and skipping every `+++`/`---` would drop it - and dropping the very lines a
    side changed is how a side falsely looks like it survived the merge."""
    rc, out, _ = git_rc("diff", "--no-color", "--no-ext-diff", "--no-renames",
                        mb, side, "--", path, cwd=ctx.root)
    added, removed, in_hunk = [], [], False
    for line in out.splitlines():
        if line.startswith("diff --git "):     # the next file's preamble starts here
            in_hunk = False
            continue
        if line.startswith("@@"):              # no content line can: it would be ` @@`/`+@@`
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            bucket = added
        elif line.startswith("-"):
            bucket = removed
        else:
            continue                           # context, or `\ No newline at end of file`
        text = line[1:].strip()
        if text:
            bucket.append(text)
    return added, removed


def side_survived(added: Sequence[str], removed: Sequence[str],
                  base: dict, merged: dict) -> Tuple[int, int]:
    """(checks met, checks made) for one side: its additions are there, its deletions are gone."""
    met = 0
    for line in added:
        if merged.get(line, 0) > 0:
            met += 1
    for line in removed:
        if merged.get(line, 0) < base.get(line, 0):
            met += 1
    return met, len(added) + len(removed)


def both_sides_row(ctx: Ctx, mb: str, ours: str, theirs: str, path: str, renamed: set) -> str:
    """One table row: what happened to each side's lines in the merged file."""
    if path in renamed:
        return "CHECK renamed on one side - compare the old and the new path yourself"
    merged_text = blob(ctx, "HEAD", path)
    if merged_text is None:
        return "CHECK deleted in the merge result - confirm both sides meant that"
    base_text = blob(ctx, mb, path) or ""
    if "\0" in merged_text[:BOTH_SIDES_SNIFF] or "\0" in base_text[:BOTH_SIDES_SNIFF]:
        return "CHECK binary - compare it with a tool that understands the format"
    merged, base = line_counts(merged_text), line_counts(base_text)
    parts, missing = [], []
    for label, side in (("ours", ours), ("theirs", theirs)):
        added, removed = side_lines(ctx, mb, side, path)
        met, made = side_survived(added, removed, base, merged)
        parts.append(f"{label} {met}/{made}")
        if met < made:
            missing.append(f"{made - met} of {label}")
    row = "  ".join(parts)
    if missing:
        row += "  CHECK " + " and ".join(missing) + " did not survive"
    return row


def both_sides_survived(ctx: Ctx, ours: str, theirs: str) -> list:
    """Files changed on both sides of the merge that was actually made.

    A clean merge is not automatically a correct one: the file that motivated this check
    merged clean and still had to be looked at. Advisory - the exit code never changes.
    Returns [(path, row)] for the merge-request body."""
    mb = git("merge-base", ours, theirs, cwd=ctx.root, check=False)
    cmd = f"git diff --name-only {short(mb)} {ours} / {theirs}"
    if not mb:
        step("verify", cmd, f"no merge base between {ours} and {theirs}")
        return []
    changed, renamed = [], set()
    for side in (ours, theirs):
        changed.append(set(diff_names(ctx, "--no-renames", mb, side)))
        for code, path, other in name_status(ctx, "--find-renames", mb, side):
            if code[0] in ("R", "C"):
                renamed.update(f for f in (path, other) if f)
    files = sorted(changed[0] & changed[1])
    if not files:
        step("verify", cmd, "no file was changed on both sides")
        return []
    step("verify", cmd, f"{len(files)} file(s) changed on both sides")
    width = min(max(len(f) for f in files), BOTH_SIDES_WIDTH)
    rows = []
    for f in files:
        row = both_sides_row(ctx, mb, ours, theirs, f, renamed)
        print(f"    {f.ljust(width)}  {row}")
        rows.append((f, row))
    flagged = [f for f, row in rows if "CHECK" in row]
    if flagged:
        print(f"    CHECK: look at {', '.join(flagged)} yourself - "
              f"a clean merge is not automatically a correct one")
    return rows


def sync_branch_is_free(ctx: Ctx, name: str, force: bool) -> None:
    """Refuse a sync branch that already exists, locally or on origin. Checked before the
    backup as well as inside `make_sync_branch`, so a rerun on the same day leaves no orphan
    backup on origin.

    origin is asked with `ls-remote`, as `free_sync_name` does and for the same reason: this
    runs before the fetch, and another clone's sync counts too. A branch that is only on
    origin gets the `--force` answer, not the `--continue` one - there is no local branch to
    resume, so `--continue` cannot get past the push that would be rejected at the end."""
    if force:
        return
    if has_ref(ctx.root, f"refs/heads/{name}"):
        raise Fail(f"branch `{name}` already exists: resume it with `forkflow sync --continue`, "
                   f"or recreate it from {ctx.origin}/{ctx.trunk} with `forkflow sync --force`")
    rc, out, _ = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{name}", cwd=ctx.root)
    if rc == 0 and f"refs/heads/{name}" in out:
        raise Fail(f"`{name}` is already on {ctx.origin} and this clone has no local copy: a "
                   f"published sync branch is never rebased or force-pushed (rule 5). Close "
                   f"its merge request and rerun with `forkflow sync --force`, which publishes "
                   f"the next free `{name}-N`")


def free_sync_name(ctx: Ctx, name: str) -> str:
    """The name a `--force` rerun publishes under: `<name>`, or the first free `<name>-N`.

    `--force` redoes a sync whose MR went stale, and the name is dated, so a same-day rerun
    would push at a branch that is already on origin - which a sync branch cannot be: its tip
    is a merge commit off the *old* trunk, so the push is not a fast-forward, and rule 5 rules
    out rewriting it (a sync MR is never rebased and never force-pushed; the stale one is
    closed and a new one opened). Asked of the remote, not of the remote-tracking refs: this
    runs before the fetch, and another clone's sync counts too."""
    rc, out, _ = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{name}",
                        f"refs/heads/{name}-*", cwd=ctx.root)
    if rc != 0:                        # cannot ask: the push itself stays the answer
        return name
    taken = {line.split("\t")[-1].strip() for line in out.splitlines() if "\t" in line}
    if f"refs/heads/{name}" not in taken:
        return name
    for n in range(2, MAX_SYNC_RERUNS):
        candidate = f"{name}-{n}"
        if (f"refs/heads/{candidate}" not in taken
                and not has_ref(ctx.root, f"refs/heads/{candidate}")):
            step("branch", f"git ls-remote --heads {sh_arg(ctx.origin)} "
                          f"{sh_arg(f'refs/heads/{name}')}",
                 f"`{name}` is already published: this sync becomes `{candidate}`")
            print(f"    close the merge request of `{name}`: it is the stale one this rerun "
                  f"replaces (a sync MR is never rebased or force-pushed)")
            return candidate
    raise Fail(f"`{name}` and every `{name}-N` up to {MAX_SYNC_RERUNS - 1} are already on "
               f"{ctx.origin}: delete the stale sync branches there first")


def make_sync_branch(ctx: Ctx, name: str, force: bool) -> None:
    """Create the sync branch off `origin/<trunk>` and switch to it. Never off the local trunk:
    the MR has to apply to what is published."""
    base = f"{ctx.origin}/{ctx.trunk}"
    cmd = f"git checkout --no-track -b {sh_arg(name)} {sh_arg(base)}"
    sync_branch_is_free(ctx, name, force)
    exists = has_ref(ctx.root, f"refs/heads/{name}")
    if ctx.dry_run:
        step("branch", cmd, f"would {'recreate' if exists else 'create'} it off {base}", dry=True)
        return
    if exists:
        if current_branch(ctx) == name:              # git refuses to delete the checked-out branch
            git("checkout", "--detach", base, cwd=ctx.root)
        git("branch", "-D", name, cwd=ctx.root)
    git("checkout", "--no-track", "-b", name, base, cwd=ctx.root)
    step("branch", cmd, f"{'recreated' if exists else 'created'} at {short(rev(ctx.root, 'HEAD'))}")


def untracked_in_the_way(ctx: Ctx, target: str) -> list:
    """Untracked working-tree files the sync merge would have to write over.

    git refuses such a merge outright - "untracked working tree files would be overwritten",
    whatever the file holds - and `setup` leaves exactly one behind: the commented
    `.forkflow.toml` template, which a forkflow-using upstream may later start tracking.
    Answered before the backup is pushed, so a sync that cannot run leaves no orphan
    `backup/*` on origin.

    Only files the merge really writes count: one that was at the merge base and this fork
    deleted stays deleted, so it is not in the way.

    `--no-renames` because git detects renames by default (2.9+) and reports the new path as
    `R`, not `A`: an upstream commit that moves a file onto a path this fork holds untracked
    is exactly the collision this answers, and rename detection hid it."""
    base = f"{ctx.origin}/{ctx.trunk}"
    added = set(diff_names(ctx, "--no-renames", "--diff-filter=A", base, target))
    merge_base = git("merge-base", base, target, cwd=ctx.root, check=False)
    if merge_base:
        added &= set(diff_names(ctx, "--no-renames", "--diff-filter=A", merge_base, target))
    if not added:
        return []
    others = git("ls-files", "--others", "--exclude-standard", "-z", cwd=ctx.root, check=False)
    return sorted(added & {f for f in others.split("\0") if f})


def merge_upstream(ctx: Ctx, name: str, target: str, commits: Sequence[str]) -> None:
    """One `--no-ff` merge commit, so `git log --merges <trunk>` is the record of every sync."""
    text = (f"Merge {ctx.mirror} (mirror of {ctx.up()}) into {name}\n\n"
            + "\n".join(commits))
    cmd = f"git merge --no-ff -F <message> {short(target)}"
    if ctx.dry_run:
        step("merge", cmd, f"would merge {short(target)} into {name} as one merge commit", dry=True)
        return
    path = write_temp(text, "merge-message")
    try:
        rc, out, err = git_rc("merge", "--no-ff", "-F", path, target, cwd=ctx.root)
    finally:
        os.unlink(path)
    if rc == 0:
        step("merge", cmd, f"merge commit {short(rev(ctx.root, 'HEAD'))}")
        return
    unmerged = unmerged_paths(ctx)
    if not unmerged:
        text = (err or out).strip()
        if "untracked working tree files would be overwritten" in text:
            # the preflight in `cmd_sync` answers this before the backup; this is what is
            # left if the tree changed under the run. `{name}` exists by now, so plain
            # `forkflow sync` would refuse it and send the user to `--continue`, which has no
            # merge to resume: `--force` is the only rerun that is not a closed loop
            raise Fail(f"the merge would overwrite untracked file(s) in the working tree, "
                       f"which git refuses outright - remove them, or get them into "
                       f"`{ctx.origin}/{ctx.trunk}` first, then rerun the sync with "
                       f"`forkflow sync --force` (`{name}` was already created, and only "
                       f"`--force` recreates it):\n{text}")
        raise Fail(f"merge of {short(target)} failed:\n{text}")
    report_paths("merge", cmd, unmerged, "conflicting file(s)")
    raise Fail(f"resolve the conflicts on `{name}`, `git add` them, "
               f"then run `forkflow sync --continue`", 4)


def sync_body(ctx: Ctx, commits: Sequence[str], both: Sequence[Tuple[str, str]],
              mirror_move: Tuple[str, str], backup_ref: str) -> str:
    """What the reviewer of a sync MR needs: what came in, what was touched on both sides,
    where the mirror stands, how to get back, and which button to press."""
    old, new = mirror_move
    lines = [f"Merge `{ctx.mirror}` (mirror of `{ctx.up()}`) into `{ctx.trunk}`.", "",
             f"Upstream commits taken ({len(commits)}):", ""]
    lines += [f"- {c}" for c in commits] or ["- (none)"]
    if both:
        lines += ["", f"Files changed on both sides ({len(both)}):", ""]
        lines += [f"- `{f}` - {row}" for f, row in both]
    else:
        lines += ["", "Files changed on both sides: none"]
    lines += [""]
    if old and new and old != new:
        lines.append(f"Mirror `{ctx.mirror}`: {short(old)} -> {short(new)} "
                     f"(pushed to {ctx.origin})")
    else:
        lines.append(f"Mirror `{ctx.mirror}`: at {short(new or old)}")
    if backup_ref:
        lines += [f"Backup of `{ctx.origin}/{ctx.trunk}` from before this sync: `{backup_ref}`",
                  f"Rollback: `git reset --hard {sh_arg(f'{ctx.origin}/{backup_ref}')}`"]
    lines += ["", f"Merge button: {merge_button(ctx, 'sync')}."]
    return "\n".join(lines)


def merge_in_progress(ctx: Ctx) -> bool:
    """True while a merge is resolved but not committed (`MERGE_HEAD` still there)."""
    path = git_path(ctx.root, "MERGE_HEAD")
    return bool(path) and os.path.exists(path)


def unmerged_paths(ctx: Ctx) -> list:
    """Paths still carrying conflict markers."""
    return diff_names(ctx, "--diff-filter=U")


def check_failure_hint(ctx: Ctx, name: str) -> str:
    """What to do about a failed `check` on a sync branch - which is not the same answer
    for the two invariants it checks."""
    rc, _, _ = git_rc("merge-base", "--is-ancestor", f"{ctx.origin}/{ctx.trunk}", "HEAD",
                      cwd=ctx.root)
    if rc != 0:
        return (f"  `{ctx.origin}/{ctx.trunk}` moved on: this sync has to be redone against "
                f"the new tip - `forkflow sync --force` recreates `{name}` (a sync MR is "
                f"never rebased)")
    return ("  fix that on this branch and commit it, then: forkflow sync --continue")


def finish_sync(ctx: Ctx, args: argparse.Namespace, name: str, commits: Sequence[str],
                rows: Sequence[Tuple[str, str]], mirror_move: Tuple[str, str],
                backup_ref: str, merge_sha: str) -> int:
    """check -> push -> merge request: the tail both `sync` and `sync --continue` run."""
    # `resolve_ctx` read `.forkflow.toml` before the merge this run just made, so on the
    # clean-merge path `ctx.cfg` is the fork's pre-merge config while the tree `check` runs
    # against is the merged one. Only `cfg` is refreshed: the branch names this run is
    # already using must not change under it half way through. A merged config that cannot
    # be read is exit 2, as it is for every other subcommand - the clone is in that state now.
    # It is raised with what this run already did and the way out: the merge commit, the sync
    # branch, the mirror push and the backup are all made by now, and a bare parse error left
    # the user with a half-done sync and nothing said about either
    if not ctx.dry_run:
        try:
            ctx = replace(ctx, cfg=load_config(ctx.root))
        except Fail as exc:
            raise Fail(f"the merge brought a `{CONFIG_FILE}` that cannot be read: {exc}\n"
                       f"  the merge commit and `{name}` are made and the backup is on "
                       f"origin; this run checked nothing and opened no merge request\n"
                       f"  fix `{CONFIG_FILE}` on `{name}` and commit it, then: "
                       f"forkflow sync --continue", 2)

    # `.forkflow.toml` names the branches every safety check depends on and holds `gate`,
    # which is run with `sh -c`: a sync that brings it in from the original project is a
    # change to how this fork is governed, and the reviewer of the MR has to see that
    config_merged = not ctx.dry_run and config_changed_in_merge(ctx, merge_sha)
    if config_merged:
        print(f"  CHECK    this sync changes `{CONFIG_FILE}` - it names the branches and "
              f"holds `gate`, which forkflow runs with `sh -c`:")
        print(f"    git diff {short(merge_sha)}^1 HEAD -- {sh_arg(CONFIG_FILE)}")
    gate_merged = not ctx.dry_run and gate_arrived_in_merge(ctx, merge_sha)

    if ctx.dry_run:
        step("check", "forkflow check", "not run (dry run)", dry=True)
    elif run_check(ctx, config_merged=gate_merged) == 3:
        print(check_failure_hint(ctx, name))
        return 3

    base = rev(ctx.root, f"{ctx.origin}/{ctx.trunk}")   # what the merge was built on
    try:
        push(ctx, name)
    except Fail as exc:
        if exc.code == 5:
            print("  fix that, then: forkflow sync --continue")
        raise
    record_pending(ctx, "sync", name, base)            # landable even if the MR step fails

    title = (getattr(args, "title", None)
             or f"sync: {ctx.up()} {utc_stamp()} ({len(commits)} commits)")
    _, url = open_mr(ctx, name, title, sync_body(ctx, commits, rows, mirror_move, backup_ref),
                     bool(getattr(args, "mr", False)))
    if url:
        record_pending(ctx, "sync", name, base, url)
    write_state(ctx, "sync", None)                     # this sync is done: nothing to resume
    if getattr(args, "merge", False):
        merge_mr(ctx, "sync", name, url)               # exit 6 leaves the record above
        land_after_merge(ctx)
        return 0
    print("  after the MR is merged, next: forkflow land")
    return 0


def land_after_merge(ctx: Ctx) -> None:
    """`--merge`'s closing step: the request is merged, so `land` runs in this process.

    A catch-up that cannot run is its own exit 2, but the message has to open with what
    did happen - the merge request *is* merged - so nobody reads a non-zero exit as
    "nothing happened": the branch is on the trunk, and `forkflow land` finishes the rest
    once the reason is dealt with. A dry run wrote no record and `merge_mr` has already
    shown `would: land`, so there is nothing to run here."""
    if ctx.dry_run:
        return
    try:
        land_pending(ctx)
    except Fail as exc:
        raise Fail(f"the merge request was merged; the local catch-up did not run: {exc}",
                   exc.code)


def cmd_sync_continue(ctx: Ctx, args: argparse.Namespace) -> int:
    """Resume the sync the conflicted run left on the sync branch.

    Verification uses the parents of the merge that was actually made, so a mirror or a
    trunk that moved in the meantime cannot change what is checked."""
    name = current_branch(ctx)
    where = f"on `{name}`" if name else "a detached HEAD"
    if not name or not name.startswith(ctx.sync_prefix):
        raise Fail(f"`--continue` resumes a sync: switch to the `{ctx.sync_prefix}...` branch "
                   f"the conflicted run left you on (HEAD is {where})")

    unmerged = unmerged_paths(ctx)
    if unmerged:
        report_paths("continue", "git diff --name-only --diff-filter=U", unmerged,
                     "file(s) still unmerged")
        raise Fail("resolve the conflicts and `git add` them, "
                   "then run `forkflow sync --continue` again")

    merging = merge_in_progress(ctx)
    # the merge does not have to be at HEAD: fixing what `check` refused means a commit on
    # top of it, and that must not turn `--continue` into "nothing to continue". The sync
    # merge is the *first* commit on the branch, so it is the last line here - `-n 1` took
    # the newest instead, and a `git merge` the user made on the branch afterwards then stood
    # in for it, which handed upstream's unread `gate` to `run_check` as this fork's own.
    # `--first-parent` keeps merges carried in on the second-parent side of such a merge out
    merges = git("rev-list", "--merges", "--first-parent", "HEAD", "--not",
                 f"{ctx.origin}/{ctx.trunk}", cwd=ctx.root, check=False).split()
    merge_sha = merges[-1] if merges else ""
    if not merging and not merge_sha:
        raise Fail(f"nothing to continue: `{name}` carries no sync merge - "
                   f"run `forkflow sync`")
    if merging:
        cmd = "git commit --no-edit"
        if ctx.dry_run:
            step("continue", cmd, "would commit the resolved merge", dry=True)
        else:
            rc, out, err = git_rc("commit", "--no-edit", cwd=ctx.root)
            if rc != 0:
                raise Fail(f"cannot commit the resolved merge:\n{(err or out).strip()}")
            merge_sha = rev(ctx.root, "HEAD")
            step("continue", cmd, f"merge commit {short(merge_sha)}")
    elif rev(ctx.root, "HEAD") == merge_sha:
        step("continue", "git rev-parse MERGE_HEAD", "the merge is already committed")
    else:
        step("continue", "git rev-list --merges --first-parent HEAD",
             f"resuming from the merge commit {short(merge_sha)}")

    if merge_sha:
        ours, theirs = f"{merge_sha}^1", f"{merge_sha}^2"
        log = git("log", "--oneline", "--no-decorate", f"{ours}..{theirs}",
                  cwd=ctx.root, check=False)
        commits = [ln for ln in log.splitlines() if ln]
        rows = both_sides_survived(ctx, ours, theirs)
    else:                                   # dry run over an uncommitted merge
        step("verify", "git diff HEAD^1 / HEAD^2",
             "not run (dry run: the merge is not committed)", dry=True)
        commits, rows = [], []

    mirror_sha = rev(ctx.root, f"refs/heads/{ctx.mirror}")
    return finish_sync(ctx, args, name, commits, rows, (mirror_sha, mirror_sha),
                       resumable(ctx, "sync", name).get("backup", ""), merge_sha)


def merge_gate(ctx: Ctx, args: argparse.Namespace) -> None:
    """`--merge` refused, or nothing - before the fetch, the backup and any push.

    Config AND flag: the fork declares once, in `.forkflow.toml`, that its merge requests are
    merged by whoever opened them (`merge = "self"`), and the flag asks for it per run. Either
    alone does nothing, so a reviewed fork can never be merged by accident. The second check
    is knowable now too: a merge command addresses the fork by URL (`mr_target`), and an
    origin that names no project would otherwise be a push followed by a failure."""
    if not getattr(args, "merge", False):
        return
    if ctx.merge != "self":
        raise Fail(f"--merge needs `merge = \"self\"` in {CONFIG_FILE}: this fork's merge "
                   f"requests are merged by hand")
    target, reason = mr_target(ctx)
    if not target:
        raise Fail(f"--merge: `{ctx.origin_url or '-'}` names no project to merge on "
                   f"({reason})")


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=True)
    header(ctx, "sync")
    merge_gate(ctx, args)             # before `--continue`: a conflicted sync resumes with it
    if getattr(args, "cont", False):
        return cmd_sync_continue(ctx, args)

    branch = current_branch(ctx)
    if not branch:
        raise Fail("HEAD is detached: switch to a branch before syncing")
    if not clean_tree(ctx):
        raise Fail("the working tree has uncommitted changes: commit or stash them first")
    name = sync_branch(ctx)
    force = bool(getattr(args, "force", False))
    sync_branch_is_free(ctx, name, force)     # before the backup: no orphan backup on a rerun
    if force:
        name = free_sync_name(ctx, name)      # a published sync branch is never pushed over
    if getattr(args, "merge", False):
        print(f"  leaving `{branch}`, switching to `{name}` "
              f"(you stay on it when this finishes unless --merge lands it - then you "
              f"are on `{ctx.trunk}`)")
    else:
        print(f"  leaving `{branch}`, switching to `{name}` "
              f"(you stay on it when this finishes; the trunk is never touched)")

    fetch_both(ctx)
    target = rev(ctx.root, ctx.up())
    if not target:
        raise Fail(f"`{ctx.up()}` does not resolve after the fetch: is `{ctx.upstream}` right?")
    step("target", f"git rev-parse {sh_arg(ctx.up())}", short(target))

    # the mirror first: it rewrites nothing and is useful on its own, and a failure here
    # leaves the trunk untouched. Everything after this reads `target`, not the mirror branch,
    # so a dry run previews the pending merge even though it moves nothing.
    mirror_move = advance_mirror(ctx, target)
    push_mirror(ctx, target)

    trunk_ref = f"refs/remotes/{ctx.origin}/{ctx.trunk}"
    rc, _, _ = git_rc("merge-base", "--is-ancestor", target, trunk_ref, cwd=ctx.root)
    if rc == 0:
        print(f"  already in sync: {ctx.origin}/{ctx.trunk} already contains {short(target)} "
              f"(the mirror was advanced and pushed above if it was behind)")
        return 0

    simulate_merge(ctx, target)

    # before the backup: git refuses a merge that would write over an untracked file, and a
    # sync that cannot run must not leave an orphan `backup/*` behind on origin
    blocked = untracked_in_the_way(ctx, target)
    if blocked:
        report_paths("untracked", "git ls-files --others --exclude-standard", blocked,
                     "untracked file(s) this sync would write over")
        raise Fail(f"git refuses a merge that would overwrite an untracked file. "
                   f"`forkflow setup` writes `{CONFIG_FILE}` untracked and an upstream that "
                   f"uses forkflow too tracks it, which is this collision. Remove the "
                   f"file(s), or get them into `{ctx.origin}/{ctx.trunk}` first (commit them "
                   f"on a branch and `forkflow ship` it), then run `forkflow sync` again")

    log = git("log", "--oneline", "--no-decorate", f"{ctx.origin}/{ctx.trunk}..{target}",
              cwd=ctx.root, check=False)
    commits = [ln for ln in log.splitlines() if ln]
    step("commits", f"git log --oneline {sh_arg(f'{ctx.origin}/{ctx.trunk}..{short(target)}')}",
         f"{len(commits)} upstream commit(s) to take")
    for line in commits:
        print(f"    {line}")

    backup_ref = backup(ctx, "pre-sync", f"{ctx.origin}/{ctx.trunk}")
    make_sync_branch(ctx, name, force=force)
    write_state(ctx, "sync", {"branch": name, "backup": backup_ref})
    merge_upstream(ctx, name, target, commits)

    if ctx.dry_run:
        step("verify", "git diff HEAD^1 / HEAD^2", "not run (dry run)", dry=True)
        rows, merge_sha = [], ""
    else:
        rows = both_sides_survived(ctx, "HEAD^1", "HEAD^2")
        merge_sha = rev(ctx.root, "HEAD")
    return finish_sync(ctx, args, name, commits, rows, mirror_move, backup_ref, merge_sha)


def rebase_in_progress(ctx: Ctx) -> bool:
    """True while `git rebase` is stopped on a conflict or an edit."""
    for name in ("rebase-merge", "rebase-apply"):
        path = git_path(ctx.root, name)
        if path and os.path.exists(path):
            return True
    return False


def rebasing_branch(ctx: Ctx) -> str:
    """The branch a stopped rebase is rewriting, or "" - HEAD is detached while it runs."""
    for name in ("rebase-merge", "rebase-apply"):
        path = git_path(ctx.root, os.path.join(name, "head-name"))
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path) as fh:
                head = fh.read().strip()
        except OSError:
            return ""
        prefix = "refs/heads/"
        return head[len(prefix):] if head.startswith(prefix) else ""
    return ""


def ship_preflight(ctx: Ctx) -> str:
    """The branch `ship` may rewrite, or Fail(2). Everything else is refused by name:
    ship rebases and squashes what it is on, and only a feature branch may be rewritten."""
    if rebase_in_progress(ctx):
        # `--continue` only resumes a ship *this clone* started: naming it after a rebase
        # that is not one (a `git pull --rebase` this run's own advice asked for, say) sends
        # the user to a command that refuses them
        being = rebasing_branch(ctx)
        resume = ("forkflow ship --continue" if being and resumable(ctx, "ship", being)
                  else "forkflow ship")
        raise Fail(f"a rebase is in progress: finish it with `git rebase --continue` and then "
                   f"`{resume}`, or start over with `git rebase --abort`")
    branch = current_branch(ctx)
    if not branch:
        raise Fail("HEAD is detached: switch to the feature branch you want to ship")
    if branch == ctx.trunk:
        raise Fail(f"`{branch}` is the trunk: it is never rebased and never pushed - "
                   f"switch to the feature branch you want to ship")
    if branch == ctx.mirror:
        raise Fail(f"`{branch}` is the mirror: it only ever copies `{ctx.up()}` - "
                   f"switch to the feature branch you want to ship")
    if branch.startswith(ctx.sync_prefix):
        raise Fail(f"`{branch}` is a sync branch: finish it with `forkflow sync --continue`; "
                   f"a sync is merged, never squashed")
    if branch.startswith(ctx.backup_prefix):
        raise Fail(f"`{branch}` is a backup branch: it is a restore point, not a feature branch")
    if not valid_branch_name(branch):
        # refused here rather than after the squash: `push()` would refuse it at the end
        raise Fail(f"`{branch}` is a name forkflow will not push: git's refspec grammar does "
                   f"not read it as one branch (a leading `+` means force, a leading `-` an "
                   f"option) - rename it with `git branch -m <name>`")
    if not clean_tree(ctx):
        raise Fail("the working tree has uncommitted changes: commit or stash them first")
    return branch


def rebase_onto(ctx: Ctx, branch: str, trunk_ref: str) -> None:
    """Rebase locally so the trunk can fast-forward: `rebase locally, merge globally`."""
    cmd = f"git rebase {sh_arg(trunk_ref)}"
    tip = rev(ctx.root, trunk_ref)
    if ctx.dry_run:
        step("rebase", cmd, f"would replay `{branch}` onto {short(tip)}", dry=True)
        return
    rc, out, err = git_rc("rebase", trunk_ref, cwd=ctx.root)
    if rc == 0:
        step("rebase", cmd, f"`{branch}` now sits on {short(tip)}")
        return
    if not rebase_in_progress(ctx):
        raise Fail(f"rebase of `{branch}` onto {trunk_ref} failed:\n{(err or out).strip()}")
    unmerged = unmerged_paths(ctx)
    report_paths("rebase", cmd, unmerged, "conflicting file(s)")
    raise Fail(f"resolve the conflicts, `git add` them, run `git rebase --continue`, "
               f"then `forkflow ship --continue`", 4)


def commit_records(ctx: Ctx, base: str) -> list:
    """(sha, subject, body) of `base..HEAD`, oldest first - what the squash message is built of.

    Read through git_rc: the separators are ASCII whitespace to Python, so `git()`'s strip()
    would eat the last record's field separator and lose that commit."""
    rc, out, _ = git_rc("log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e",
                        f"{base}..HEAD", cwd=ctx.root)
    if rc != 0:
        return []
    records = []
    for chunk in out.split("\x1e"):
        parts = chunk.split("\x1f")
        if len(parts) < 2 or not parts[0].strip():
            continue
        body = parts[2] if len(parts) > 2 else ""
        records.append((parts[0].strip(), parts[1].strip(), body.strip("\n")))
    return records


def squash_message(records: Sequence[Tuple[str, str, str]],
                   message_file: Optional[str] = None) -> str:
    """--message-file verbatim; a single commit keeps its own message; otherwise the oldest
    subject with every squashed commit listed under it, oldest first."""
    if message_file:
        try:
            with open(message_file, "r") as fh:
                text = fh.read()
        except OSError as exc:
            raise Fail(f"cannot read `{message_file}`: {exc}")
        if not text.strip():
            raise Fail(f"`{message_file}` is empty: the squashed commit needs a message")
        return text if text.endswith("\n") else text + "\n"
    if not records:
        raise Fail("nothing to squash: no commits beyond the trunk")
    if len(records) == 1:
        _, subject, body = records[0]
        return subject + (f"\n\n{body}" if body else "") + "\n"
    lines = [records[0][1], "",
             f"Squashed from {len(records)} commits (oldest first):", ""]
    for _, subject, body in records:
        lines.append(f"- {subject}")
        for line in body.splitlines():
            lines.append(f"  {line}" if line.strip() else "")
    return "\n".join(lines).rstrip() + "\n"


def squash(ctx: Ctx, base: str, message: str) -> str:
    """One commit on `base` carrying exactly the tree that was there before.

    The tree hash is the proof: `reset --soft` keeps the index, so a differing tree means
    something was lost and nothing may be pushed."""
    cmd = f"git reset --soft {short(base)} && git commit -F <message>"
    if ctx.dry_run:
        step("squash", cmd, "would replace the branch's commits with one", dry=True)
        return ""
    tree_before = git("rev-parse", "HEAD^{tree}", cwd=ctx.root)
    head_before = rev(ctx.root, "HEAD")
    path = write_temp(message, "commit-message")
    try:
        git("reset", "--soft", base, cwd=ctx.root)
        rc, out, err = git_rc("commit", "--allow-empty", "-F", path, cwd=ctx.root)
    finally:
        os.unlink(path)
    if rc != 0:
        git("reset", "--soft", head_before, cwd=ctx.root)
        step("squash", cmd, "FAILED")
        raise Fail(f"cannot commit the squashed change:\n{(err or out).strip()}", 5)
    tree_after = git("rev-parse", "HEAD^{tree}", cwd=ctx.root)
    if tree_after != tree_before:
        git("reset", "--soft", head_before, cwd=ctx.root, check=False)   # leave no bad commit
        step("squash", cmd, "TREE MISMATCH")
        raise Fail(f"the squashed commit's tree {short(tree_after)} differs from "
                   f"{short(tree_before)}: refusing to push a squash that changed the result", 5)
    head = rev(ctx.root, "HEAD")
    step("squash", cmd, f"one commit {short(head)}, tree {short(tree_after)} unchanged")
    return head


def ship_body(ctx: Ctx, message: str, touched: Sequence[str]) -> str:
    """The squashed commit message, the upstream-tracked warning, and the merge button."""
    lines = [message.rstrip(), ""]
    if touched:
        lines += [f"Upstream-tracked files touched ({len(touched)}) - "
                  f"every one of them is a permanent merge cost:", ""]
        lines += [f"- `{f}`" for f in touched]
        lines += [""]
    lines += [f"Merge button: {merge_button(ctx, 'ship')}."]
    return "\n".join(lines)


def finish_ship(ctx: Ctx, args: argparse.Namespace, branch: str,
                backup_ref: str, lease: str) -> int:
    """squash -> check -> push -> merge request: the tail both `ship` and `ship --continue` run."""
    trunk_ref = f"{ctx.origin}/{ctx.trunk}"
    # here, not in `cmd_ship`: a rebase that dropped every commit (`git rebase --skip`) lands on
    # the trunk's tip too, and `--continue` resumes straight into this function
    if not ctx.dry_run and rev(ctx.root, "HEAD") == rev(ctx.root, trunk_ref):
        print(f"  nothing to ship: every commit of `{branch}` is already on {trunk_ref} "
              f"(the backup `{backup_ref}` still holds the branch as it was)")
        return 0
    mb = git("merge-base", trunk_ref, "HEAD", cwd=ctx.root)
    records = commit_records(ctx, mb)
    step("commits", f"git log --oneline {sh_arg(f'{trunk_ref}..HEAD')}",
         f"{len(records)} commit(s) to squash into one")
    for sha, subject, _ in records:
        print(f"    {short(sha)} {subject}")

    message = squash_message(records, getattr(args, "message_file", None))
    rollback = (f"  rollback: git reset --hard {sh_arg(f'{ctx.origin}/{backup_ref}')}"
                if backup_ref else "")
    touched = upstream_tracked(ctx, branch_files(ctx))
    try:
        squash(ctx, mb, message)
    except Fail as exc:
        if exc.code == 5 and rollback:
            print(rollback)
        raise

    if ctx.dry_run:
        warn_upstream_tracked(ctx, touched)      # the real run prints it after the squash
        step("check", "forkflow check", "not run (dry run)", dry=True)
    elif run_check(ctx, touched) == 3:
        if rollback:
            print(rollback)
        # the squash has already happened, so a plain rerun of `ship` starts from a branch whose
        # commits are no longer the published ones; `--continue` resumes this run instead
        print(f"  fix that on `{branch}` and commit it, then: forkflow ship --continue")
        return 3

    base = rev(ctx.root, trunk_ref)                    # what the squash was built on
    try:
        push(ctx, branch, lease=lease or None, backup_ref=backup_ref)
    except Fail as exc:
        if exc.code == 5 and rollback:
            print(rollback)
        raise
    record_pending(ctx, "ship", branch, base)          # landable even if the MR step fails

    title = (getattr(args, "title", None)
             or (message.strip().splitlines() or [f"ship {branch}"])[0])
    _, url = open_mr(ctx, branch, title, ship_body(ctx, message, touched),
                     bool(getattr(args, "mr", False)))
    if url:
        record_pending(ctx, "ship", branch, base, url)
    write_state(ctx, "ship", None)                     # this ship is done: nothing to resume
    if getattr(args, "merge", False):
        merge_mr(ctx, "ship", branch, url)             # exit 6 leaves the record above
        land_after_merge(ctx)
        return 0
    print("  after the MR is merged, next: forkflow land")
    return 0


PUSH_REFLOG = "update by push"       # git's own message when *this* clone's push moved a
                                     # remote-tracking ref; a fetch, a pull or a checkout is
                                     # logged under the command that brought the commit in


def pushed_from_here(ctx: Ctx, branch: str, commit: str) -> bool:
    """True when this clone's own `git push` put `commit` on `origin/<branch>`.

    git keeps a reflog for the remote-tracking ref as well, and that one *can* tell a push
    apart from an arrival: our push writes `update by push`, while a fetch or a pull writes
    the command that fetched. The **branch** reflog cannot tell them apart at all - an
    ordinary `git pull` puts a teammate's commit straight into `refs/heads/<branch>`'s reflog,
    which is why it is no evidence of ownership.

    Reflogs expire and can be switched off, so this is evidence, not a precondition: what it
    cannot answer, the recorded publications and the backups still can."""
    rc, out, _ = git_rc("reflog", "show", "--format=%H %gs",
                        f"refs/remotes/{ctx.origin}/{branch}", cwd=ctx.root)
    if rc != 0:
        return False
    for line in out.splitlines():
        sha, _, message = line.partition(" ")
        if sha == commit and message.strip() == PUSH_REFLOG:
            return True
    return False


def backed_up_on_origin(ctx: Ctx, commit: str) -> bool:
    """True when a `<backup_prefix>` branch that origin still has carries `commit`.

    Asked of the remote, not of the remote-tracking refs: a stale `refs/remotes/<origin>/
    backup/...` is not a restore point. A tip a backup on origin still carries survives being
    replaced, which is exactly what rule 4 asks of a rewrite - so it is also the escape the
    refusal in `cmd_ship` points at."""
    rc, out, _ = git_rc("ls-remote", "--heads", ctx.origin,
                        f"refs/heads/{ctx.backup_prefix}*", cwd=ctx.root)
    if rc != 0:
        return False
    for line in out.splitlines():
        sha = line.split("\t")[0].strip()
        if not sha:
            continue
        rc, _, _ = git_rc("merge-base", "--is-ancestor", commit, sha, cwd=ctx.root)
        if rc == 0:
            return True
    return False


def published_is_ours(ctx: Ctx, branch: str, published: str) -> bool:
    """True when `origin/<branch>`'s tip is one this clone published, or one that is kept.

    `ship` exists to rewrite a feature branch and force-push it behind a lease and a backup
    (rule 4), so `origin/<branch>` not being an ancestor of HEAD is the normal case after an
    amend, a rebase, or a squash this very command made before a gate failure. What must never
    be force-pushed away is somebody *else's* work, and only a record of what this clone did
    tells the two apart:

    - git logged this clone's own push of it on the remote-tracking ref (`pushed_from_here`) -
      the amend and the local-rebase cases, however the branch was first published;
    - `record_published` wrote it down when this clone pushed it, which outlives a reflog that
      has expired or was switched off;
    - the interrupted `ship` this rerun repeats recorded it as the lease its own fetch saw -
      the exit-3 path, where the squash has already happened, so the published tip is no
      longer an ancestor of HEAD and a plain rerun would otherwise dead-end;
    - a backup still carries it, so nothing is lost by replacing it: the one this run
      recorded, or any `<backup_prefix>` branch origin confirms.

    The branch reflog is deliberately *not* evidence: `git pull` and `git checkout` put a
    teammate's commit into it, and "it was once in my reflog" is not "it is mine to destroy"."""
    if pushed_from_here(ctx, branch, published) or published_here(ctx, branch, published):
        return True
    state = resumable(ctx, "ship", branch)
    if state.get("lease") == published:
        return True
    keep = state.get("backup") or ""
    for ref in (f"refs/heads/{keep}", f"refs/remotes/{ctx.origin}/{keep}"):
        if keep and has_ref(ctx.root, ref):
            rc, _, _ = git_rc("merge-base", "--is-ancestor", published, ref, cwd=ctx.root)
            if rc == 0:
                return True
    return backed_up_on_origin(ctx, published)


def cmd_ship(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=True)
    header(ctx, "ship")
    branch = ship_preflight(ctx)
    merge_gate(ctx, args)             # before `--continue`, the fetch, the backup, the push
    trunk_ref = f"{ctx.origin}/{ctx.trunk}"

    if getattr(args, "cont", False):
        # a ship in progress is one this clone started: its backup and the lease its fetch
        # saw are recorded, and without them there is nothing to resume and nothing to
        # force-push behind (rule 4)
        state = resumable(ctx, "ship", branch)
        if not state.get("backup"):
            raise Fail(f"no ship to continue on `{branch}`: `--continue` resumes the run that "
                       f"made the pre-ship backup - run `forkflow ship`")
        cmd = f"git merge-base --is-ancestor {sh_arg(trunk_ref)} HEAD"
        rc, _, _ = git_rc("merge-base", "--is-ancestor", trunk_ref, "HEAD", cwd=ctx.root)
        if rc != 0:
            step("continue", cmd, f"`{branch}` is not on {trunk_ref}'s tip")
            raise Fail("the rebase did not complete; run `forkflow ship` again")
        step("continue", cmd, "the rebase completed - resuming at the squash")
        return finish_ship(ctx, args, branch, state["backup"], state.get("lease", ""))

    rc, cmd, result, err = fetch(ctx, (ctx.origin,), [trunk_ref])
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    step("fetch", cmd, result)

    rc, _, _ = git_rc("merge-base", "--is-ancestor", "HEAD", trunk_ref, cwd=ctx.root)
    if rc == 0:
        print(f"  nothing to ship: `{branch}` has no commits beyond {trunk_ref}")
        return 0

    # the lease is what the fetch just saw; the rebase and the squash come after it. A lease
    # only proves nobody pushed after that fetch - so what the fetch found has to be ours
    # already, or shipping would rewrite someone else's commits out of the branch.
    branch_ref = f"refs/remotes/{ctx.origin}/{branch}"
    lease = rev(ctx.root, branch_ref)
    if lease:
        cmd = f"git merge-base --is-ancestor {sh_arg(f'{ctx.origin}/{branch}')} HEAD"
        rc, _, _ = git_rc("merge-base", "--is-ancestor", branch_ref, "HEAD", cwd=ctx.root)
        if rc == 0:
            step("origin", cmd, f"{ctx.origin}/{branch} is already in `{branch}`")
        elif published_is_ours(ctx, branch, lease):
            step("origin", cmd, f"{ctx.origin}/{branch} is this clone's own earlier "
                                f"`{branch}` - the lease and the backup cover it")
        else:
            step("origin", cmd, f"{ctx.origin}/{branch} is not in `{branch}`")
            theirs = git("log", "--oneline", "--no-decorate", f"HEAD..{branch_ref}",
                         cwd=ctx.root, check=False)
            for line in theirs.splitlines():
                print(f"    {line}")
            keep = f"{ctx.backup_prefix}{utc_stamp('%Y%m%d-%H%M%S')}-theirs"
            raise Fail(
                f"`{ctx.origin}/{branch}` carries commits that `{branch}` does not and that "
                f"this clone has no record of publishing: shipping would force-push them "
                f"away. If they are somebody else's, take them in first "
                f"(`git pull --rebase {sh_arg(ctx.origin)} {sh_arg(branch)}`). If they are "
                f"yours from elsewhere, keep them first (`git branch {sh_arg(keep)} "
                f"{sh_arg(ctx.origin + '/' + branch)} && git push {sh_arg(ctx.origin)} "
                f"{sh_arg('refs/heads/' + keep)}:{sh_arg('refs/heads/' + keep)}`), then ship "
                f"again")

    backup_ref = backup(ctx, "pre-ship", "HEAD")
    write_state(ctx, "ship", {"branch": branch, "backup": backup_ref, "lease": lease})
    rebase_onto(ctx, branch, trunk_ref)
    return finish_ship(ctx, args, branch, backup_ref, lease)


# --------------------------------------------------------------------------- #
# land - the closing step: the merge request is merged, catch the local trunk up
#
# The only place this script moves the local trunk, and only by fast-forward (a merge, not a
# ref move: `git branch -f` stays blocked by `TestSourceInvariants`). It neither pushes,
# rebases nor commits: what `origin/<trunk>` holds after the platform merged the request
# is taken as it is, and the branch that was merged is deleted locally.
# --------------------------------------------------------------------------- #

def landed(ctx: Ctx, entry: dict) -> Tuple[Optional[str], str]:
    """Is the pending commit on `origin/<trunk>`? (the trunk commit it landed as, how).

    Git only - no API call. Ancestry first: a fast-forward or a merge commit keeps the
    SHA, so the recorded commit is simply reachable from the trunk ("ancestor"). A ship
    the platform or a human rebased or squashed lands under a new SHA with the same patch,
    so for a ship `git cherry` is asked, in the direction that names the trunk commit: which
    commits in `<base>..origin/<trunk>` carry the shipped commit's patch-id ("rewritten"). That
    survives other commits landing in between, which tree equality would not. A sync's tip
    is the merge commit itself and only ever lands as an ancestor - rewritten, it would
    have broken rule 5, which the caller says. (None, "") when nothing landed."""
    trunk_ref = f"refs/remotes/{ctx.origin}/{ctx.trunk}"
    commit, base = entry["commit"], entry["base"]
    rc, _, _ = git_rc("merge-base", "--is-ancestor", commit, trunk_ref, cwd=ctx.root)
    if rc == 0:
        return (commit, "ancestor")
    if rc != 1:
        raise Fail(f"cannot verify the landing: {short(commit)} (the pushed `{entry['branch']}`) "
                   f"is not in this clone - `land` needs the clone that ran the ship or the "
                   f"sync, or `land --force` to fast-forward unverified")
    if entry["kind"] != "ship":
        return (None, "")
    # `git cherry <upstream> <head> <limit>`: the commits in `<limit>..<head>` (here, what
    # landed on the trunk since the base), each marked `-` when a commit reachable from
    # `<upstream>` but not from `<head>` (here, exactly the shipped commit) has its patch
    rc, out, _ = git_rc("cherry", commit, trunk_ref, base, cwd=ctx.root)
    if rc != 0:
        return (None, "")
    for line in out.splitlines():
        mark, _, sha = line.partition(" ")
        if mark == "-" and sha.strip():
            return (sha.strip(), "rewritten")
    return (None, "")


def trunk_elsewhere(ctx: Ctx) -> None:
    """Fail(2) when the trunk is checked out in another worktree: it has to be checked out
    and fast-forwarded there, as `advance_mirror` insists for the mirror."""
    wt = branch_worktree(ctx, ctx.trunk)
    if wt and os.path.realpath(wt) != os.path.realpath(ctx.root):
        raise Fail(f"trunk `{ctx.trunk}` is checked out in {wt}: run `forkflow land` there, "
                   f"or remove that worktree")


def land_trunk(ctx: Ctx) -> Tuple[str, str]:
    """Check the local trunk out and fast-forward it to `origin/<trunk>`. (old sha, new sha).

    The checkout is unconditional: HEAD is usually on the branch about to be deleted, and
    `land` leaves you on the trunk either way (`git checkout`, not `switch` - the git floor
    is 2.20). The fast-forward is `merge --ff-only` - the second owner of that call after
    `advance_mirror`, pinned by `TestSourceInvariants` - and never a ref move: the branch is
    checked out, so its files have to follow. A local trunk with commits origin lacks is
    refused before anything moves: this plugin never creates such commits (rule 2), so they
    are someone's by-hand work and not this script's to lose."""
    t = ctx.trunk
    trunk_ref = f"refs/remotes/{ctx.origin}/{t}"
    shown_ref = f"{ctx.origin}/{t}"
    new = rev(ctx.root, trunk_ref)
    if not new:
        raise Fail(f"`{shown_ref}` does not resolve after the fetch: is the trunk still on "
                   f"{ctx.origin}?")
    old = rev(ctx.root, f"refs/heads/{t}")
    if old and old != new:
        rc, _, _ = git_rc("merge-base", "--is-ancestor", old, trunk_ref, cwd=ctx.root)
        if rc != 0:
            raise Fail(f"`{t}` has commits {ctx.origin} lacks - the plugin never creates "
                       f"these; resolve by hand (`git log {sh_arg(f'{shown_ref}..{t}')}`), "
                       f"then run `forkflow land` again")
    trunk_elsewhere(ctx)
    if not old:
        cmd = f"git branch --no-track {sh_arg(t)} {sh_arg(shown_ref)}"
        if ctx.dry_run:
            step("trunk", cmd, f"would create {t} at {short(new)}", dry=True)
        else:
            git("branch", "--no-track", t, trunk_ref, cwd=ctx.root)
            step("trunk", cmd, f"created at {short(new)} (no local `{t}` before)")
    cmd = f"git checkout {sh_arg(t)}"
    if ctx.dry_run:
        step("checkout", cmd, f"would leave you on {t}", dry=True)
    else:
        rc, out, err = git_rc("checkout", t, cwd=ctx.root)
        if rc != 0:
            step("checkout", cmd, "FAILED")
            raise Fail(f"cannot check out `{t}`:\n{(err or out).strip()}")
        step("checkout", cmd, f"on {t}")
    if not old or old == new:
        step("trunk", f"git rev-parse {sh_arg(t)}", f"up to date at {short(new)}")
        return (old or new, new)
    cmd = f"git merge --ff-only {sh_arg(shown_ref)}"
    if ctx.dry_run:
        step("trunk", cmd, f"{short(old)} -> {short(new)}", dry=True)
        return (old, new)
    rc, out, err = git_rc("merge", "--ff-only", trunk_ref, cwd=ctx.root)
    if rc != 0:                     # unreachable after the ancestor check, but git has the say
        step("trunk", cmd, "REFUSED")
        raise Fail(f"cannot fast-forward `{t}`:\n{(err or out).strip()}")
    step("trunk", cmd, f"{short(old)} -> {short(new)}")
    return (old, new)


def land_pending(ctx: Ctx, force: bool = False) -> None:
    """The closing step of a ship or a sync, from the `pending` record: fetch, recognise
    the landing, fast-forward the local trunk, delete the landed branch, forget the record.

    Works after a human merged the merge request, in any later session, and after a
    "squash and merge" or a "rebase and merge" of a ship (see `landed`). A merge request
    that is not on the trunk yet is exit 2 and not an error - "not merged yet" - unless
    `force`, which fast-forwards to whatever origin has, deletes no branch (nothing was
    verified) and clears the record: the escape hatch for a landing the tool cannot see,
    and for a request that was closed instead of merged. Rule 5 is reported, not enforced:
    a ship that landed as a merge commit is said out loud. A dry run fetches, decides, and
    prints every mutating step as `would:`; the record stays (`write_state`)."""
    entry = pending_entry(ctx)
    if not entry:
        raise Fail("nothing pending: `forkflow ship` or `forkflow sync` first - `land` "
                   "finishes the run that pushed a branch")
    kind, branch = entry["kind"], entry["branch"]
    if rebase_in_progress(ctx):            # before the tree: a stopped rebase leaves it dirty
        raise Fail("a rebase is in progress: finish it (`git rebase --continue`) or abort "
                   "it (`git rebase --abort`) first")
    if not clean_tree(ctx):
        raise Fail("the working tree has uncommitted changes: commit or stash them first")
    trunk_elsewhere(ctx)

    shown_ref = f"{ctx.origin}/{ctx.trunk}"
    trunk_ref = f"refs/remotes/{shown_ref}"
    rc, cmd, result, err = fetch(ctx, (ctx.origin,), [shown_ref])
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    step("fetch", cmd, result)

    request = entry.get("mr") or f"`{branch}`"
    sha, how = landed(ctx, entry)
    if sha is None:
        cmd = f"git merge-base --is-ancestor {sh_arg(short(entry['commit']))} {sh_arg(shown_ref)}"
        if not force:
            step("landed?", cmd, f"no - {short(entry['commit'])} is not on {shown_ref}")
            note = ""
            if kind == "sync":
                note = (f"; if it was squashed or rebased in the UI, rule 5 was broken (see "
                        f"rules.md) - `forkflow land --force` fast-forwards anyway")
            raise Fail(f"MR {request} is not on {shown_ref} yet - merge it, then run "
                       f"`forkflow land` again{note}")
        step("landed?", cmd, "landing not verified (--force): fast-forwarding to whatever "
                             f"{shown_ref} holds")
    else:
        if how == "ancestor":
            cmd = (f"git merge-base --is-ancestor {sh_arg(short(entry['commit']))} "
                   f"{sh_arg(shown_ref)}")
        else:
            cmd = (f"git cherry {sh_arg(short(entry['commit']))} {sh_arg(shown_ref)} "
                   f"{sh_arg(short(entry['base']))}")
        step("landed?", cmd, f"yes - as {short(sha)} ({how})")
        if kind == "ship" and how == "ancestor":
            # a fast-forward puts the shipped commit on the trunk's first-parent line; a
            # merge commit keeps its SHA too, but hangs it off a second parent
            first = git("rev-list", "--first-parent", f"{entry['base']}..{trunk_ref}",
                        cwd=ctx.root, check=False).split()
            if sha not in first:
                print(f"  WARNING: the ship MR was merged as a merge commit - rule 5 asks for "
                      f"a fast-forward; check the project's merge method (`forkflow setup` "
                      f"reports it)")

    old, new = land_trunk(ctx)

    if sha is not None and branch not in (ctx.trunk, ctx.mirror):
        # `-d` refuses a branch whose tip is unreachable from the trunk, which a rewritten
        # landing is by definition; the patch is verifiably on the trunk, so `-D` is safe
        flag = "-d" if how == "ancestor" else "-D"
        cmd = f"git branch {flag} {sh_arg(branch)}"
        if ctx.dry_run:
            step("branch", cmd, f"would delete `{branch}` (landed as {short(sha)})", dry=True)
        elif not has_ref(ctx.root, f"refs/heads/{branch}"):
            step("branch", cmd, f"`{branch}` is already gone")
        else:
            rc, out, err = git_rc("branch", flag, branch, cwd=ctx.root)
            if rc != 0:             # the landing is done; a branch that will not go is said
                step("branch", cmd, f"NOT deleted: {(err or out).strip().splitlines()[-1]}")
            else:
                step("branch", cmd, f"deleted (landed as {short(sha)})")
    elif sha is None:
        print(f"  `{branch}` is kept: its landing was not verified")

    write_state(ctx, "pending", None)
    moved = f"{short(old)}..{short(new)}" if old != new else f"at {short(new)}"
    print(f"  {'would: ' if ctx.dry_run else ''}landed: {ctx.trunk} {moved} - you are on "
          f"{ctx.trunk}")
    if ctx.platform == "github" and kind == "ship":
        print(f"  {ctx.origin}/{branch} may still exist: git push {sh_arg(ctx.origin)} "
              f"--delete {sh_arg(branch)}")


def cmd_land(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=False)
    header(ctx, "land")
    land_pending(ctx, force=bool(getattr(args, "force", False)))
    return 0


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #

# key = value lines of `.forkflow.toml`, commented or not, with whatever trails them
CONFIG_KEY_LINE = re.compile(r'^\s*#?\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
                             r'(?P<val>"[^"]*"|\[[^\]]*\]|\S*)(?P<rest>.*)$')


def toml_string(value: str) -> str:
    """A TOML basic string. `"` and `\\` are escaped: a name that carries either would
    otherwise write a `.forkflow.toml` that `load_config` refuses, bricking every subcommand."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def template_text(ctx: Ctx) -> str:
    """The commented `.forkflow.toml` `setup` drops in: every key optional, defaults shown.

    The trailing comments are padded to the longest entry, so the file lines up whatever
    the resolved names are - a hard-coded column only fits the default lengths."""
    entries = [
        (f'upstream = {toml_string(ctx.upstream)}', "remote name of the original project"),
        (f'upstream_branch = {toml_string(ctx.upstream_branch)}',
         "its branch we track (default: its HEAD)"),
        (f'mirror = {toml_string(ctx.mirror)}', "our fast-forward-only copy of it"),
        (f'trunk = {toml_string(ctx.trunk)}', "protected, MR-only branch with our work"),
        ("gate = []", 'e.g. ["make test", "terraform fmt"]'),
        (f'merge = {toml_string(ctx.merge)}',
         '"self": this fork\'s MRs are merged by whoever opened them - enables --merge'),
        (f'sync_prefix = {toml_string(ctx.sync_prefix)}', ""),
        (f'backup_prefix = {toml_string(ctx.backup_prefix)}', ""),
    ]
    width = max(len(entry) for entry, _ in entries)
    lines = [
        "# forkflow - every key is optional; the values below are what this clone resolves to.",
        "# Reading this file needs Python 3.11+ (tomllib); a file that cannot be read is fatal.",
        "",
    ]
    for entry, why in entries:
        lines.append(f"# {entry.ljust(width)}  # {why}" if why else f"# {entry}")
    lines.append("")
    return "\n".join(lines) + "\n"


def config_with_keys(text: str, pairs: Sequence[Tuple[str, str]]) -> str:
    """`text` with each key set: an existing line (commented or not) is rewritten in place,
    keeping the comment that trails it; every other line is left exactly as it was.

    Only the region above the first `[table]` header is touched, for reading and for writing:
    `load_config` reads top-level keys, so a `trunk` inside a table is a different setting.
    Rewriting that one would report a change the branch names never see, and appending below
    the header would write a key into the table instead of into the config."""
    lines = text.splitlines()
    top = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), len(lines))
    for key, value in pairs:
        entry = f'{key} = {toml_string(value)}'
        for i in range(top):
            m = CONFIG_KEY_LINE.match(lines[i])
            if m and m.group("key") == key:
                lines[i] = entry + m.group("rest")
                break
        else:
            lines.insert(top, entry)
            top += 1
    return "\n".join(lines) + "\n"


def write_config_keys(ctx: Ctx, pairs: Sequence[Tuple[str, str]]) -> None:
    """--trunk/--mirror persisted: the branch names must outlive the flags that set them."""
    path = os.path.join(ctx.root, CONFIG_FILE)
    exists = os.path.exists(path)
    if exists:
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
    else:
        text = template_text(ctx)
    new = config_with_keys(text, pairs)
    shown = ", ".join(f'{k} = "{v}"' for k, v in pairs)
    cmd = f"edit {CONFIG_FILE}"
    if new == text:
        step("toml", cmd, f"already {shown}")
        return
    if ctx.dry_run:
        step("toml", cmd, f"would {'update' if exists else 'create'} it with {shown}", dry=True)
        return
    with open(path, "w") as fh:
        fh.write(new)
    step("toml", cmd, f"{'updated' if exists else 'created'}: {shown}")


def setup_upstream_remote(ctx: Ctx, args: argparse.Namespace) -> Optional[str]:
    """The remote of the original project, added from --upstream-url when it is missing.

    None means a dry run cannot go further: the remote it would add is not there yet."""
    remotes = git("remote", cwd=ctx.root).split()
    name = getattr(args, "upstream", None) or ctx.cfg.get("upstream")
    others = [r for r in remotes if r != ctx.origin]
    if not name:
        name = others[0] if len(others) == 1 else None
    url = getattr(args, "upstream_url", None)

    if name and name in remotes:
        current = git("remote", "get-url", name, cwd=ctx.root, check=False)
        step("remote", f"git remote get-url {sh_arg(name)}", f"{name} -> {current or '-'}")
        if url and url != current:
            print(f"    note: `{name}` already points at {current}; --upstream-url is ignored - "
                  f"change it with `git remote set-url {sh_arg(name)} {sh_arg(url)}` "
                  f"if that is what you want")
        if name != "upstream":
            print(f"    note: the original project is the remote `{name}`, not `upstream`; "
                  f"forkflow uses it as it is - `git remote rename {sh_arg(name)} upstream` if you "
                  f"prefer the usual name")
        return name

    if not url:
        raise Fail(no_upstream_message(name, others))
    name = name or "upstream"
    cmd = f"git remote add {sh_arg(name)} {sh_arg(url)}"
    if ctx.dry_run:
        step("remote", cmd, "would add the remote of the original project", dry=True)
        return None
    git("remote", "add", name, url, cwd=ctx.root)
    step("remote", cmd, "added")
    return name


def setup_fetch(ctx: Ctx, upstream: str) -> None:
    """Both remotes, then their HEADs. A failure here has changed nothing yet."""
    rc, cmd, result, err = fetch(ctx, (ctx.origin, upstream))
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed - nothing has been changed yet:\n{err.strip()}")
    step("fetch", cmd, result)
    for remote in (ctx.origin, upstream):
        sub = f"git remote set-head {sh_arg(remote)} -a"
        if ctx.dry_run:                      # set-head writes config: not in a dry run
            step("set-head", sub, "not run (dry run)", dry=True)
            continue
        rc, _, err = git_rc("remote", "set-head", remote, "-a", cwd=ctx.root)
        tail = (err.strip().splitlines() or ["see git output"])[-1]
        step("set-head", sub, f"{remote}/HEAD set" if rc == 0 else f"not set ({tail})")


def origin_mirror_tip(ctx: Ctx, m: str) -> str:
    """What `origin/<mirror>` really is, asked of origin rather than of this clone.

    Remote-tracking refs are not authoritative: a narrowed fetch refspec (`clone
    --single-branch`) hides `origin/<mirror>` here even though the fork has one, and it may
    be a mirror that carries the fork's own work - the migration `setup` must refuse."""
    confirm = f"git ls-remote --heads {sh_arg(ctx.origin)} {sh_arg(m)}"
    rc, out, err = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{m}", cwd=ctx.root)
    if rc != 0:
        # a failed question is not a "no": creating a mirror on that silence makes a branch
        # the next `sync` cannot push, and hides a published mirror that carries work
        step("mirror", confirm, "FAILED")
        tail = (tail_lines(err or out, 1) or ["see git output"])[0]
        raise Fail(f"cannot ask {ctx.origin} whether `{m}` is there ({tail}): refusing to "
                   f"create the mirror on a guess - fix the connection and rerun "
                   f"`forkflow setup`")
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip() == f"refs/heads/{m}":
            return parts[0].strip()
    return ""


def check_published_mirror(ctx: Ctx, m: str, published: str, target: str) -> None:
    """Refuse a published mirror this clone cannot prove is a pure copy of upstream."""
    confirm = f"git ls-remote --heads {sh_arg(ctx.origin)} {sh_arg(m)}"
    widen = (f"git config --add {sh_arg(f'remote.{ctx.origin}.fetch')} "
             f"{sh_arg(f'+refs/heads/{m}:refs/remotes/{ctx.origin}/{m}')} && "
             f"git fetch {sh_arg(ctx.origin)}")
    rc, _, _ = git_rc("merge-base", "--is-ancestor", published, target, cwd=ctx.root)
    if rc == 1:
        step("mirror", confirm, f"on {ctx.origin} at {short(published)}: REFUSED")
        raise Fail(f"`{ctx.origin}/{m}` is at {short(published)}, which is not in "
                   f"`{ctx.up()}`: it carries commits that are not upstream's, so it cannot "
                   f"be the mirror, and forkflow never resets a published branch. "
                   f"{README_POINTER}")
    if rc != 0:
        step("mirror", confirm, f"on {ctx.origin} at {short(published)}: not in this clone")
        raise Fail(f"`{ctx.origin}/{m}` is at {short(published)}, a commit this clone does "
                   f"not have, so it cannot be checked against `{ctx.up()}` - and a mirror "
                   f"that carries the fork's own work is a migration, not a `setup`. Widen "
                   f"the refspec and fetch ({widen}), then rerun `forkflow setup`. "
                   f"{README_POINTER}")
    step("mirror", confirm, f"already on {ctx.origin} at {short(published)} and in "
                            f"`{ctx.up()}`, but this clone does not fetch it")
    print(f"    widen the refspec and fetch: {widen}")


def setup_mirror(ctx: Ctx, target: str) -> None:
    """Check the mirror, or create it in a single-branch clone. It is never reset:
    a `<mirror>` that carries work is a migration, and that is done by hand."""
    m = ctx.mirror
    local = has_ref(ctx.root, f"refs/heads/{m}")
    remote = has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{m}")
    if not (local or remote):
        published = origin_mirror_tip(ctx, m)
        if published:
            check_published_mirror(ctx, m, published, target)
        cmd = f"git branch --no-track {sh_arg(m)} {short(target)}"
        if ctx.dry_run:
            step("mirror", cmd, f"would create the mirror at {short(target)}", dry=True)
            return
        git("branch", "--no-track", m, target, cwd=ctx.root)
        step("mirror", cmd, f"created at {short(target)}")
        return
    for ref in ([m] if local else []) + ([f"{ctx.origin}/{m}"] if remote else []):
        rc, _, _ = git_rc("merge-base", "--is-ancestor", ref, target, cwd=ctx.root)
        if rc == 1:
            raise Fail(f"`{ref}` has commits that are not in `{ctx.up()}`: it cannot be the "
                       f"mirror, and forkflow never resets a published branch. {README_POINTER}")
        if rc != 0:
            raise Fail(f"cannot compare `{ref}` with `{ctx.up()}`: "
                       f"run `git fetch {sh_arg(ctx.upstream)}`")
    step("mirror", f"git merge-base --is-ancestor {sh_arg(m)} {sh_arg(ctx.up())}",
         f"`{m}` is a pure copy of `{ctx.up()}` (behind is fine - `forkflow sync` advances it)")


def setup_trunk(ctx: Ctx, target: str) -> None:
    """Create the trunk on a fresh fork. Runs before the hook, which refuses every push
    of the trunk - creation included."""
    t = ctx.trunk
    origin_t = rev(ctx.root, f"refs/remotes/{ctx.origin}/{t}")
    if origin_t:
        step("trunk", f"git rev-parse {sh_arg(f'{ctx.origin}/{t}')}",
             f"already on {ctx.origin} at {short(origin_t)}")
        return
    if has_ref(ctx.root, f"refs/heads/{t}"):
        raise Fail(f"trunk `{t}` exists locally but not on {ctx.origin}: forkflow never pushes "
                   f"the trunk - push it yourself once it is what you want "
                   f"(`git push -u {sh_arg(ctx.origin)} {sh_arg(t)}`, and do it before "
                   f"the pre-push hook is "
                   f"installed: from then on rule 2 refuses every trunk push, creation "
                   f"included), or delete it and rerun `forkflow setup`")
    # remote-tracking refs are not authoritative: a narrowed fetch refspec hides `origin/<trunk>`
    # from this clone, and bootstrapping then pushes a trunk that already exists
    confirm = f"git ls-remote --heads {sh_arg(ctx.origin)} {sh_arg(t)}"
    rc, out, err = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{t}", cwd=ctx.root)
    if rc != 0:
        # a failed question is not a "no": the remote-tracking refs have already said nothing,
        # and bootstrapping on a guess pushes a trunk that may be there - outside a merge
        # request, which is the one way the trunk is allowed to move (rule 2)
        step("trunk", confirm, "FAILED")
        tail = (tail_lines(err or out, 1) or ["see git output"])[0]
        raise Fail(f"cannot ask {ctx.origin} whether `{t}` is there ({tail}): refusing to "
                   f"create the trunk on a guess - fix the connection and rerun "
                   f"`forkflow setup`")
    if f"refs/heads/{t}" in out:
        step("trunk", confirm, f"already on {ctx.origin}, but this clone does not fetch it")
        widen = f"remote.{ctx.origin}.fetch"
        spec = f"+refs/heads/{t}:refs/remotes/{ctx.origin}/{t}"
        print(f"    widen the refspec and fetch: git config --add "
              f"{sh_arg(widen)} {sh_arg(spec)} && "
              f"git fetch {sh_arg(ctx.origin)}")
        return
    bootstrap_trunk(ctx, target)


def setup_push_url(ctx: Ctx) -> None:
    """`DISABLED` makes `git push <upstream>` fail before any hook can even run.

    `remote.<name>.pushurl` is multi-valued and git pushes to every one of them, while
    `set-url --push` only replaces the first: the whole key is dropped first, so a second
    live URL cannot survive a run that reports `DISABLED`."""
    unset = f"git config --unset-all remote.{sh_arg(ctx.upstream)}.pushurl"
    set_url = f"git remote set-url --push {sh_arg(ctx.upstream)} DISABLED"
    urls = push_urls(ctx.root, ctx.upstream)
    if urls == ["DISABLED"]:
        step("push url", set_url, "already DISABLED")
        return
    extra = len([u for u in urls if u != "DISABLED"]) > 1
    cmd = f"{unset} && {set_url}" if extra else set_url
    if ctx.dry_run:
        step("push url", cmd, f"would stop every push to `{ctx.upstream}`"
                              + (f" ({len(urls)} push URLs configured)" if extra else ""),
             dry=True)
        return
    git("config", "--unset-all", f"remote.{ctx.upstream}.pushurl", cwd=ctx.root, check=False)
    git("remote", "set-url", "--push", ctx.upstream, "DISABLED", cwd=ctx.root)
    step("push url", cmd, f"pushes to `{ctx.upstream}` now fail")


# `repo_id()` as POSIX sh, for the hook. Rule 1 is about a repository, and a repository has many
# spellings: `<url>/`, `file://<url>`, `<url>.git`, `user@host:...`, an explicit default port and
# a capitalised host all reach the same place, and so do `../repo.git`, `<path>/.`, a symlink to
# it and `file://localhost/<path>` when the repository is a local one. Comparing the raw strings
# refuses one spelling and waves the rest through, so both sides are normalised here - the same
# way `repo_id()` does, and kept in step with it by `test_the_hooks_url_normaliser_matches_repo_id`
# and `test_every_spelling_of_a_local_upstream_is_one_repository`.
# It is spliced into HOOK_TEMPLATE rather than written inside it: this string is never
# %-formatted, so sh's `${x%y}` expansions read as themselves.
HOOK_REPO_ID = """ff_repo_id() {
    r=$1
    while :; do
        case "$r" in
        */) r=${r%/} ;;
        *) break ;;
        esac
    done
    s=
    a=
    h=
    case "$r" in
    *://*)
        s=$(printf '%s' "${r%%://*}" | tr 'A-Z' 'a-z')
        r=${r#*://}
        h=1
        ;;
    *:*)
        case "${r%%:*}" in
        */*) ;;                                 # a path with a colon in it, not host:path
        *)
            s=ssh                               # scp-like [user@]host:path
            h=1
            p=${r#*:}
            while :; do
                case "$p" in
                /*) p=${p#/} ;;
                *) break ;;
                esac
            done
            r="${r%%:*}/$p"
            ;;
        esac
        ;;
    esac
    if [ -n "$h" ]; then
        a=${r%%/*}                              # [user@]host[:port]
        rest=${r#"$a"}
        a=${a##*@}
        case "$s:${a##*:}" in
        ssh:22|git+ssh:22|git:9418|http:80|https:443) a=${a%:*} ;;
        esac
        case "$s/$(printf '%s' "$a" | tr 'A-Z' 'a-z')" in
        file/localhost|file/localhost.) a= ;;   # `file://localhost/p` is the path `/p`
        esac
        if [ -n "$a" ]; then
            r=$(printf '%s%s' "$a" "$rest" | tr 'A-Z' 'a-z')
        else
            r=$rest
        fi
    fi
    if [ -z "$a" ] && [ -n "$r" ]; then          # a local path: `..`, `/.`, symlinks resolved
        p=$(CDPATH= cd -P -- "$r" 2>/dev/null && pwd -P) && [ -n "$p" ] && r=$p
    fi
    case "$r" in
    *.git) r=${r%.git} ;;
    esac
    printf '%s' "$r"
}"""

# The hook is the second half of the guarantee: the script routes every push through the three
# helpers, and this refuses the forbidden pushes even when git is driven by hand. Every name and
# every upstream URL are baked in at install time, so the hook needs no config of its own.
HOOK_TEMPLATE = """#!/bin/sh
%(mark)s - written by `forkflow setup`; `forkflow setup --force` replaces it.
#
# Refuses the pushes the workflow forbids, however git is driven:
#   - anything to the original project (remote `%(upstream)s`, or any spelling of its URL)
#   - anything to the trunk `%(trunk)s`, deletion included: merge requests only
#   - any push of the mirror `%(mirror)s` that is not a pure copy of upstream
#
# The mirror check validates against the last fetch of `%(upstream)s` (%(up_ref)s);
# fetch before pushing the mirror.

up_remote=%(upstream_q)s
up_ref=%(up_ref_q)s
origin_remote=%(origin_q)s
trunk_ref=%(trunk_ref_q)s
mirror_ref=%(mirror_ref_q)s
zero='0000000000000000000000000000000000000000'

%(repo_id_fn)s

to_upstream=no
[ "$1" = "$up_remote" ] && to_upstream=yes
if [ "$to_upstream" = no ] && [ -n "$2" ]; then
    dest=$(ff_repo_id "$2")
    for u in %(up_urls_q)s; do
        [ -n "$u" ] || continue
        [ "$dest" = "$(ff_repo_id "$u")" ] && to_upstream=yes
    done
fi
if [ "$to_upstream" = yes ]; then
    echo "forkflow: never push to upstream (rule 1) - it is the original project" >&2
    exit 1
fi

status=0
while read -r local_ref local_sha remote_ref remote_sha; do
    [ -n "$remote_ref" ] || continue
    case "$remote_ref" in
    "$trunk_ref")
        echo "forkflow: never push the trunk (rule 2) - $remote_ref moves only through a merge request" >&2
        status=1
        ;;
    "$mirror_ref")
        if [ "$local_sha" = "$zero" ]; then
            echo "forkflow: refusing to delete the mirror $remote_ref (rule 6)" >&2
            status=1
            continue
        fi
        if [ "$remote_sha" != "$zero" ]; then
            git merge-base --is-ancestor "$remote_sha" "$local_sha" 2>/dev/null
            rc=$?
            if [ "$rc" -eq 1 ]; then
                echo "forkflow: the mirror only moves forward (rule 6) - $remote_sha is not in $local_sha" >&2
                status=1
                continue
            elif [ "$rc" -ne 0 ]; then
                echo "forkflow: cannot verify that the mirror only moves forward (rule 6) - $remote_sha is not in this clone; run: git fetch $origin_remote" >&2
                status=1
                continue
            fi
        fi
        git merge-base --is-ancestor "$local_sha" "$up_ref" 2>/dev/null
        rc=$?
        if [ "$rc" -eq 1 ]; then
            echo "forkflow: the mirror push must be a pure copy of upstream (rule 6) - $local_sha is not in $up_ref" >&2
            status=1
        elif [ "$rc" -ne 0 ]; then
            echo "forkflow: cannot verify the mirror against $up_ref (missing or unfetched); run: git fetch $up_remote" >&2
            status=1
        fi
        ;;
    esac
done
exit $status
"""


def sh_arg(value: str) -> str:
    """One word of a command this script *prints* for a human or Claude to paste.

    Every branch and remote name reaches such a command, and `git check-ref-format` accepts
    `;`, a backtick and `$(` - names that come from `.forkflow.toml`, a tracked file a sync
    can bring in from upstream. Quoted only when it has to be (`shlex.quote`, as `open_mr`
    already renders its argv), so an ordinary name prints exactly as the reader expects and
    only a name that would otherwise be more than one word changes shape."""
    return shlex.quote(value)


def sh_quote(value: str) -> str:
    """`value` as one single-quoted shell word. Always quoted, unlike `shlex.quote`, so the
    hook reads the same whatever the name is - and a value carrying `'` or `$` cannot end
    the word early and turn the rest of it into code.

    Used for the generated hook and for the platform report's fix commands, which are pasted
    into a shell by hand; `open_mr` renders an argv list it also runs itself, and quotes that
    with `shlex.quote` so what is shown is exactly the argv that ran."""
    return "'" + value.replace("'", "'\\''") + "'"


def upstream_urls(ctx: Ctx) -> list:
    """Every URL that reaches the original project, for the hook to refuse.

    `remote.<name>.url` and `remote.<name>.pushurl` are both multi-valued, and `setup` has
    already replaced the push URL with `DISABLED` by the time the hook is written - so the
    fetch URLs are what is left, and every one of them is a way to reach upstream (rule 1)."""
    urls = [ctx.upstream_url]
    urls += git("remote", "get-url", "--all", ctx.upstream, cwd=ctx.root,
                check=False).splitlines()
    urls += push_urls(ctx.root, ctx.upstream)
    seen, out = set(), []
    for url in urls:
        url = (url or "").strip()
        if not url or url == "DISABLED" or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def hook_text(ctx: Ctx) -> str:
    """The pre-push hook for this fork - names and upstream URLs substituted in.

    Every value the shell reads is quoted: the names come from `.forkflow.toml`, a tracked
    file a sync can bring in from upstream, and this text is run by `sh` on every push."""
    up_ref = f"refs/remotes/{ctx.upstream}/{ctx.upstream_branch}"
    urls = [sh_quote(u) for u in upstream_urls(ctx)]
    return HOOK_TEMPLATE % {
        "mark": HOOK_MARK,
        "upstream": ctx.upstream,
        "upstream_q": sh_quote(ctx.upstream),
        # one empty word rather than nothing: `for u in ; do` is a syntax error, and a hook
        # that will not parse refuses nothing at all
        "up_urls_q": " ".join(urls) or "''",
        "repo_id_fn": HOOK_REPO_ID,
        "up_ref": up_ref,
        "up_ref_q": sh_quote(up_ref),
        "origin_q": sh_quote(ctx.origin),
        "trunk": ctx.trunk,
        "trunk_ref_q": sh_quote(f"refs/heads/{ctx.trunk}"),
        "mirror": ctx.mirror,
        "mirror_ref_q": sh_quote(f"refs/heads/{ctx.mirror}"),
    }


def setup_hook(ctx: Ctx, force: bool) -> None:
    """Install the pre-push hook. It must run after the trunk exists: the hook refuses every
    push of the trunk, creation included."""
    d = hooks_path(ctx.root)
    if not d:
        raise Fail("cannot locate the hooks directory (`git rev-parse --git-path hooks`)")
    path = os.path.join(d, "pre-push")
    cmd = f"write {path}"

    shared = git("config", "--get", "core.hooksPath", cwd=ctx.root, check=False)
    if shared:
        step("hook", cmd, f"WARNING: `core.hooksPath` is `{shared}` - a shared hooks directory")
        if not force:
            raise Fail(f"the hooks directory `{d}` is shared through `core.hooksPath`: "
                       f"forkflow's pre-push hook would apply to every repository that uses "
                       f"it. Install it there yourself, or rerun with `forkflow setup --force`")

    state = hook_state(ctx)
    if state == "foreign" and not force:
        step("hook", cmd, "REFUSED")
        raise Fail(f"`{path}` is not forkflow's hook: rerun with `forkflow setup --force` to "
                   f"install forkflow's and keep the current one as `pre-push.pre-forkflow`")

    what = {"missing": "would install it",
            "installed": "would rewrite it with the current names",
            "foreign": "would install it, keeping the current one as `pre-push.pre-forkflow`"}
    if ctx.dry_run:
        step("hook", cmd, what[state], dry=True)
        return

    os.makedirs(d, exist_ok=True)
    if state == "foreign":
        os.replace(path, path + ".pre-forkflow")
    with open(path, "w") as fh:
        fh.write(hook_text(ctx))
    os.chmod(path, 0o755)
    done = {"missing": "installed", "installed": "rewritten with the current names",
            "foreign": "installed; the previous hook is kept as `pre-push.pre-forkflow`"}
    step("hook", cmd, done[state])
    print(f"    it refuses every push to `{ctx.upstream}` (by name, and by any spelling of its "
          f"URL) and to `{ctx.trunk}`, "
          f"and any `{ctx.mirror}` push that is not a copy of `{ctx.up()}` as last fetched - "
          f"so `git fetch {sh_arg(ctx.upstream)}` before pushing the mirror")


def setup_git_config(ctx: Ctx) -> None:
    """ff-only for the two long-lived branches is rule 3 and rule 6 in git's own hands."""
    settings = [
        (f"branch.{ctx.trunk}.mergeOptions", "--ff-only", "rule: the trunk only fast-forwards"),
        (f"branch.{ctx.mirror}.mergeOptions", "--ff-only", "rule: the mirror only fast-forwards"),
        ("pull.ff", "only", "rule: a pull never creates a merge commit"),
        ("rerere.enabled", "true", "convenience, not a rule: remembers conflict resolutions"),
    ]
    for key, value, why in settings:
        cmd = f"git config {sh_arg(key)} {sh_arg(value)}"
        if git("config", "--get", key, cwd=ctx.root, check=False) == value:
            step("config", cmd, f"already set ({why})")
            continue
        if ctx.dry_run:
            step("config", cmd, f"would set it ({why})", dry=True)
            continue
        git("config", key, value, cwd=ctx.root)
        step("config", cmd, why)


# --------------------------------------------------------------------------- #
# setup: the platform report
#
# Default branch, merge method and branch protection are project-wide settings that only a
# Maintainer can change, and changing one behind someone's back is exactly the kind of surprise
# this plugin exists to prevent. So every call here is a GET: what is wrong is reported with the
# command that fixes it, and a human runs it.
# --------------------------------------------------------------------------- #

@dataclass
class Reply:
    """One read-only `glab api` / `gh api` call: the object it returned, or why there is none."""
    data: Optional[dict] = None
    status: Optional[int] = None
    note: str = ""

    def ok(self) -> bool:
        return self.data is not None


def api_status(text: str) -> Optional[int]:
    """The HTTP status a failed call reports: `gh` prints `(HTTP 404)`, `glab` `404 Not Found`.

    Both patterns are anchored to what the tools actually print. A bare three-digit number
    anywhere in an unrelated message (a port, a line number) is not a status: reporting one
    would turn `could not resolve host ...:443` into a verdict about branch protection."""
    m = (re.search(r"HTTP[ /][0-9.]*\s*(\d{3})", text)
         or re.search(r"(?m)^\s*([45]\d\d)\s+[A-Za-z]", text))
    return int(m.group(1)) if m else None


def api_get(ctx: Ctx, tool: str, path: str) -> Reply:
    """`<tool> api <path>` - a GET, always: `setup` reports server settings, never changes them."""
    try:
        p = subprocess.run([tool, "api", path], cwd=ctx.root, capture_output=True)
    except OSError as exc:
        return Reply(note=f"not checked ({tool} unavailable: {exc.strerror or exc})")
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        status = api_status(err + out)
        if status == 403:
            return Reply(status=status, note="not checked (insufficient rights)")
        if status:
            return Reply(status=status, note=f"not checked (HTTP {status})")
        tail = (tail_lines(err, 1) or ["no output"])[0]
        return Reply(note=f"not checked ({tool} exit {p.returncode}: {tail})")
    try:
        data = json.loads(out)
    except ValueError:
        return Reply(note=f"not checked ({tool} did not answer with JSON)")
    if not isinstance(data, dict):
        return Reply(note=f"not checked ({tool} did not answer with an object)")
    return Reply(data=data)


def finding(text: str) -> None:
    print(f"    {text}")


def fix_cmd(cmd: str) -> None:
    print(f"      fix: {cmd}")


def default_branch_finding(ctx: Ctx, default: str) -> bool:
    """True when the default branch has to change: merge requests open against it."""
    if default == ctx.trunk:
        finding(f"default branch: `{default}` is the trunk: ok")
        return False
    finding(f"default branch: `{default}` - it must be the trunk `{ctx.trunk}`, so that merge "
            f"requests open against it and a fresh clone starts there")
    return True


def protection_finding(role: str, branch: str, force: Optional[bool],
                       direct_push: Optional[bool] = None) -> bool:
    """Print the verdict for one branch; True when a fix command belongs under it.

    `force` is None when the branch is not protected at all. `direct_push` is True when the
    server still lets somebody push straight to it, None when that could not be read - and it
    is a finding for the trunk only: `sync` reaches the mirror by pushing it, so the mirror has
    to stay directly pushable."""
    if force is None:
        if role == "trunk":
            finding(f"trunk `{branch}`: NOT protected - nothing on the server stops a direct "
                    f"push or a force-push to it")
            return True
        finding(f"mirror `{branch}`: not protected (advisory: forkflow's pre-push hook already "
                f"keeps it a pure copy of upstream; protecting it as well is optional)")
        return False
    problems = []
    if force:
        problems.append("force-push is ALLOWED - undoing a bad merge by rewriting a published "
                        "branch is the one thing protection is for here")
    if direct_push and role == "trunk":
        problems.append("a direct push is still allowed - the trunk moves only through a "
                        "merge request, so somebody who can push straight to it is rule 2 "
                        "undone")
    if problems:
        finding(f"{role} `{branch}`: protected, but " + "; and ".join(problems))
        return True
    ok = "protected, force-push disallowed"
    if role == "trunk":
        ok += ", direct push blocked" if direct_push is False else ", direct push not checked"
    finding(f"{role} `{branch}`: {ok}: ok")
    return False


def gitlab_push_access(data: dict) -> Optional[bool]:
    """True when somebody can still push straight to a protected branch.

    `push_access_levels` is a list of rules; level 0 is GitLab's "No one". None when the answer
    does not carry the field or carries it in a shape this cannot read."""
    levels = data.get("push_access_levels")
    if not isinstance(levels, list):
        return None
    found = []
    for item in levels:
        if not isinstance(item, dict) or not isinstance(item.get("access_level"), int):
            return None
        found.append(item["access_level"])
    return any(level > 0 for level in found)


def gitlab_protection(ctx: Ctx, base: str, role: str, branch: str) -> None:
    """`base` is the fork's own project path, `projects/<encoded full path>` - never
    `:fullpath`, which glab resolves to the original project (see `forge_path`)."""
    # the name is one path segment: `release/1.0` unencoded would address another endpoint
    path = f"{base}/protected_branches/{urllib.parse.quote(branch, safe='')}"
    reply = api_get(ctx, "glab", path)
    if reply.ok():
        force = bool(reply.data.get("allow_force_push"))
        direct = gitlab_push_access(reply.data)
    elif reply.status == 404:                       # GitLab: no such protected branch
        force, direct = None, None
    else:
        finding(f"{role} `{branch}`: {reply.note}")
        return
    if not protection_finding(role, branch, force, direct):
        return
    # every name reaches a shell the user is told to paste: `git check-ref-format` allows `;`,
    # a backtick and `$(`, and these names come from a tracked file a sync can bring in
    protect = (f"glab api --method POST {sh_arg(base)}/protected_branches "
               f"-f name={sh_quote(branch)} -F push_access_level=0 -F merge_access_level=40 "
               f"-F allow_force_push=false")
    if force is None:
        fix_cmd(protect)
        return
    if force:
        fix_cmd(f"glab api --method PATCH {sh_arg(path)} -F allow_force_push=false")
    if direct and role == "trunk":
        finding("who may push can only be changed by recreating the rule (GitLab's PATCH takes "
                "`allowed_to_push` entries by id, not a level): this deletes it and creates it "
                "again, with merge requests merged by Maintainers")
        fix_cmd(f"glab api --method DELETE {sh_arg(path)} && {protect}")


def gitlab_report(ctx: Ctx, project: str) -> None:
    # the whole path is one id: a subgroup's `/` is part of the project's name here
    path = "projects/" + urllib.parse.quote(project, safe="")
    reply = api_get(ctx, "glab", path)
    if not reply.ok():
        step("platform", f"glab api {sh_arg(path)}", reply.note)
        return
    default = str(reply.data.get("default_branch") or "-")
    method = str(reply.data.get("merge_method") or "-")
    step("platform", f"glab api {sh_arg(path)}",
         f"read-only: default_branch={default}  merge_method={method}")
    fixes = []
    if default_branch_finding(ctx, default):
        fixes.append(f"-f default_branch={sh_quote(ctx.trunk)}")
    if method == "ff":
        finding("merge method: `ff`: ok")
    else:
        finding(f"merge method: `{method}` - ship MRs must fast-forward; a sync MR still goes "
                f"in whole, its tip being the merge commit")
        fixes.append("-f merge_method=ff")
    if fixes:
        fix_cmd(f"glab api --method PUT {sh_arg(path)} " + " ".join(fixes))
    gitlab_protection(ctx, path, "trunk", ctx.trunk)
    gitlab_protection(ctx, path, "mirror", ctx.mirror)


def names(items: object, *keys: str) -> list:
    """The logins/slugs of a `restrictions` list, as the PUT wants them."""
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            for key in keys:
                if isinstance(item.get(key), str):
                    out.append(item[key])
                    break
        elif isinstance(item, str):
            out.append(item)
    return out


def actors(current: object) -> Optional[dict]:
    """A `{users, teams, apps}` block of the GET, in the shape the PUT wants it back."""
    if not isinstance(current, dict):
        return None
    return {"users": names(current.get("users"), "login"),
            "teams": names(current.get("teams"), "slug", "name"),
            "apps": names(current.get("apps"), "slug", "name")}


def flag(current: dict, key: str) -> bool:
    """One boolean of the protection object - most of them are wrapped as `{"enabled": ...}`."""
    value = current.get(key)
    return bool(value.get("enabled")) if isinstance(value, dict) else bool(value)


def github_protection_body(current: Optional[dict], require_pr: bool = False) -> str:
    """The `PUT .../protection` body that turns force-pushes off (and, with `require_pr`, makes
    a merge request the only way in) while carrying back every other setting the GET returned.

    GitHub's PUT replaces the whole protection object: anything this body does not name goes
    back to its default, so a required review, a status check, a linear-history or
    conversation-resolution requirement the project already has has to be translated back into
    what the PUT expects, key by key."""
    cur = current if isinstance(current, dict) else {}
    checks = cur.get("required_status_checks")
    status = None
    if isinstance(checks, dict):
        status = {"strict": bool(checks.get("strict"))}
        # `checks` binds each context to the app that may report it; `contexts` is the flat
        # legacy list of the same names. Carrying only the latter back would silently unbind
        # them, so the richer form wins whenever the GET returned one
        detailed = []
        for item in checks.get("checks") or []:
            if isinstance(item, dict) and isinstance(item.get("context"), str):
                one = {"context": item["context"]}
                if isinstance(item.get("app_id"), int):
                    one["app_id"] = item["app_id"]
                detailed.append(one)
        if detailed:
            status["checks"] = detailed
        else:
            status["contexts"] = [c for c in (checks.get("contexts") or [])
                                  if isinstance(c, str)]
    reviews = cur.get("required_pull_request_reviews")
    prr = None
    if isinstance(reviews, dict):
        prr = {"dismiss_stale_reviews": bool(reviews.get("dismiss_stale_reviews")),
               "require_code_owner_reviews": bool(reviews.get("require_code_owner_reviews")),
               "require_last_push_approval": flag(reviews, "require_last_push_approval"),
               "required_approving_review_count":
                   int(reviews.get("required_approving_review_count") or 0)}
        dismissal = actors(reviews.get("dismissal_restrictions"))
        if dismissal is not None:
            prr["dismissal_restrictions"] = dismissal
        bypass = actors(reviews.get("bypass_pull_request_allowances"))
        if bypass is not None:
            prr["bypass_pull_request_allowances"] = bypass
    elif require_pr:            # rule 2: a protected branch without this takes a direct push
        prr = {"dismiss_stale_reviews": False, "require_code_owner_reviews": False,
               "required_approving_review_count": 0}
    if require_pr and prr is not None:
        # the two ways past a required pull request, both of which leave the trunk directly
        # pushable: an admin exempted by `enforce_admins: false`, and a bypass allowance
        prr.pop("bypass_pull_request_allowances", None)
    body = {"required_status_checks": status,
            "enforce_admins": True if require_pr else flag(cur, "enforce_admins"),
            "required_pull_request_reviews": prr,
            "restrictions": actors(cur.get("restrictions")),
            "allow_force_pushes": False,
            "allow_deletions": flag(cur, "allow_deletions"),
            "block_creations": flag(cur, "block_creations"),
            "required_conversation_resolution": flag(cur, "required_conversation_resolution"),
            "required_linear_history": flag(cur, "required_linear_history"),
            "lock_branch": flag(cur, "lock_branch"),
            "allow_fork_syncing": flag(cur, "allow_fork_syncing")}
    return json.dumps(body, separators=(",", ":"))


def github_direct_push(data: dict) -> Tuple[bool, str]:
    """(somebody can still push straight to this protected branch, why).

    A required pull request is not on its own the answer rule 2 needs. `enforce_admins: false`
    exempts every administrator from the whole protection object, and every user, team or app
    in `bypass_pull_request_allowances` pushes straight in - either way the trunk moves
    without a merge request."""
    reviews = data.get("required_pull_request_reviews")
    if not isinstance(reviews, dict):
        return (True, "no pull request is required")
    if not flag(data, "enforce_admins"):
        return (True, "`enforce_admins` is false, which exempts every administrator")
    bypass = actors(reviews.get("bypass_pull_request_allowances")) or {}
    named = [n for group in bypass.values() for n in group]
    if named:
        return (True, "`bypass_pull_request_allowances` lets " + ", ".join(named) + " through")
    return (False, "")


def github_protection(ctx: Ctx, base: str, role: str, branch: str) -> None:
    """`base` is the fork's own `repos/<owner>/<repo>` - never gh's `{owner}/{repo}`, which
    gh resolves to the original project (see `forge_path`)."""
    path = f"{base}/branches/" + urllib.parse.quote(branch, safe="") + "/protection"
    reply = api_get(ctx, "gh", path)
    why = ""
    if reply.ok():
        force = flag(reply.data, "allow_force_pushes")
        direct, why = github_direct_push(reply.data)
    elif reply.status == 404:                       # GitHub: "Branch not protected"
        force, direct = None, None
    else:
        finding(f"{role} `{branch}`: {reply.note}")
        return
    if not protection_finding(role, branch, force, direct):
        return
    if direct and role == "trunk" and why:
        finding(f"direct push: {why}")
    if force is None:
        # nothing to carry over: a fresh protection object, admins included and a pull request
        # required - without the latter a protected branch still takes a direct push from
        # anyone with write access, which is rule 2
        body = github_protection_body({"enforce_admins": True}, require_pr=True)
        fix_cmd(f"echo {sh_quote(body)} | gh api -X PUT {sh_arg(path)} --input -")
        return
    finding("the PUT below replaces the whole protection object: it carries over the "
            "settings the read above returned - check them before you run it")
    body = github_protection_body(reply.data, require_pr=bool(direct) and role == "trunk")
    fix_cmd(f"echo {sh_quote(body)} | gh api -X PUT {sh_arg(path)} --input -")


def github_report(ctx: Ctx, project: str) -> None:
    path = "repos/" + "/".join(urllib.parse.quote(p, safe="") for p in project.split("/"))
    reply = api_get(ctx, "gh", path)
    if not reply.ok():
        step("platform", f"gh api {sh_arg(path)}", reply.note)
        return
    default = str(reply.data.get("default_branch") or "-")
    merge_commit = bool(reply.data.get("allow_merge_commit"))
    rebase = bool(reply.data.get("allow_rebase_merge"))
    step("platform", f"gh api {sh_arg(path)}",
         f"read-only: default_branch={default}  allow_merge_commit={str(merge_commit).lower()}"
         f"  allow_rebase_merge={str(rebase).lower()}")
    fixes = []
    if default_branch_finding(ctx, default):
        fixes.append(f"-f default_branch={sh_quote(ctx.trunk)}")
    if merge_commit:
        finding("merge commits: allowed: ok (a sync MR is merged with `Create a merge commit`)")
    else:
        finding("merge commits: NOT allowed - a sync MR has to go in as a merge commit; "
                "squashing or rebasing it rewrites upstream's SHAs out of the trunk")
        fixes.append("-F allow_merge_commit=true")
    if rebase:
        finding("rebase merges: allowed: ok (a ship MR is merged with `Rebase and merge`)")
    else:
        finding("rebase merges: NOT allowed - a ship MR is merged with `Rebase and merge`")
        fixes.append("-F allow_rebase_merge=true")
    if fixes:
        fix_cmd(f"gh api -X PATCH {sh_arg(path)} " + " ".join(fixes))
    github_protection(ctx, path, "trunk", ctx.trunk)
    github_protection(ctx, path, "mirror", ctx.mirror)


def platform_report(ctx: Ctx) -> None:
    """What the hosting platform has to say - reported, never changed. Runs in a dry run too:
    every call is a GET.

    Every path is built from the origin URL: the placeholders the two tools expand themselves
    name the original project in a fork that has an upstream remote (see `forge_path`)."""
    project = forge_path(ctx.origin_url)
    if ctx.platform == "gitlab" and project:
        gitlab_report(ctx, project)
    elif ctx.platform == "github" and project.count("/") == 1:   # `owner/repo`, and no more
        github_report(ctx, project)
    else:
        why = ("unknown host" if ctx.platform == "unknown" else
               f"{ctx.platform}, but `{ctx.origin_url}` names no project there")
        step("platform", f"# origin {ctx.origin_url or '-'}",
             f"{why} - check yourself that the default branch is `{ctx.trunk}`, that "
             f"merge requests into it fast-forward, and that it is protected")


def setup_template(ctx: Ctx) -> None:
    """A commented `.forkflow.toml`, left untracked: the branch names are the fork's decision."""
    path = os.path.join(ctx.root, CONFIG_FILE)
    cmd = f"write {CONFIG_FILE}"
    if not have_tomllib():
        # the template is a starting point to edit, and the first uncommented key in it would
        # make every subcommand exit 2 on this Python. Nothing here needs a config file.
        step("template", cmd, f"skipped: reading {CONFIG_FILE} needs Python 3.11+ (tomllib) "
                              f"and this is Python {sys.version_info[0]}."
                              f"{sys.version_info[1]} - the defaults are in use")
        return
    if os.path.exists(path):
        step("template", cmd, "already there, left as it is")
    elif ctx.dry_run:
        step("template", cmd, "would write the commented template", dry=True)
        return
    else:
        with open(path, "w") as fh:
            fh.write(template_text(ctx))
        step("template", cmd, "commented template written")
    if not git_ok("ls-files", "--error-unmatch", CONFIG_FILE, cwd=ctx.root):
        print(f"    it is untracked: commit {CONFIG_FILE} when you are happy with it, so the "
              f"branch names and the gate are the same for everyone")


def cmd_setup(args: argparse.Namespace) -> int:
    """Make the rules mechanical in this clone. The step order matters - see README."""
    ctx = resolve_ctx(args.dir, args, need_upstream=False, need_trunk=False, strict_mirror=False)
    header(ctx, "setup")

    name = setup_upstream_remote(ctx, args)
    if name is None:                                   # dry run: the remote is not there yet
        print("  the rest of `setup` needs the upstream remote - rerun without --dry-run")
        return 0
    setup_fetch(ctx, name)
    # the upstream branch (and with it the default mirror name) is only known after the fetch;
    # `upstream=name` because a remote this run has just added is in neither flag nor config
    ctx = resolve_ctx(ctx.root, args, need_upstream=True, need_trunk=False,
                      strict_mirror=False, upstream=name)
    step("names", "-", f"upstream={ctx.up()}  mirror={ctx.mirror}  trunk={ctx.trunk}")

    pairs = [(key, getattr(ctx, key)) for key in ("trunk", "mirror")
             if getattr(args, key, None)]
    if getattr(args, "upstream", None):
        # a repo with more than one non-origin remote cannot infer it: without this every
        # later subcommand would exit 2 asking for the flag again
        pairs.append(("upstream", ctx.upstream))
    if pairs:
        write_config_keys(ctx, pairs)

    target = rev(ctx.root, ctx.up())
    if not target:
        raise Fail(f"`{ctx.up()}` does not resolve after the fetch: is `{name}` the right remote?")
    step("target", f"git rev-parse {sh_arg(ctx.up())}", short(target))

    setup_mirror(ctx, target)
    setup_trunk(ctx, target)
    setup_push_url(ctx)
    setup_hook(ctx, bool(getattr(args, "force", False)))
    setup_git_config(ctx)
    platform_report(ctx)
    setup_template(ctx)
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

COMMANDS = {
    "status": cmd_status,
    "check": cmd_check,
    "sync": cmd_sync,
    "ship": cmd_ship,
    "land": cmd_land,
    "setup": cmd_setup,
}


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    # SUPPRESS keeps a subparser from clobbering a flag given before the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", dest="dir", default=argparse.SUPPRESS, metavar="DIR",
                        help="repository directory (default: cwd)")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="report what would happen; move no branch, push nothing, "
                             "write no config (it does fetch, and simulates the merge)")
    common.add_argument("--force", action="store_true", default=argparse.SUPPRESS,
                        help="`sync`: recreate an existing sync branch; `setup`: replace a "
                             "foreign pre-push hook. No effect on status, check or ship; "
                             "`land --force` fast-forwards a landing the tool cannot verify")

    p = argparse.ArgumentParser(prog="forkflow", parents=[common],
                                description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="`forkflow.py --test` runs the embedded test suite.")
    sub = p.add_subparsers(dest="cmd", metavar="{status,check,sync,ship,land,setup}")

    s = sub.add_parser("status", parents=[common], help="where mirror, trunk and upstream stand")
    s.add_argument("--fetch", action="store_true", help="refresh remote-tracking refs first")
    s.add_argument("--offline", action="store_true",
                   help="no network at all: skip asking the upstream server for its tip")

    sub.add_parser("check", parents=[common], help="preflight invariants (gate, trunk tip)")

    sy = sub.add_parser("sync", parents=[common], help="advance the mirror and merge it into the trunk")
    sy.add_argument("--continue", dest="cont", action="store_true",
                    help="resume after resolving merge conflicts")
    sy.add_argument("--mr", action="store_true", help="run the merge-request command")
    sy.add_argument("--merge", action="store_true",
                    help="open the merge request and merge it; needs merge = \"self\" "
                         "in " + CONFIG_FILE)
    sy.add_argument("--title", help="merge-request title")

    sh = sub.add_parser("ship", parents=[common], help="squash a feature branch onto the trunk's tip")
    sh.add_argument("--continue", dest="cont", action="store_true",
                    help="resume after `git rebase --continue`")
    sh.add_argument("--mr", action="store_true", help="run the merge-request command")
    sh.add_argument("--merge", action="store_true",
                    help="open the merge request and merge it; needs merge = \"self\" "
                         "in " + CONFIG_FILE)
    sh.add_argument("--title", help="merge-request title")
    sh.add_argument("--message-file", help="file with the squashed commit message")

    sub.add_parser("land", parents=[common],
                   help="the MR is merged: fast-forward the local trunk and delete the branch")

    st = sub.add_parser("setup", parents=[common], help="make the rules mechanical in this clone")
    st.add_argument("--upstream", metavar="NAME", help="remote name of the original project")
    st.add_argument("--upstream-url", metavar="URL", help="add the upstream remote with this URL")
    st.add_argument("--trunk", metavar="NAME", help=f"trunk branch (default: {DEFAULT_TRUNK})")
    st.add_argument("--mirror", metavar="NAME", help="mirror branch (default: upstream's branch)")

    args = p.parse_args(list(argv))
    if not getattr(args, "cmd", None):
        p.print_usage(sys.stderr)
        raise Fail("no subcommand given; try `forkflow.py status`")
    for name, default in (("dir", "."), ("dry_run", False), ("force", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if getattr(args, "merge", False):
        args.mr = True                   # merging a merge request means opening it first
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["--test"]:       # only as the first word: `ship --title --test` is a title
            run_tests()                  # exits through SystemExit, which this does not catch
        args = parse_args(argv)
        return COMMANDS[args.cmd](args)
    except Fail as exc:
        print(f"forkflow: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


# --------------------------------------------------------------------------- #
# embedded tests
# --------------------------------------------------------------------------- #

def run_tests() -> None:
    """Run the embedded unit tests (`forkflow.py --test`) and exit."""
    import contextlib
    import io
    import unittest
    from unittest import mock

    # ------------------------------------------------------------------- #
    # harness
    # ------------------------------------------------------------------- #

    def sh(*args: str, cwd: Optional[str] = None, check: bool = True) -> str:
        p = subprocess.run(list(args), cwd=cwd, capture_output=True)
        if check and p.returncode != 0:
            raise AssertionError("command failed: %s\n%s"
                                 % (" ".join(args), p.stderr.decode("utf-8", "replace")))
        return p.stdout.decode("utf-8", "replace").strip()

    @contextlib.contextmanager
    def isolated_env(tmp):
        """Make git behave the same on every machine, then restore the environment."""
        saved = dict(os.environ)
        gitconfig = os.path.join(tmp, "gitconfig")
        with open(gitconfig, "w"):
            pass
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG",
                    "GIT_OBJECT_DIRECTORY", "XDG_CONFIG_HOME"):
            os.environ.pop(key, None)
        tmpdir = os.path.join(tmp, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        os.environ.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": gitconfig,
            "HOME": tmp,
            "TMPDIR": tmpdir,            # write_temp writes here, not into the developer's /tmp
            "GIT_AUTHOR_NAME": "forkflow tests",
            "GIT_AUTHOR_EMAIL": "tests@example.invalid",
            "GIT_COMMITTER_NAME": "forkflow tests",
            "GIT_COMMITTER_EMAIL": "tests@example.invalid",
            "GIT_EDITOR": "true",
        })
        saved_tempdir = tempfile.tempdir      # gettempdir() caches: TMPDIR alone is too late
        tempfile.tempdir = tmpdir
        try:
            yield
        finally:
            tempfile.tempdir = saved_tempdir
            os.environ.clear()
            os.environ.update(saved)

    def write(root: str, path: str, text) -> str:
        """Text, or bytes for the binary cases the both-sides table has to flag."""
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb" if isinstance(text, bytes) else "w") as fh:
            fh.write(text)
        return full

    def identity(repo: str) -> None:
        sh("git", "config", "user.name", "forkflow tests", cwd=repo)
        sh("git", "config", "user.email", "tests@example.invalid", cwd=repo)
        sh("git", "config", "commit.gpgsign", "false", cwd=repo)

    def scaffold(tmp: str) -> str:
        """bare upstream (seeded through a work clone) + bare origin cloned from it + fork/."""
        up_git = os.path.join(tmp, "upstream.git")
        origin_git = os.path.join(tmp, "origin.git")
        seed = os.path.join(tmp, "seed")
        fork = os.path.join(tmp, "fork")
        sh("git", "init", "--bare", "-b", "main", up_git)
        sh("git", "init", "-b", "main", seed)
        identity(seed)
        write(seed, "README.md", "# project\n")
        write(seed, "src/app.py", "def main():\n    return 1\n")
        write(seed, "shared.tf", 'resource "null_resource" "a" {\n  count = 1\n}\n')
        sh("git", "add", "-A", cwd=seed)
        sh("git", "commit", "-m", "initial", cwd=seed)
        write(seed, "src/app.py", "def main():\n    return 1\n\n\ndef helper():\n    return 2\n")
        sh("git", "commit", "-am", "add helper", cwd=seed)
        sh("git", "remote", "add", "origin", up_git, cwd=seed)
        sh("git", "push", "-u", "origin", "main", cwd=seed)
        sh("git", "--git-dir=" + up_git, "symbolic-ref", "HEAD", "refs/heads/main")
        sh("git", "clone", "--bare", up_git, origin_git)
        sh("git", "clone", origin_git, fork)
        identity(fork)
        sh("git", "remote", "add", "upstream", up_git, cwd=fork)
        sh("git", "fetch", "upstream", cwd=fork)
        sh("git", "remote", "set-head", "upstream", "-a", cwd=fork)
        return fork

    def make_fork(tmp: str, trunk: str = "develop", mirror: str = "main",
                  config: Optional[str] = None) -> str:
        fork = scaffold(tmp)
        origin_git = os.path.join(tmp, "origin.git")
        sh("git", "branch", "--no-track", trunk, "main", cwd=fork)
        sh("git", "switch", trunk, cwd=fork)
        if config is not None:
            write(fork, CONFIG_FILE, config)
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-m", "add " + CONFIG_FILE, cwd=fork)
        sh("git", "push", "origin", "%s:refs/heads/%s" % (trunk, trunk), cwd=fork)
        sh("git", "--git-dir=" + origin_git, "symbolic-ref", "HEAD", "refs/heads/" + trunk)
        if mirror != "main":
            sh("git", "branch", "-m", "main", mirror, cwd=fork)
            sh("git", "push", "origin", "%s:refs/heads/%s" % (mirror, mirror), cwd=fork)
            sh("git", "push", "origin", ":refs/heads/main", cwd=fork)
        sh("git", "remote", "set-head", "origin", "-a", cwd=fork)
        sh("git", "fetch", "--prune", "origin", cwd=fork)
        return fork

    def make_fresh_fork(tmp: str) -> str:
        """The just-forked case: only `main` on origin, no trunk anywhere."""
        return scaffold(tmp)

    def commit_upstream(tmp: str, path: str, content: str, message: str = "upstream change") -> str:
        seed = os.path.join(tmp, "seed")
        sh("git", "fetch", "origin", cwd=seed)
        sh("git", "checkout", "-B", "main", "origin/main", cwd=seed)
        write(seed, path, content)
        sh("git", "add", "-A", cwd=seed)
        sh("git", "commit", "-m", message, cwd=seed)
        sh("git", "push", "origin", "main", cwd=seed)
        return sh("git", "rev-parse", "HEAD", cwd=seed)

    def commit_fork(fork: str, path: str, content: str, message: str = "fork change",
                    push: bool = False) -> str:
        write(fork, path, content)
        sh("git", "add", "-A", cwd=fork)
        sh("git", "commit", "-m", message, cwd=fork)
        branch = sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork)
        if push:
            sh("git", "push", "origin", "%s:refs/heads/%s" % (branch, branch), cwd=fork)
        return sh("git", "rev-parse", "HEAD", cwd=fork)

    def push_upstream_into_origin(tmp: str, branch: str = "main") -> None:
        """Move origin/<branch> forward without touching the fork (test setup only)."""
        sh("git", "push", os.path.join(tmp, "origin.git"), "main:refs/heads/" + branch,
           cwd=os.path.join(tmp, "seed"))

    def second_clone_commit(tmp: str, branch: str = "develop", path: str = "ours/other.txt",
                            content: str = "from another clone\n") -> str:
        """A commit that reaches origin behind the fork's back - only a fetch can find it."""
        other = os.path.join(tmp, "other")
        if not os.path.exists(other):
            sh("git", "clone", os.path.join(tmp, "origin.git"), other)
            identity(other)
        sh("git", "fetch", "origin", cwd=other)
        sh("git", "checkout", "-B", branch, "origin/" + branch, cwd=other)
        write(other, path, content)
        sh("git", "add", "-A", cwd=other)
        sh("git", "commit", "-m", "from another clone", cwd=other)
        sh("git", "push", "origin", branch, cwd=other)
        return sh("git", "rev-parse", "HEAD", cwd=other)

    def checked_out(fork: str) -> str:
        return sh("git", "symbolic-ref", "-q", "--short", "HEAD", cwd=fork, check=False)

    def origin_sha(fork: str, branch: str) -> str:
        out = sh("git", "ls-remote", "origin", "refs/heads/" + branch, cwd=fork)
        return out.split("\t")[0] if out else ""

    def fake_tool(bin_dir: str, name: str, script: str) -> str:
        os.makedirs(bin_dir, exist_ok=True)
        path = os.path.join(bin_dir, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + script)
        os.chmod(path, 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        return path

    def reject_pushes(tmp: str, pattern: str = "") -> None:
        """A pre-receive hook in the bare origin: deterministic push rejection."""
        hooks = os.path.join(tmp, "origin.git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        body = ("#!/bin/sh\n"
                "while read old new ref; do\n"
                "  case \"$ref\" in %s) echo \"rejected by test hook: $ref\" >&2; exit 1;; esac\n"
                "done\nexit 0\n" % (pattern or "*"))
        path = os.path.join(hooks, "pre-receive")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def delete_after_receive(tmp: str, pattern: str = "refs/heads/backup/*") -> None:
        """A post-receive hook in the bare origin that drops the ref it has just accepted:
        the push reports success, so only the ls-remote confirmation can catch it."""
        hooks = os.path.join(tmp, "origin.git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        body = ("#!/bin/sh\n"
                "while read old new ref; do\n"
                "  case \"$ref\" in %s) git update-ref -d \"$ref\" \"$new\";; esac\n"
                "done\nexit 0\n" % pattern)
        path = os.path.join(hooks, "post-receive")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def commit_after_receive(tmp: str, pattern: str = "refs/heads/feat/*") -> None:
        """A post-receive hook in the bare origin that puts one more commit on the branch it
        has just accepted - a teammate pushing between the push and the merge. The push
        reports success; only the head-commit guard at merge time can catch it."""
        hooks = os.path.join(tmp, "origin.git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        body = ("#!/bin/sh\n"
                "while read old new ref; do\n"
                "  case \"$ref\" in %s) git update-ref \"$ref\" "
                "\"$(git commit-tree \"$new^{tree}\" -p \"$new\" -m teammate)\" \"$new\";; esac\n"
                "done\nexit 0\n" % pattern)
        path = os.path.join(hooks, "post-receive")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    MERGING_TOOL_URL = {"glab": "https://example.invalid/-/merge_requests/1",
                        "gh": "https://example.invalid/pull/1"}

    def merging_tool(tmp: str, name: str, trunk: str = "develop") -> None:
        """A glab/gh on PATH that opens AND merges: the platform simulated, not faked away.

        Its argv goes one argument per line into `<name>-<subcommand>-argv.txt` - `create`
        and `merge` get separate logs, because the one binary is run twice under `--merge`
        and a single truncating log would lose the create argv and the body copy
        (`<name>-body-copy.md`). On `create` it prints a URL in the platform's shape. On
        `merge` it reads the sha from its own `--sha` / `--match-head-commit`, compares it
        with the tip of the source branch in the bare origin and exits 1 with "head
        mismatch" when they differ - so the head-commit guard is tested as behaviour, not
        as an argv string - then merges the way the platform would: a fast-forward of the
        bare origin's trunk (`update-ref`) for glab and for gh `--merge` (a sync's tip is
        the merge commit), and for gh `--rebase` a cherry-pick of the commit in a temp
        clone of the origin, pushed back - a one-commit rebase, new SHA, same patch; a bare
        repository cannot rebase. `FORKFLOW_FAKE_FAIL=1` in the environment makes the merge
        exit 1 with a stderr line and move nothing."""
        origin_git = os.path.join(tmp, "origin.git")
        script = (
            'sub="$2"\n'
            'for a in "$@"; do echo "$a"; done > {logs}/{name}-"$sub"-argv.txt\n'
            'case "$sub" in\n'
            'create)\n'
            '  for a in "$@"; do [ -f "$a" ] && cp "$a" {body}; done\n'
            '  echo "Creating merge request"\n'
            '  echo {url}\n'
            '  exit 0;;\n'
            'merge)\n'
            '  if [ -n "$FORKFLOW_FAKE_FAIL" ]; then\n'
            '    echo "{name}: merge refused (FORKFLOW_FAKE_FAIL)" >&2; exit 1\n'
            '  fi\n'
            '  branch="$3"; want=""; rebase=0; prev=""\n'
            '  for a in "$@"; do\n'
            '    case "$prev" in --sha|--match-head-commit) want="$a";; esac\n'
            '    [ "$a" = --rebase ] && rebase=1\n'
            '    prev="$a"\n'
            '  done\n'
            '  tip=$(git --git-dir={origin} rev-parse "refs/heads/$branch") || exit 1\n'
            '  if [ "$want" != "$tip" ]; then\n'
            '    echo "{name}: head mismatch: $branch is at $tip, not $want" >&2; exit 1\n'
            '  fi\n'
            '  if [ "$rebase" = 1 ]; then\n'
            '    clone={clone}; rm -rf "$clone"\n'
            '    git clone -q -b {trunk} {origin} "$clone" >/dev/null 2>&1 || exit 1\n'
            '    git -C "$clone" config commit.gpgsign false\n'
            '    GIT_COMMITTER_NAME="{name} platform" GIT_COMMITTER_EMAIL=noreply@example.invalid\n'
            '    export GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL\n'
            '    git -C "$clone" cherry-pick "$tip" >/dev/null 2>&1 || {{\n'
            '      echo "{name}: rebase and merge failed" >&2; exit 1; }}\n'
            '    git -C "$clone" push -q origin HEAD:{trunk} >/dev/null 2>&1 || exit 1\n'
            '  else\n'
            '    git --git-dir={origin} update-ref refs/heads/{trunk} "$tip" || exit 1\n'
            '  fi\n'
            '  echo "merged $branch into {trunk}"\n'
            '  exit 0;;\n'
            'esac\n'
            'echo "{name}: unexpected subcommand $sub" >&2; exit 2\n'
        ).format(name=name, logs=shlex.quote(tmp), url=shlex.quote(MERGING_TOOL_URL[name]),
                 body=shlex.quote(os.path.join(tmp, name + "-body-copy.md")),
                 origin=shlex.quote(origin_git), trunk=shlex.quote(trunk),
                 clone=shlex.quote(os.path.join(tmp, "platform-clone")))
        fake_tool(os.path.join(tmp, "bin"), name, script)

    def tool_argv(tmp: str, name: str, sub: str) -> list:
        """What `merging_tool` recorded for `<name> <mr|pr> <sub>`, [] when it never ran."""
        path = os.path.join(tmp, "%s-%s-argv.txt" % (name, sub))
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [ln.rstrip("\n") for ln in fh]

    def local_branches(fork: str) -> str:
        return sh("git", "for-each-ref", "--format=%(refname)", "refs/heads/", cwd=fork)

    # One UTC date for the whole run: the code stamps names when it runs, the assertions when
    # they compare, and a suite crossing UTC midnight would otherwise fail a dozen cases.
    # Only the date is frozen - backup names keep their live HH:MM:SS, so they stay unique.
    UTC_DATE = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    real_utc_stamp = utc_stamp

    def frozen_utc_stamp(fmt: str = "%Y%m%d") -> str:
        return UTC_DATE if fmt == "%Y%m%d" else real_utc_stamp(fmt)

    def sync_branch_name(remote: str = "upstream") -> str:
        return "%s%s-%s" % (DEFAULT_SYNC_PREFIX, remote, UTC_DATE)

    def next_utc_second() -> None:
        """Wait for the backup-name clock to tick. `backup/<UTC HH:MM:SS>-<reason>` is refused
        when that name already exists at another commit, so a test that runs two `sync`s or
        two `ship`s of one branch has to let the second turn over."""
        start = real_utc_stamp("%Y%m%d-%H%M%S")
        while real_utc_stamp("%Y%m%d-%H%M%S") == start:
            time.sleep(0.05)

    def capture(fn, *a, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            value = fn(*a, **kw)
        return value, out.getvalue(), err.getvalue()

    def run(*argv: str):
        return capture(main, list(argv))

    def ctx_for(fork: str, need_upstream: bool = True, need_trunk: bool = True,
                strict_mirror: bool = True, **ns) -> Ctx:
        ns.setdefault("dry_run", False)
        args = argparse.Namespace(**ns)
        return resolve_ctx(fork, args, need_upstream=need_upstream,
                           need_trunk=need_trunk, strict_mirror=strict_mirror)

    has_tomllib = sys.version_info >= (3, 11)
    needs_tomllib = unittest.skipUnless(has_tomllib, "reading .forkflow.toml needs Python 3.11+")
    needs_merge_tree = unittest.skipUnless(git_version() >= MERGE_TREE_GIT,
                                           "the merge simulation needs git 2.38+")

    class Base(unittest.TestCase):
        def setUp(self):
            td = tempfile.TemporaryDirectory()
            self.addCleanup(td.cleanup)
            self.tmp = os.path.realpath(td.name)
            stack = contextlib.ExitStack()
            stack.enter_context(isolated_env(self.tmp))
            stack.enter_context(mock.patch.object(sys.modules[__name__], "utc_stamp",
                                                  frozen_utc_stamp))
            self.addCleanup(stack.close)

    # ------------------------------------------------------------------- #
    # pure helpers
    # ------------------------------------------------------------------- #

    class TestDetectPlatform(unittest.TestCase):
        def test_gitlab(self):
            self.assertEqual(detect_platform("git@gitlab.com:group/proj.git"), "gitlab")
            self.assertEqual(detect_platform("https://gitlab.example.com/group/proj.git"), "gitlab")
            self.assertEqual(detect_platform("ssh://git@gitlab.com:2222/group/proj.git"), "gitlab")

        def test_github(self):
            self.assertEqual(detect_platform("git@github.com:owner/repo.git"), "github")
            self.assertEqual(detect_platform("https://github.com/owner/repo"), "github")
            self.assertEqual(detect_platform("ssh://git@github.com/owner/repo.git"), "github")

        def test_unknown(self):
            self.assertEqual(detect_platform("/tmp/x/origin.git"), "unknown")
            self.assertEqual(detect_platform("https://git.example.org/x.git"), "unknown")
            self.assertEqual(detect_platform(""), "unknown")

    class TestForgePath(unittest.TestCase):
        """Which project the platform report addresses.

        gh's `{owner}/{repo}` and glab's `:fullpath` are resolved by the tool from the
        repository it is run in, and both answer with the remote named `upstream` when there
        is one - the original project, which `setup` itself adds. The fork's own project has
        to come out of its origin URL instead."""

        def test_every_spelling_of_one_fork_names_the_same_project(self):
            for url in ("git@github.com:acme/widget.git",
                        "https://github.com/acme/widget",
                        "https://git@github.com/acme/widget.git",
                        "ssh://git@github.com:22/acme/widget.git",
                        "https://github.com:443/acme/widget/"):
                self.assertEqual(forge_path(url), "acme/widget", url)

        def test_a_subgroup_keeps_every_segment(self):
            """A self-hosted GitLab project lives under any number of groups: dropping one
            addresses a different project, or none."""
            self.assertEqual(forge_path("git@gitlab.example.com:group/sub/proj.git"),
                             "group/sub/proj")
            self.assertEqual(forge_path("https://gitlab.example.com/g/s/deep/proj.git"),
                             "g/s/deep/proj")

        def test_the_spelling_is_kept(self):
            """`repo_id` folds the case to decide whether two URLs are one repository; this
            is pasted into a command instead. GitHub answers a differently-cased repository
            with a redirect, and a PATCH or a PUT does not follow one."""
            self.assertEqual(forge_path("git@github.com:Acme/Widget.git"), "Acme/Widget")
            self.assertEqual(forge_path("https://GitHub.com/Acme/Widget"), "Acme/Widget")

        def test_a_url_that_names_no_project_gives_nothing(self):
            """Nothing is better than a guess: the report says so and prints no command."""
            for url in ("", "/tmp/x/origin.git", "file:///srv/repo.git",
                        "file://localhost/srv/repo.git", "../repo.git",
                        "https://github.com/", "git@github.com:", "https://github.com/owner",
                        "git@github.com:owner/.git"):
                self.assertEqual(forge_path(url), "", url)

    class TestGitVersion(unittest.TestCase):
        def test_parse(self):
            self.assertEqual(_parse_version("git version 2.39.5 (Apple Git-154)"), (2, 39))
            self.assertEqual(_parse_version("git version 2.42.0.windows.1"), (2, 42))
            self.assertEqual(_parse_version("nonsense"), (0, 0))

        def test_real_git(self):
            major, minor = git_version()
            self.assertGreaterEqual(major, 2)
            self.assertIsInstance(minor, int)

    class TestLoadConfig(Base):
        def test_absent(self):
            self.assertEqual(load_config(self.tmp), {})

        @needs_tomllib
        def test_valid(self):
            write(self.tmp, CONFIG_FILE, 'trunk = "trunk"\ngate = ["true"]\n')
            cfg = load_config(self.tmp)
            self.assertEqual(cfg["trunk"], "trunk")
            self.assertEqual(cfg["gate"], ["true"])

        @needs_tomllib
        def test_invalid_is_fatal(self):
            write(self.tmp, CONFIG_FILE, "trunk = \n")
            with self.assertRaises(Fail) as cm:
                load_config(self.tmp)
            self.assertEqual(cm.exception.code, 2)

        def test_a_comment_only_file_is_no_config_at_all_without_tomllib(self):
            """`setup` used to leave a commented template behind on any Python; a file that
            configures nothing must not brick every subcommand on 3.9/3.10."""
            write(self.tmp, CONFIG_FILE, "# trunk = \"develop\"\n\n#  gate = []\n")
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                self.assertEqual(load_config(self.tmp), {})

        def test_a_file_that_does_configure_something_is_still_fatal_without_tomllib(self):
            write(self.tmp, CONFIG_FILE, '# the trunk\ntrunk = "trunk"\n')
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                with self.assertRaises(Fail) as cm:
                    load_config(self.tmp)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("3.11", str(cm.exception))

        @needs_tomllib
        def test_a_value_of_the_wrong_type_is_fatal(self):
            """Every value ends up on a git command line: a wrong type would be a traceback."""
            for text in ('trunk = 123\n', 'mirror = ["a"]\n', 'sync_prefix = true\n',
                         'upstream = 1.5\n', 'backup_prefix = 7\n', 'upstream_branch = []\n'):
                write(self.tmp, CONFIG_FILE, text)
                with self.assertRaises(Fail) as cm:
                    load_config(self.tmp)
                self.assertEqual(cm.exception.code, 2, text)
                self.assertIn("must be a string", str(cm.exception))

        @needs_tomllib
        def test_a_file_that_cannot_be_read_is_fatal(self):
            path = write(self.tmp, CONFIG_FILE, 'trunk = "trunk"\n')
            os.chmod(path, 0)
            self.addCleanup(os.chmod, path, 0o644)
            if os.access(path, os.R_OK):                 # running as root: nothing to prove
                self.skipTest("the file is readable anyway")
            with self.assertRaises(Fail) as cm:
                load_config(self.tmp)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("cannot be read", str(cm.exception))

        def test_without_tomllib_is_fatal(self):
            write(self.tmp, CONFIG_FILE, 'trunk = "trunk"\n')
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                with self.assertRaises(Fail) as cm:
                    load_config(self.tmp)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("3.11", str(cm.exception))

        @needs_tomllib
        def test_merge_is_manual_or_self_and_nothing_else(self):
            """`merge` decides whether `--merge` may merge at all: a spelling nobody defined
            is refused outright rather than read as one of the two."""
            for text, expected in (('merge = "self"\n', "self"), ('merge = "manual"\n', "manual")):
                write(self.tmp, CONFIG_FILE, text)
                self.assertEqual(load_config(self.tmp)["merge"], expected, text)
            write(self.tmp, CONFIG_FILE, 'trunk = "develop"\n')
            self.assertNotIn("merge", load_config(self.tmp))              # absent: the default
            for text in ('merge = "auto"\n', 'merge = "Self"\n', 'merge = ""\n'):
                write(self.tmp, CONFIG_FILE, text)
                with self.assertRaises(Fail) as cm:
                    load_config(self.tmp)
                self.assertEqual(cm.exception.code, 2, text)
                self.assertIn('`merge` must be "manual" or "self"', str(cm.exception))
            write(self.tmp, CONFIG_FILE, "merge = true\n")
            with self.assertRaises(Fail) as cm:
                load_config(self.tmp)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("`merge` must be a string", str(cm.exception))

    # ------------------------------------------------------------------- #
    # resolve_ctx
    # ------------------------------------------------------------------- #

    class TestResolveCtx(Base):
        def test_defaults(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            self.assertEqual((ctx.trunk, ctx.mirror), ("develop", "main"))
            self.assertEqual(ctx.upstream, "upstream")
            self.assertEqual(ctx.up(), "upstream/main")
            self.assertEqual(ctx.platform, "unknown")
            self.assertFalse(mirror_divergence(ctx)[0])

        @needs_tomllib
        def test_names_from_config(self):
            fork = make_fork(self.tmp, trunk="trunk", mirror="upstream-main",
                             config='trunk = "trunk"\nmirror = "upstream-main"\n')
            ctx = ctx_for(fork)
            self.assertEqual((ctx.trunk, ctx.mirror), ("trunk", "upstream-main"))
            self.assertEqual(ctx.up(), "upstream/main")

        def test_merge_defaults_to_manual(self):
            """No config, no `merge` key: the fork's merge requests are merged by hand."""
            self.assertEqual(ctx_for(make_fork(self.tmp)).merge, "manual")

        @needs_tomllib
        def test_merge_is_read_from_the_config(self):
            fork = make_fork(self.tmp, config='merge = "self"\n')
            self.assertEqual(ctx_for(fork).merge, "self")
            write(fork, CONFIG_FILE, 'merge = "manual"\n')       # the working tree decides
            self.assertEqual(ctx_for(fork).merge, "manual")

        def test_upstream_head_master_fallback(self):
            fork = make_fork(self.tmp)
            sha = sh("git", "rev-parse", "refs/remotes/upstream/main", cwd=fork)
            sh("git", "update-ref", "refs/remotes/upstream/master", sha, cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=fork)
            sh("git", "symbolic-ref", "-d", "refs/remotes/upstream/HEAD", cwd=fork)
            ctx = ctx_for(fork, strict_mirror=False)
            self.assertEqual(ctx.upstream_branch, "master")
            self.assertEqual(ctx.up(), "upstream/master")

        def test_missing_upstream(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "remove", "upstream", cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("--upstream-url", str(cm.exception))
            ctx = ctx_for(fork, need_upstream=False, strict_mirror=False)
            self.assertEqual(ctx.upstream, "upstream")

        def test_status_without_upstream_exits_2(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "remove", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 2)
            self.assertIn("--upstream-url", err)

        def test_trunk_equals_mirror(self):
            fork = make_fork(self.tmp)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork, trunk="main")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("cannot be the same branch", str(cm.exception))

        def test_diverged_mirror(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours.txt", "ours\n", "work on the mirror")
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("Adopting forkflow", str(cm.exception))
            ctx = ctx_for(fork, strict_mirror=False)
            self.assertTrue(mirror_divergence(ctx)[0])

        def test_an_origin_mirror_ahead_of_an_unfetched_upstream_is_not_divergence(self):
            """A teammate's sync legitimately puts `origin/<mirror>` ahead of an `upstream/*`
            ref this clone has not refreshed. Only the local mirror is judged, and only that
            keeps normal work from being blocked with the migration hint."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            push_upstream_into_origin(self.tmp, "main")     # the teammate's sync pushed it
            sh("git", "fetch", "origin", cwd=fork)          # ... and we fetched only origin
            self.assertNotEqual(rev(fork, "refs/remotes/origin/main"),
                                rev(fork, "refs/remotes/upstream/main"))
            self.assertFalse(mirror_divergence(ctx_for(fork))[0])

            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertNotIn("Adopting forkflow", out)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n", "ours: a")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err)

        def synced_by_a_teammate(self) -> str:
            """A fork where a teammate's sync advanced `origin/<mirror>` and this clone's
            `git fetch origin` fast-forwarded the local mirror onto it - without fetching
            `upstream`, which is what every one of these commands does for itself."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            push_upstream_into_origin(self.tmp, "main")
            sh("git", "fetch", "origin", cwd=fork)
            sh("git", "switch", "main", cwd=fork)
            sh("git", "merge", "--ff-only", "origin/main", cwd=fork)
            sh("git", "switch", "develop", cwd=fork)
            self.assertNotEqual(rev(fork, "refs/heads/main"),
                                rev(fork, "refs/remotes/upstream/main"))
            return fork

        def test_a_local_mirror_fast_forwarded_from_origin_is_not_divergence(self):
            """The local mirror is ahead of `upstream/*` as last fetched, and every commit it
            carries is on `origin/<mirror>`. `sync`, `ship` and `check` fetch upstream
            themselves; refusing before that fetch fails them all in an ordinary team state."""
            fork = self.synced_by_a_teammate()
            ctx = ctx_for(fork)                       # strict: no longer a hard failure
            self.assertTrue(mirror_divergence(ctx)[1])
            self.assertTrue(mirror_from_origin(ctx))
            for argv in (("status",), ("check",)):
                code, out, err = run("-C", fork, *argv)
                self.assertEqual(code, 0, " ".join(argv) + ": " + err + out)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n", "ours: a")
            code, out, err = run("-C", fork, "ship", "--dry-run")   # fetches origin only
            self.assertEqual(code, 0, err + out)
            code, out, err = run("-C", fork, "sync", "--dry-run")   # fetches upstream itself
            self.assertEqual(code, 0, err + out)

        def test_a_commit_on_top_of_a_teammates_mirror_is_still_divergence(self):
            """The tolerance is exactly "everything the mirror carries is on origin's mirror".
            One commit of our own on top of it is rule 6 again, and still exit 2."""
            fork = self.synced_by_a_teammate()
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours.txt", "ours\n", "work on the mirror")
            sh("git", "switch", "develop", cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("Adopting forkflow", str(cm.exception))
            self.assertEqual(run("-C", fork, "check")[0], 2)
            self.assertFalse(mirror_from_origin(ctx_for(fork, strict_mirror=False)))

        def test_unfetched_upstream(self):
            fork = make_fork(self.tmp)
            sh("git", "symbolic-ref", "-d", "refs/remotes/upstream/HEAD", cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("not fetched", str(cm.exception))
            self.assertFalse(mirror_divergence(ctx_for(fork, strict_mirror=False))[0])

        def test_trunk_missing_on_origin(self):
            fork = make_fresh_fork(self.tmp)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("forkflow setup", str(cm.exception))
            ctx = ctx_for(fork, need_trunk=False)
            self.assertEqual(ctx.trunk, "develop")

        def test_outside_a_repository_is_exit_2(self):
            plain = os.path.join(self.tmp, "plain")
            os.makedirs(plain)
            with self.assertRaises(Fail) as cm:
                ctx_for(plain)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("not inside a git repository", str(cm.exception))

        def test_no_origin_remote_is_exit_2(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "remove", "origin", cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("no `origin` remote", str(cm.exception))

        def test_several_non_origin_remotes_need_the_upstream_flag(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "add", "vendor", os.path.join(self.tmp, "upstream.git"), cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("several non-origin remotes (upstream, vendor)", str(cm.exception))
            self.assertEqual(ctx_for(fork, upstream="upstream").upstream, "upstream")

        def test_origin_as_the_upstream_is_refused(self):
            fork = make_fork(self.tmp)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork, upstream="origin")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("cannot be the same remote", str(cm.exception))

        def test_a_mirror_that_is_nowhere_is_exit_2(self):
            fork = make_fork(self.tmp)
            sh("git", "branch", "-D", "main", cwd=fork)
            sh("git", "push", "origin", ":refs/heads/main", cwd=fork)
            sh("git", "fetch", "--prune", "origin", cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("is in this clone neither as a branch nor as", str(cm.exception))
            # not "it is not on origin": this clone cannot know that (a narrowed refspec)
            self.assertIn("narrowed fetch refspec", str(cm.exception))
            self.assertFalse(mirror_divergence(ctx_for(fork, strict_mirror=False))[0])

        @needs_tomllib
        def test_the_prefix_and_upstream_keys_come_from_the_config(self):
            fork = make_fork(self.tmp, config='sync_prefix = "merge-up/"\n'
                                              'backup_prefix = "safety/"\n'
                                              'upstream = "up"\nupstream_branch = "main"\n')
            sh("git", "remote", "rename", "upstream", "up", cwd=fork)
            ctx = ctx_for(fork)
            self.assertEqual(ctx.sync_prefix, "merge-up/")
            self.assertEqual(ctx.backup_prefix, "safety/")
            self.assertEqual(ctx.upstream, "up")
            self.assertEqual(ctx.up(), "up/main")
            self.assertTrue(sync_branch(ctx).startswith("merge-up/up-"))
            self.assertTrue(backup_name(ctx, "pre-ship").startswith("safety/"))

        @needs_tomllib
        def test_upstream_branch_from_the_config_names_the_mirror(self):
            fork = make_fork(self.tmp, config='upstream_branch = "legacy"\n')
            sha = rev(fork, "refs/remotes/upstream/main")
            sh("git", "update-ref", "refs/remotes/upstream/legacy", sha, cwd=fork)
            sh("git", "branch", "--no-track", "legacy", sha, cwd=fork)
            ctx = ctx_for(fork)
            self.assertEqual(ctx.up(), "upstream/legacy")
            self.assertEqual(ctx.mirror, "legacy")   # the mirror defaults to upstream's branch

        @needs_tomllib
        def test_a_name_git_would_refuse_is_exit_2(self):
            fork = make_fork(self.tmp, config='trunk = "bad..name"\n')
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("check-ref-format", str(cm.exception))

        def test_the_pseudo_refs_are_refused_although_git_accepts_them(self):
            """`git check-ref-format refs/heads/HEAD` says yes. As a branch name it is a trap:
            `git rev-parse HEAD` is then the checked-out commit and `origin/HEAD` the remote's
            default branch, so the trunk `ship` must never push stops being the branch it
            names."""
            self.assertTrue(git_ok("check-ref-format", "refs/heads/HEAD"))
            for name in ("HEAD", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD", "AUTO_MERGE"):
                self.assertFalse(valid_branch_name(name), name)
            self.assertTrue(valid_branch_name("head"))       # only the names git resolves first
            self.assertTrue(valid_branch_name("HEAD/x"))     # no ambiguity below a directory

        @needs_tomllib
        def test_head_as_the_trunk_is_exit_2(self):
            fork = make_fork(self.tmp, config='trunk = "HEAD"\n')
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("check-ref-format", str(cm.exception))

        def test_an_origin_push_url_aimed_at_upstream_is_refused(self):
            """`git push origin` obeys `remote.origin.pushurl`, so an origin aliased onto the
            original project sends every push there - rule 1, under a name that looks safe."""
            fork = make_fork(self.tmp)
            up = sh("git", "remote", "get-url", "upstream", cwd=fork)
            sh("git", "config", "remote.origin.pushurl", up, cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("pushes to", str(cm.exception))
            self.assertIn("remote.origin.pushurl", str(cm.exception))

        def test_a_second_origin_push_url_aimed_at_upstream_is_refused(self):
            """`pushurl` is multi-valued and git pushes to every one of them."""
            fork = make_fork(self.tmp)
            up = sh("git", "remote", "get-url", "upstream", cwd=fork)
            origin = sh("git", "remote", "get-url", "origin", cwd=fork)
            sh("git", "config", "remote.origin.pushurl", origin, cwd=fork)
            sh("git", "config", "--add", "remote.origin.pushurl", up, cwd=fork)
            self.assertEqual(len(push_urls(fork, "origin")), 2)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertIn("pushes to", str(cm.exception))

        def test_an_ordinary_origin_push_url_is_left_alone(self):
            fork = make_fork(self.tmp)
            origin = sh("git", "remote", "get-url", "origin", cwd=fork)
            sh("git", "config", "remote.origin.pushurl", origin, cwd=fork)
            self.assertEqual(ctx_for(fork).origin, "origin")

        def test_repo_id_reads_the_spellings_of_one_repository_the_same(self):
            same = ["https://github.com/o/r.git", "https://git@github.com/o/r",
                    "git@github.com:o/r.git", "ssh://github.com/o/r/",
                    "https://GitHub.com/o/r"]
            self.assertEqual({repo_id(u) for u in same}, {"github.com/o/r"})
            self.assertNotEqual(repo_id("https://github.com/o/r"),
                                repo_id("https://github.com/o/fork"))
            self.assertEqual(repo_id("/tmp/Case/origin.git"), "/tmp/Case/origin")

        def test_repo_id_folds_the_case_and_the_default_port_of_a_hosted_url(self):
            """A forge resolves `Owner/Repo` case-insensitively and `:443` is `https`: both
            are live aliases onto the same repository, and rule 1 is about the repository."""
            cased = ["https://GitHub.com/Owner/Repo.git", "https://github.com/owner/repo",
                     "ssh://git@github.com:22/Owner/repo", "git@GITHUB.com:owner/Repo.git",
                     "https://github.com:443/owner/repo/"]
            self.assertEqual({repo_id(u) for u in cased}, {"github.com/owner/repo"})
            # a port that is not the scheme's default names another service, not an alias
            self.assertNotEqual(repo_id("https://github.com:8080/o/r"),
                                repo_id("https://github.com/o/r"))
            # a path is not case-folded the way a host is - `file://` spells one too
            self.assertEqual(repo_id("file:///tmp/Case/upstream.git"), "/tmp/Case/upstream")
            self.assertEqual(repo_id("/tmp/Case/upstream.git/"), "/tmp/Case/upstream")

        def test_an_origin_push_url_that_differs_only_in_case_is_refused(self):
            """The false negative this closes: a `pushurl` spelled with another case is an
            alias onto the original project, and `push()` would write to it."""
            fork = make_fork(self.tmp)
            sh("git", "remote", "set-url", "upstream", "https://github.com/o/r.git", cwd=fork)
            sh("git", "config", "remote.origin.pushurl",
               "https://GitHub.com/O/R", cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("pushes to", str(cm.exception))

    class TestShQuote(unittest.TestCase):
        """`sh_quote` is what keeps a branch name out of the shell's syntax: the names come
        from `.forkflow.toml`, a tracked file a sync can bring in from upstream, and they end
        up in the generated pre-push hook and in the fix commands `setup` tells a human to
        paste. `git check-ref-format` allows `;`, `$`, a backtick and `(`."""

        VALUES = ["plain", "with space", "it's", '"quoted"', "$HOME", "`id`", "$(id)",
                  "a;rm -rf /", "back\\slash", "two\nlines", "!bang", "a&b|c", "#hash"]

        def test_the_shell_reads_back_exactly_what_went_in(self):
            for value in self.VALUES:
                out = sh("sh", "-c", "printf %s " + sh_quote(value))
                self.assertEqual(out, value, "mangled: " + repr(value))

        def test_every_value_stays_one_word(self):
            for value in self.VALUES:
                out = sh("sh", "-c", "set -- " + sh_quote(value) + "; echo $#")
                self.assertEqual(out, "1", "split: " + repr(value))

        def test_it_is_always_quoted(self):
            self.assertEqual(sh_quote("plain"), "'plain'")
            self.assertEqual(sh_quote("it's"), "'it'\\''s'")

        def test_sh_arg_leaves_an_ordinary_name_readable(self):
            for value in ("main", "feat/x", "backup/20260101-000000-pre-ship",
                          "refs/heads/x:refs/heads/x", "origin/develop..HEAD", "--ff-only"):
                self.assertEqual(sh_arg(value), value)
            self.assertEqual(sh_arg("dev;id"), "'dev;id'")

    class TestPrintedCommandsAreQuoted(Base):
        """Every command this script prints may be pasted into a shell, by a human or by
        Claude. `git check-ref-format` accepts `;`, a backtick and `$(`, and the branch names
        come from `.forkflow.toml` - a tracked file a sync can bring in from upstream."""

        TRUNK = "dev;touch$(id)"
        PREFIX = "bk;$(id)/"

        def commands(self, out: str) -> list:
            """What each printed line offers to run: the `$ ...` of a `step()` line, and the
            rollback and fix instructions printed under one."""
            found = []
            for line in out.splitlines():
                if "  $ " in line:
                    found.append(line.split("  $ ", 1)[1].split("  -> ")[0])
                for marker in ("rollback: ", "fix: ", "may still exist: "):
                    if marker in line:
                        found.append(line.split(marker, 1)[1])
            return found

        def outside_quotes(self, cmd: str) -> str:
            """The parts of a command the shell reads as syntax rather than as text."""
            return "".join(p for i, p in enumerate(cmd.split("'")) if i % 2 == 0)

        def quoted(self, out: str, names: Sequence[str]) -> set:
            """Which of `names` a printed command carried - failing on any that is bare."""
            seen = set()
            for cmd in self.commands(out):
                for name in names:
                    if name in cmd:
                        seen.add(name)
                        self.assertNotIn(name, self.outside_quotes(cmd),
                                         "unquoted %r in: %s" % (name, cmd))
            return seen

        @needs_tomllib
        def test_no_command_any_run_prints_carries_a_bare_name(self):
            fork = make_fork(self.tmp, trunk=self.TRUNK,
                             config='trunk = "%s"\nbackup_prefix = "%s"\n'
                                    % (self.TRUNK, self.PREFIX))
            commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "checkout", "-b", "feat/x", self.TRUNK, cwd=fork)
            commit_fork(fork, "ours/f.txt", "ours\n", "ours: step")
            # a `pending` entry nothing landed, so `land --force --dry-run` prints the
            # checkout and the fast-forward it would run, with the trunk's name in both
            write_state(ctx_for(fork, strict_mirror=False), "pending",
                        {"kind": "ship", "branch": "feat/x", "commit": rev(fork, "feat/x"),
                         "base": origin_sha(fork, self.TRUNK), "mr": ""})

            names, seen = (self.TRUNK, self.PREFIX), set()
            for argv in (("status", "--fetch"), ("check",), ("sync", "--dry-run"),
                         ("ship", "--dry-run"), ("land", "--force", "--dry-run"),
                         ("setup", "--dry-run")):
                code, out, err = run("-C", fork, *argv)
                self.assertEqual(code, 0, " ".join(argv) + ": " + err + out)
                self.assertTrue(self.commands(out), "no commands printed by " + argv[0])
                seen |= self.quoted(out, names)
            # and both names really did reach one, so the check above had something to judge
            self.assertEqual(seen, set(names))

    class TestState(Base):
        """`--continue` force-pushes, and rule 4 allows that only behind the backup the run it
        resumes actually made - so which backup is its own has to be recorded, not guessed."""

        def test_round_trip_and_removal_of_one_kind_at_a_time(self):
            ctx = ctx_for(make_fork(self.tmp))
            self.assertEqual(read_state(ctx), {})
            self.assertEqual(resumable(ctx, "ship", "feat/x"), {})

            write_state(ctx, "ship", {"branch": "feat/x", "backup": "b1", "lease": "abc"})
            write_state(ctx, "sync", {"branch": "sync/x", "backup": "b2"})
            self.assertEqual(resumable(ctx, "ship", "feat/x")["backup"], "b1")
            self.assertEqual(resumable(ctx, "ship", "feat/x")["lease"], "abc")
            self.assertEqual(resumable(ctx, "sync", "sync/x")["backup"], "b2")
            self.assertEqual(resumable(ctx, "ship", "feat/other"), {})   # another branch
            self.assertEqual(resumable(ctx, "ship", "sync/x"), {})       # the other kind

            write_state(ctx, "ship", None)
            self.assertEqual(resumable(ctx, "ship", "feat/x"), {})
            self.assertEqual(resumable(ctx, "sync", "sync/x")["backup"], "b2")

        def test_the_file_lives_in_the_git_directory_and_never_in_the_tree(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
            self.assertEqual(os.path.basename(state_path(ctx)), STATE_FILE)
            self.assertTrue(os.path.exists(state_path(ctx)))
            self.assertEqual(sh("git", "status", "--porcelain", cwd=fork), "")

        def test_an_absent_or_unreadable_file_is_no_state_rather_than_a_crash(self):
            ctx = ctx_for(make_fork(self.tmp))
            self.assertEqual(read_state(ctx), {})                        # absent
            for text in ("{not json", '["a", "list"]', ""):
                with open(state_path(ctx), "w") as fh:
                    fh.write(text)
                self.assertEqual(read_state(ctx), {}, repr(text))
                self.assertEqual(resumable(ctx, "ship", "feat/x"), {})
            write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
            self.assertEqual(resumable(ctx, "ship", "feat/x")["backup"], "b")

        def test_a_dry_run_records_nothing(self):
            ctx = ctx_for(make_fork(self.tmp), dry_run=True)
            write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
            self.assertFalse(os.path.exists(state_path(ctx)))

        def test_each_worktree_keeps_its_own(self):
            """Two worktrees of one clone ship different branches at the same time; a
            `--continue` in one must not roll back to the other one's backup."""
            fork = make_fork(self.tmp)
            other = os.path.join(self.tmp, "wt")
            sh("git", "worktree", "add", "-b", "feat/other", other, "develop", cwd=fork)
            here, there = ctx_for(fork), ctx_for(other)
            write_state(here, "ship", {"branch": "feat/x", "backup": "here"})
            write_state(there, "ship", {"branch": "feat/other", "backup": "there"})
            self.assertNotEqual(state_path(here), state_path(there))
            self.assertEqual(resumable(here, "ship", "feat/x")["backup"], "here")
            self.assertEqual(resumable(there, "ship", "feat/other")["backup"], "there")
            self.assertEqual(resumable(here, "ship", "feat/other"), {})

    class TestPendingEntry(Base):
        """`land` works from the `pending` record and nothing else, so what is read back
        has to be exactly what `record_pending` wrote - or nothing at all."""

        def test_round_trip_and_the_url_added_afterwards(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            self.assertEqual(pending_entry(ctx), {})
            sh("git", "branch", "feat/x", "develop", cwd=fork)
            base = rev(fork, "origin/develop")
            record_pending(ctx, "ship", "feat/x", base)
            self.assertEqual(pending_entry(ctx),
                             {"kind": "ship", "branch": "feat/x", "commit": rev(fork, "feat/x"),
                              "base": base, "mr": ""})
            record_pending(ctx, "ship", "feat/x", base, "https://example.invalid/pull/1")
            self.assertEqual(pending_entry(ctx)["mr"], "https://example.invalid/pull/1")
            self.assertEqual(pending_entry(ctx)["commit"], rev(fork, "feat/x"))
            write_state(ctx, "pending", None)
            self.assertEqual(pending_entry(ctx), {})

        def test_the_most_recent_run_is_the_only_one_kept(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            sh("git", "branch", "feat/x", "develop", cwd=fork)
            sh("git", "branch", "feat/y", "develop", cwd=fork)
            record_pending(ctx, "ship", "feat/x", rev(fork, "origin/develop"))
            record_pending(ctx, "sync", "feat/y", rev(fork, "origin/develop"))
            self.assertEqual((pending_entry(ctx)["kind"], pending_entry(ctx)["branch"]),
                             ("sync", "feat/y"))

        def test_anything_but_the_written_shape_is_no_entry(self):
            ctx = ctx_for(make_fork(self.tmp))
            good = {"kind": "ship", "branch": "feat/x", "commit": "abc", "base": "def"}
            for bad in (["a", "list"],                                   # not a dict
                        {k: v for k, v in good.items() if k != "commit"},  # missing a field
                        dict(good, commit=1),                            # non-string field
                        dict(good, base=None),
                        dict(good, mr=7),                                # non-string URL
                        "abc"):
                save_state(ctx, {"pending": bad})
                self.assertEqual(pending_entry(ctx), {}, repr(bad))
            save_state(ctx, {"pending": good})                           # `mr` may be absent
            self.assertEqual(pending_entry(ctx), good)

        def test_a_dry_run_records_nothing(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork, dry_run=True)
            record_pending(ctx, "ship", "develop", rev(fork, "origin/develop"))
            self.assertFalse(os.path.exists(state_path(ctx)))

    class TestCleanTree(Base):
        def test_untracked_is_clean(self):
            fork = make_fork(self.tmp)
            write(fork, "scratch.txt", "not tracked\n")
            self.assertTrue(clean_tree(ctx_for(fork)))

        def test_modified_is_dirty(self):
            fork = make_fork(self.tmp)
            write(fork, "README.md", "# changed\n")
            self.assertFalse(clean_tree(ctx_for(fork)))

    # ------------------------------------------------------------------- #
    # push helpers
    # ------------------------------------------------------------------- #

    class TestPush(Base):
        def test_refuses_trunk_and_mirror_without_calling_git(self):
            ctx = Ctx(root=os.path.join(self.tmp, "nowhere"), trunk="develop", mirror="main")
            for branch, needle in (("develop", "refusing to push the trunk"),
                                   ("main", "refusing to push the mirror")):
                with self.assertRaises(Fail) as cm:
                    push(ctx, branch)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn(needle, str(cm.exception))

        def test_pushes_a_feature_branch(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            head = commit_fork(fork, "src/new.py", "x = 1\n")
            ctx = ctx_for(fork)
            _, out, _ = capture(push, ctx, "feat/x")
            self.assertEqual(origin_sha(fork, "feat/x"), head)
            self.assertIn("-u", out)
            self.assertNotIn("--force ", out)
            self.assertNotIn("--no-verify", out)

        def test_lease_argument(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            head = commit_fork(fork, "src/new.py", "x = 1\n")
            ctx = ctx_for(fork)
            capture(push, ctx, "feat/x")
            sh("git", "fetch", "origin", cwd=fork)
            keep, _, _ = capture(backup, ctx, "pre-ship", "HEAD")   # rule 4: a lease needs one
            _, out, _ = capture(push, ctx, "feat/x", head, keep)
            self.assertIn("--force-with-lease=feat/x:%s" % head, out)
            self.assertNotIn(" -u ", out)          # already on origin: no upstream to set

        def test_a_lease_without_a_backup_is_refused(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            head = commit_fork(fork, "src/new.py", "x = 1\n")
            ctx = ctx_for(fork)
            capture(push, ctx, "feat/x")
            sh("git", "fetch", "origin", cwd=fork)
            sh("git", "commit", "--allow-empty", "-m", "rewrite me", cwd=fork)
            with self.assertRaises(Fail) as cm:
                capture(push, ctx, "feat/x", head)
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("without a confirmed backup", str(cm.exception))
            self.assertEqual(origin_sha(fork, "feat/x"), head)     # nothing was rewritten

        def test_a_lease_behind_a_backup_that_is_not_on_origin_is_refused(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            head = commit_fork(fork, "src/new.py", "x = 1\n")
            ctx = ctx_for(fork)
            capture(push, ctx, "feat/x")
            sh("git", "fetch", "origin", cwd=fork)
            sh("git", "branch", DEFAULT_BACKUP_PREFIX + "local-only", cwd=fork)
            with self.assertRaises(Fail) as cm:
                capture(push, ctx, "feat/x", head, DEFAULT_BACKUP_PREFIX + "local-only")
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("is not on origin", str(cm.exception))
            self.assertEqual(origin_sha(fork, "feat/x"), head)

        def test_rejection_is_exit_5(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "src/new.py", "x = 1\n")
            reject_pushes(self.tmp)
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(push, ctx, "feat/x")
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("rejected", str(cm.exception))

        def test_dry_run_pushes_nothing(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "src/new.py", "x = 1\n")
            ctx = ctx_for(fork, dry_run=True)
            _, out, _ = capture(push, ctx, "feat/x")
            self.assertIn("would:", out)
            self.assertEqual(origin_sha(fork, "feat/x"), "")

        def test_a_branch_named_like_a_force_flag_cannot_force_anything(self):
            """`git push origin +plus:refs/heads/+plus` is `force=yes, src=plus` to git: the
            name alone makes an ordinary push an unconditional force-push of a *different*
            branch, with no `--force` literal anywhere for a source check to find. Only running
            the push can prove it does not happen."""
            fork = make_fork(self.tmp)
            sh("git", "branch", "--no-track", "plus", "develop", cwd=fork)   # the decoy source
            sh("git", "switch", "-c", "+plus", cwd=fork)
            commit_fork(fork, "src/new.py", "x = 1\n")
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(push, ctx, "+plus")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("refspec grammar", str(cm.exception))
            self.assertEqual(origin_sha(fork, "+plus"), "")
            self.assertEqual(origin_sha(fork, "plus"), "")

        def test_the_source_is_the_branch_and_never_another_ref_of_the_same_name(self):
            """Both sides of the refspec are fully qualified, so a tag of the same name is
            neither pushed instead of the branch nor an ambiguity that stops the push."""
            fork = make_fork(self.tmp)
            sh("git", "tag", "feat/x", "main", cwd=fork)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            head = commit_fork(fork, "src/new.py", "x = 1\n")
            ctx = ctx_for(fork)
            capture(push, ctx, "feat/x")
            self.assertEqual(origin_sha(fork, "feat/x"), head)
            self.assertNotEqual(head, rev(fork, "refs/tags/feat/x"))

    class TestPushMirror(Base):
        def test_pushes_after_advance(self):
            fork = make_fork(self.tmp)
            target_up = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            target = rev(fork, ctx.up())
            self.assertEqual(target, target_up)
            capture(advance_mirror, ctx, target)
            capture(push_mirror, ctx)
            self.assertEqual(origin_sha(fork, "main"), target)

        def test_refuses_a_mirror_with_own_commits(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours.txt", "ours\n")
            ctx = ctx_for(fork, strict_mirror=False)
            with self.assertRaises(Fail) as cm:
                capture(push_mirror, ctx)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("pure copy", str(cm.exception))

        def test_refuses_when_origin_mirror_is_ahead(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            push_upstream_into_origin(self.tmp)
            sh("git", "fetch", "--multiple", "origin", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(push_mirror, ctx)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("not an ancestor", str(cm.exception))

        def test_no_local_mirror_is_refused(self):
            fork = make_fork(self.tmp)
            sh("git", "branch", "-D", "main", cwd=fork)
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(push_mirror, ctx)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("no local `main` to push", str(cm.exception))

        def test_a_dry_run_previews_a_mirror_this_run_would_create(self):
            """A plain clone of the fork has no local mirror: `sync --dry-run` must preview
            the push `advance_mirror` would have made it possible, not fail on it."""
            fork = make_fork(self.tmp)
            sh("git", "branch", "-D", "main", cwd=fork)
            ctx = ctx_for(fork, dry_run=True)
            _, out, _ = capture(push_mirror, ctx)
            self.assertIn("would:", out)
            self.assertIn("created by this run", out)

        def test_a_dry_run_judges_the_target_this_run_would_move_the_mirror_to(self):
            """The ordinary state of a fork with two people in it: a teammate's sync advanced
            `origin/<mirror>`, upstream has moved on again, and the local mirror is behind
            both. The real run fast-forwards the mirror before pushing it, so the dry run has
            to judge that target - the tip it has not moved is not what would be sent."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/a.md", "a\n", "theirs: a")
            push_upstream_into_origin(self.tmp)              # the teammate's sync
            commit_upstream(self.tmp, "docs/b.md", "b\n", "theirs: b")
            sh("git", "fetch", "--multiple", "origin", "upstream", cwd=fork)
            target = rev(fork, "refs/remotes/upstream/main")
            self.assertNotEqual(rev(fork, "refs/remotes/origin/main"), target)

            _, out, _ = capture(push_mirror, ctx_for(fork, dry_run=True), target)
            self.assertIn("would:", out)
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)

        def test_the_target_is_still_refused_when_origin_carries_more_than_upstream(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/a.md", "a\n", "theirs: a")
            push_upstream_into_origin(self.tmp)
            sh("git", "fetch", "--multiple", "origin", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            stale = rev(fork, "refs/heads/main")             # the mirror before the teammate
            with self.assertRaises(Fail) as cm:
                capture(push_mirror, ctx, stale)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("not an ancestor", str(cm.exception))

        def test_an_unfetched_upstream_cannot_verify_the_mirror(self):
            fork = make_fork(self.tmp)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=fork)
            ctx = ctx_for(fork, strict_mirror=False)
            with self.assertRaises(Fail) as cm:
                capture(push_mirror, ctx)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("cannot verify", str(cm.exception))

    class TestAdvanceMirror(Base):
        def test_update_ref_when_not_checked_out(self):
            fork = make_fork(self.tmp)
            target = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            old, new = capture(advance_mirror, ctx, target)[0]
            self.assertEqual(new, target)
            self.assertEqual(rev(fork, "refs/heads/main"), target)
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), "develop")
            self.assertNotEqual(old, new)

        def test_ff_when_checked_out_here(self):
            fork = make_fork(self.tmp)
            target = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "fetch", "upstream", cwd=fork)
            sh("git", "switch", "main", cwd=fork)
            ctx = ctx_for(fork)
            capture(advance_mirror, ctx, target)
            self.assertEqual(rev(fork, "refs/heads/main"), target)
            with open(os.path.join(fork, "src/app.py")) as fh:
                self.assertIn("return 3", fh.read())

        def test_linked_worktree_is_refused(self):
            fork = make_fork(self.tmp)
            target = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "fetch", "upstream", cwd=fork)
            wt = os.path.join(self.tmp, "wt")
            sh("git", "worktree", "add", wt, "main", cwd=fork)
            ctx = ctx_for(fork)
            before = rev(fork, "refs/heads/main")
            with self.assertRaises(Fail) as cm:
                capture(advance_mirror, ctx, target)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("wt", str(cm.exception))
            self.assertEqual(rev(fork, "refs/heads/main"), before)

        def test_untracked_collision(self):
            fork = make_fork(self.tmp)
            target = commit_upstream(self.tmp, "docs/new.md", "upstream file\n")
            sh("git", "fetch", "upstream", cwd=fork)
            sh("git", "switch", "main", cwd=fork)
            write(fork, "docs/new.md", "mine\n")
            ctx = ctx_for(fork)
            self.assertTrue(clean_tree(ctx))
            with self.assertRaises(Fail) as cm:
                capture(advance_mirror, ctx, target)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("would be overwritten", str(cm.exception))

        def test_refuses_a_non_ancestor(self):
            fork = make_fork(self.tmp)
            target = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "fetch", "upstream", cwd=fork)
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours.txt", "ours\n")
            ctx = ctx_for(fork, strict_mirror=False)
            with self.assertRaises(Fail) as cm:
                capture(advance_mirror, ctx, target)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("only ever", str(cm.exception))

        def test_a_target_that_cannot_be_compared_is_refused(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            before = rev(fork, "refs/heads/main")
            with self.assertRaises(Fail) as cm:
                capture(advance_mirror, ctx, "0" * 40)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("cannot compare", str(cm.exception))
            self.assertEqual(rev(fork, "refs/heads/main"), before)

        def test_without_worktreepath_only_this_worktree_is_seen(self):
            """Git older than 2.23 has no `%(worktreepath)`: the fallback sees only the
            current worktree, and must still recognise a mirror checked out in it."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            real_rc = git_rc

            def old_git_rc(*args, **kw):
                if args[:1] == ("for-each-ref",):
                    return (129, "", "fatal: unknown field name: worktreepath")
                return real_rc(*args, **kw)

            with mock.patch.object(sys.modules[__name__], "git_rc", old_git_rc):
                self.assertEqual(mirror_worktree(ctx), "")        # `develop` is checked out
                sh("git", "switch", "main", cwd=fork)
                self.assertEqual(mirror_worktree(ctx), ctx.root)

        def test_dry_run_moves_nothing(self):
            fork = make_fork(self.tmp)
            target = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "fetch", "upstream", cwd=fork)
            before = rev(fork, "refs/heads/main")
            ctx = ctx_for(fork, dry_run=True)
            (old, new), out, _ = capture(advance_mirror, ctx, target)
            self.assertIn("would:", out)
            self.assertEqual((old, new), (before, target))
            self.assertEqual(rev(fork, "refs/heads/main"), before)

    class TestBootstrapTrunk(Base):
        def test_creates_and_pushes_on_a_fresh_fork(self):
            fork = make_fresh_fork(self.tmp)
            ctx = ctx_for(fork, need_trunk=False)
            target = rev(fork, ctx.up())
            capture(bootstrap_trunk, ctx, target)
            self.assertEqual(rev(fork, "refs/heads/develop"), target)
            self.assertEqual(origin_sha(fork, "develop"), target)
            self.assertEqual(sh("git", "config", "--get", "branch.develop.remote",
                                cwd=fork, check=False), "")
            self.assertEqual(sh("git", "config", "--get", "branch.develop.merge",
                                cwd=fork, check=False), "")

        def test_refuses_when_the_trunk_exists(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(bootstrap_trunk, ctx, rev(fork, ctx.up()))
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("already exists", str(cm.exception))

        def test_works_without_a_local_mirror(self):
            make_fresh_fork(self.tmp)
            clone = os.path.join(self.tmp, "clone2")
            sh("git", "clone", os.path.join(self.tmp, "origin.git"), clone)
            identity(clone)
            sh("git", "checkout", "--detach", cwd=clone)
            sh("git", "branch", "-D", "main", cwd=clone)
            sh("git", "remote", "add", "upstream", os.path.join(self.tmp, "upstream.git"), cwd=clone)
            sh("git", "fetch", "upstream", cwd=clone)
            ctx = ctx_for(clone, need_trunk=False)
            self.assertEqual(rev(clone, "refs/heads/main"), "")
            target = rev(clone, ctx.up())
            capture(bootstrap_trunk, ctx, target)
            self.assertEqual(origin_sha(clone, "develop"), target)

        def test_rejection_is_exit_5(self):
            fork = make_fresh_fork(self.tmp)
            reject_pushes(self.tmp)
            ctx = ctx_for(fork, need_trunk=False)
            with self.assertRaises(Fail) as cm:
                capture(bootstrap_trunk, ctx, rev(fork, ctx.up()))
            self.assertEqual(cm.exception.code, 5)
            self.assertEqual(rev(fork, "refs/heads/develop"), "")

        def test_dry_run_creates_nothing(self):
            fork = make_fresh_fork(self.tmp)
            ctx = ctx_for(fork, need_trunk=False, dry_run=True)
            _, out, _ = capture(bootstrap_trunk, ctx, rev(fork, ctx.up()))
            self.assertIn("would:", out)
            self.assertEqual(rev(fork, "refs/heads/develop"), "")
            self.assertEqual(origin_sha(fork, "develop"), "")

    # ------------------------------------------------------------------- #
    # backups and the merge simulation
    # ------------------------------------------------------------------- #

    class TestBackup(Base):
        def test_creates_pushes_and_confirms(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            trunk_sha = rev(fork, "refs/remotes/origin/develop")
            name, out, _ = capture(backup, ctx, "pre-sync", "origin/develop")
            self.assertRegex(name, r"^backup/\d{8}-\d{6}-pre-sync$")
            self.assertEqual(rev(fork, "refs/heads/" + name), trunk_sha)
            self.assertEqual(origin_sha(fork, name), trunk_sha)
            self.assertIn("confirmed on origin", out)
            self.assertIn("rollback: git reset --hard origin/" + name, out)
            self.assertIn("restore point", out)              # what a pre-sync backup is for

        def test_from_head_on_a_feature_branch(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            head = commit_fork(fork, "ours/new.txt", "one\n", "one")
            ctx = ctx_for(fork)
            name, out, _ = capture(backup, ctx, "pre-ship", "HEAD")
            self.assertTrue(name.endswith("-pre-ship"), name)
            self.assertEqual(origin_sha(fork, name), head)
            self.assertIn("before the rebase", out)

        def test_push_rejection_is_exit_5_and_leaves_no_local_branch(self):
            fork = make_fork(self.tmp)
            reject_pushes(self.tmp, "refs/heads/backup/*")
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(backup, ctx, "pre-sync", "origin/develop")
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("rejected", str(cm.exception))
            self.assertNotIn("backup/", local_branches(fork))

        def test_missing_on_origin_after_the_push_is_exit_5(self):
            fork = make_fork(self.tmp)
            delete_after_receive(self.tmp)
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(backup, ctx, "pre-sync", "origin/develop")
            self.assertEqual(cm.exception.code, 5)
            self.assertIn("not on origin", str(cm.exception))
            self.assertNotIn("backup/", local_branches(fork))

        def test_dry_run_creates_nothing(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork, dry_run=True)
            name, out, _ = capture(backup, ctx, "pre-sync", "origin/develop")
            self.assertIn("would:", out)
            self.assertIn("rollback:", out)
            self.assertEqual(rev(fork, "refs/heads/" + name), "")
            self.assertEqual(origin_sha(fork, name), "")

        def test_unresolvable_ref_is_exit_2(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(backup, ctx, "pre-sync", "origin/no-such-branch")
            self.assertEqual(cm.exception.code, 2)
            self.assertNotIn("backup/", local_branches(fork))

        def test_a_branch_of_the_same_name_at_another_sha_is_refused(self):
            """The push-failure path deletes the backup branch: it must never be a branch
            that was already there (Task 4)."""
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n")
            ctx = ctx_for(fork)
            name = ctx.backup_prefix + "20200101-000000-pre-ship"
            sh("git", "branch", name, "develop", cwd=fork)     # someone else's, at another sha
            other = rev(fork, "refs/heads/" + name)
            with mock.patch.object(sys.modules[__name__], "backup_name",
                                   lambda ctx, reason: name):
                with self.assertRaises(Fail) as cm:
                    capture(backup, ctx, "pre-ship", "HEAD")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("already exists", str(cm.exception))
            self.assertEqual(rev(fork, "refs/heads/" + name), other)   # left exactly as it was
            self.assertEqual(origin_sha(fork, name), "")

        @needs_tomllib
        def test_custom_backup_prefix(self):
            fork = make_fork(self.tmp, config='backup_prefix = "safety/"\n')
            ctx = ctx_for(fork)
            name, _, _ = capture(backup, ctx, "pre-sync", "origin/develop")
            self.assertTrue(name.startswith("safety/"), name)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/remotes/origin/develop"))

    @needs_merge_tree
    class TestSimulateMerge(Base):
        def test_clean(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "ours/new.txt", "ours\n", "ours", push=True)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n")
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            result, out, _ = capture(simulate_merge, ctx, rev(fork, ctx.up()))
            self.assertEqual(result, (True, []))
            self.assertIn("merges clean", out)

        def test_conflicting_paths_are_parsed(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf",
                        'resource "null_resource" "a" {\n  count = 2\n}\n', "ours: shared",
                        push=True)
            commit_upstream(self.tmp, "shared.tf",
                            'resource "null_resource" "a" {\n  count = 3\n}\n')
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            result, out, _ = capture(simulate_merge, ctx, rev(fork, ctx.up()))
            self.assertEqual(result, (False, ["shared.tf"]))
            self.assertIn("1 conflicting file(s)", out)
            self.assertIn("    shared.tf", out)

        def test_two_conflicting_files(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf",
                        'resource "null_resource" "a" {\n  count = 2\n}\n', "ours: shared",
                        push=True)
            commit_fork(fork, "README.md", "# ours\n", "ours: readme", push=True)
            commit_upstream(self.tmp, "shared.tf",
                            'resource "null_resource" "a" {\n  count = 3\n}\n')
            commit_upstream(self.tmp, "README.md", "# theirs\n")
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            result, _, _ = capture(simulate_merge, ctx, rev(fork, ctx.up()))
            self.assertFalse(result[0])
            self.assertEqual(sorted(result[1]), ["README.md", "shared.tf"])

        def test_bad_ref_is_exit_2(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            with self.assertRaises(Fail) as cm:
                capture(simulate_merge, ctx, "no-such-ref")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("merge", str(cm.exception))

        def test_nothing_is_written_and_the_worktree_is_untouched(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf",
                        'resource "null_resource" "a" {\n  count = 2\n}\n', "ours: shared",
                        push=True)
            commit_upstream(self.tmp, "shared.tf",
                            'resource "null_resource" "a" {\n  count = 3\n}\n')
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            before = sh("git", "for-each-ref", cwd=fork)
            capture(simulate_merge, ctx, rev(fork, ctx.up()))
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)
            self.assertEqual(sh("git", "status", "--porcelain", cwd=fork), "")

    class TestSimulateMergeOldGit(Base):
        def test_old_git_returns_none_with_a_note(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            with mock.patch.object(sys.modules[__name__], "git_version", lambda: (2, 37)):
                result, out, _ = capture(simulate_merge, ctx, rev(fork, ctx.up()))
            self.assertIsNone(result)
            self.assertIn("2.38+", out)

    # ------------------------------------------------------------------- #
    # status
    # ------------------------------------------------------------------- #

    class TestStatus(Base):
        def test_numbers_for_a_constructed_state(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "src/app.py", "def main():\n    return 9\n", "ours: app", push=True)
            commit_fork(fork, "ours/notes.md", "notes\n", "ours: notes", push=True)
            commit_upstream(self.tmp, "README.md", "# project moved\n")
            sh("git", "fetch", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("+1/-2 vs origin/develop", out)
            self.assertIn("divergence: 2 files, 1 upstream-tracked", out)
            self.assertIn("mirror behind by 1", out)

        def test_upstream_tracked_warning_lists_exactly_the_touched_file(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "src/app.py", "def main():\n    return 7\n", "touch theirs")
            commit_fork(fork, "ours/new.txt", "ours only\n", "add ours")
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("touches upstream-tracked files (WARNING, 1)", out)
            self.assertIn("    src/app.py", out)
            self.assertNotIn("ours/new.txt", out)
            self.assertIn("branch   feat/x  not on origin", out)

        def test_a_non_ascii_path_reaches_the_upstream_tracked_list(self):
            """git C-quotes such paths unless it is asked not to, and a quoted name is not a
            path any later `git` call can resolve - the file would drop out of the list."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/café.txt", "theirs\n", "theirs: cafe")
            sh("git", "fetch", "upstream", cwd=fork)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "docs/café.txt", "ours\n", "ours: cafe")
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("touches upstream-tracked files (WARNING, 1)", out)
            self.assertIn("    docs/café.txt", out)
            self.assertNotIn("\\303", out)                  # not the C-quoted spelling

        def test_branch_line_counts_unpushed_and_modified(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "-c", "feat/x", cwd=fork)
            commit_fork(fork, "ours/new.txt", "one\n", "one", push=True)
            commit_fork(fork, "ours/new.txt", "two\n", "two")
            write(fork, "README.md", "# dirty\n")
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("branch   feat/x  1 unpushed (vs origin/feat/x)", out)
            self.assertIn("tree: 1 modified", out)

        def test_detached_head_is_reported(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "--detach", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("(detached at", out)

        def test_setup_line_before_setup(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("(LIVE)", out)
            self.assertIn("pre-push hook: missing", out)
            self.assertIn("ff-only: develop no, main no", out)
            self.assertIn("run `forkflow setup`", out)

        def test_setup_line_after_a_simulated_setup(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "set-url", "--push", "upstream", "DISABLED", cwd=fork)
            sh("git", "config", "branch.develop.mergeOptions", "--ff-only", cwd=fork)
            sh("git", "config", "branch.main.mergeOptions", "--ff-only", cwd=fork)
            hooks = hooks_path(fork)
            os.makedirs(hooks, exist_ok=True)
            with open(os.path.join(hooks, "pre-push"), "w") as fh:
                fh.write("#!/bin/sh\n" + HOOK_MARK + "\nexit 0\n")
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("upstream push: DISABLED", out)
            self.assertIn("pre-push hook: installed", out)
            self.assertIn("ff-only: develop yes, main yes", out)
            self.assertNotIn("run `forkflow setup`", out)

        def test_foreign_hook_is_named(self):
            fork = make_fork(self.tmp)
            hooks = hooks_path(fork)
            os.makedirs(hooks, exist_ok=True)
            with open(os.path.join(hooks, "pre-push"), "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("pre-push hook: foreign", out)

        def test_backups_listed_newest_first(self):
            fork = make_fork(self.tmp)
            for name in ("backup/20260101-000000-pre-sync",
                         "backup/20260102-000000-pre-ship",
                         "backup/20251231-000000-pre-sync"):
                sh("git", "push", "origin", "develop:refs/heads/" + name, cwd=fork)
            sh("git", "fetch", "origin", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("backups  3 (refs/remotes/origin/backup/*):", out)
            listed = [ln for ln in out.splitlines() if ln.startswith("  backups")][0]
            self.assertIn("origin/backup/20260102-000000-pre-ship, "
                          "origin/backup/20260101-000000-pre-sync, "
                          "origin/backup/20251231-000000-pre-sync", listed)

        def test_no_backups(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("backups  0 (refs/remotes/origin/backup/*)", out)

        def test_the_age_of_the_last_fetch_is_bucketed(self):
            self.assertEqual(_rel_age(0), "0s ago")
            self.assertEqual(_rel_age(89), "89s ago")
            self.assertEqual(_rel_age(90), "1m ago")
            self.assertEqual(_rel_age(89 * 60), "89m ago")
            self.assertEqual(_rel_age(90 * 60), "1h ago")
            self.assertEqual(_rel_age(35 * 3600), "35h ago")
            self.assertEqual(_rel_age(36 * 3600), "1d ago")
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            # the bucketed age comes first; which remotes the fetch reached follows in parentheses
            self.assertRegex(last_fetch(ctx), r"^\d+[smhd] ago( \([^)]*\))?$")
            path = git("rev-parse", "--git-path", "FETCH_HEAD", cwd=fork)
            os.remove(path if os.path.isabs(path) else os.path.join(fork, path))
            self.assertEqual(last_fetch(ctx), "never")

        def test_fetch_refreshes_a_moved_upstream(self):
            fork = make_fork(self.tmp)
            before = rev(fork, "refs/remotes/upstream/main")
            moved = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 5\n")
            self.assertEqual(rev(fork, "refs/remotes/upstream/main"), before)
            code, out, err = run("-C", fork, "status", "--fetch")
            self.assertEqual(code, 0, err)
            self.assertIn("git fetch --multiple origin upstream", out)
            self.assertEqual(rev(fork, "refs/remotes/upstream/main"), moved)
            self.assertIn("mirror behind by 1", out)

        def test_no_writes_without_fetch(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "src/app.py", "def main():\n    return 5\n")
            push_upstream_into_origin(self.tmp)
            before = sh("git", "for-each-ref", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)
            self.assertNotIn("git fetch", out)

        def test_status_asks_the_server_and_reports_a_moved_upstream(self):
            # the GET fork, 2026-09-09: `(=)` printed from a 6h-old fetch while upstream had
            # moved, and the advice built on it was wrong. One ls-remote, no writes, no fetch.
            fork = make_fork(self.tmp)
            moved = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 5\n")
            before = sh("git", "for-each-ref", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn(f"server at {short(moved)}", out)
            self.assertIn("upstream moved since the last fetch", out)
            self.assertIn("as fetched, server moved", out)      # the mirror cell no longer says (=)
            self.assertNotIn("git fetch", out)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)

        def test_status_reports_the_server_in_step_with_the_fetch(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("= fetched", out)
            self.assertNotIn("server moved", out)

        def test_offline_status_makes_no_network_call(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "src/app.py", "def main():\n    return 5\n")
            # an unreachable server proves which runs ask it and which do not
            sh("git", "remote", "set-url", "upstream", os.path.join(self.tmp, "nowhere.git"), cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("server not reachable", out)
            code, out, err = run("-C", fork, "status", "--offline")
            self.assertEqual(code, 0, err)
            self.assertIn("not asked (--offline)", out)
            self.assertNotIn("ls-remote", out)
            self.assertNotIn("server moved", out)

        def test_the_age_line_names_the_remotes_the_last_fetch_reached(self):
            # FETCH_HEAD is rewritten by any fetch: after ship's `git fetch origin` the age
            # reads seconds while the upstream ref is as old as ever - say so
            fork = make_fork(self.tmp)
            sh("git", "fetch", "origin", cwd=fork)
            _, out, _ = run("-C", fork, "status", "--offline")
            age = next(ln for ln in out.splitlines() if "as of last fetch" in ln)
            self.assertIn("(origin only - upstream not in it)", age)
            sh("git", "fetch", "--multiple", "origin", "upstream", cwd=fork)
            _, out, _ = run("-C", fork, "status", "--offline")
            age = next(ln for ln in out.splitlines() if "as of last fetch" in ln)
            self.assertIn("(origin, upstream)", age)

        def test_mirror_unpushed_after_a_local_advance(self):
            fork = make_fork(self.tmp)
            target = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 5\n")
            sh("git", "fetch", "upstream", cwd=fork)
            _, out, _ = run("-C", fork, "status")
            self.assertIn("mirror behind by 1", out)
            sh("git", "update-ref", "refs/heads/main", target, cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("unpushed 1", out)

        def test_fresh_fork_gets_the_setup_hint(self):
            fork = make_fresh_fork(self.tmp)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("origin/develop missing", out)
            self.assertIn("run `forkflow setup`", out)
            self.assertIn("trunk `develop` is not on origin", out)

        def test_diverged_mirror_is_reported_and_exits_0(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours.txt", "ours\n", "work on the mirror")
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("DIVERGED", out)
            self.assertIn("Adopting forkflow", out)

        def test_fetch_recomputes_the_mirror_hint_it_prints(self):
            """The hint is a verdict on the refs; `--fetch` refreshes them, so printing the
            verdict from before the fetch contradicts the table printed above it."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            push_upstream_into_origin(self.tmp, "main")
            sh("git", "fetch", "origin", cwd=fork)
            sh("git", "switch", "main", cwd=fork)
            sh("git", "merge", "--ff-only", "origin/main", cwd=fork)

            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("hint     mirror `main` is ahead of `upstream/main`", out)

            code, out, err = run("-C", fork, "status", "--fetch")
            self.assertEqual(code, 0, err)
            self.assertNotIn("hint     mirror", out)
            self.assertIn("upstream/main %s (=)" % short(rev(fork, "refs/heads/main")), out)

        def test_unfetched_upstream_is_reported_and_exits_0(self):
            fork = make_fork(self.tmp)
            sh("git", "symbolic-ref", "-d", "refs/remotes/upstream/HEAD", cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("unfetched", out)
            self.assertIn("divergence: unknown", out)

        def test_single_branch_clone_shows_a_dash(self):
            make_fork(self.tmp)
            clone = os.path.join(self.tmp, "single")
            sh("git", "clone", "--single-branch", "--branch", "develop",
               os.path.join(self.tmp, "origin.git"), clone)
            identity(clone)
            sh("git", "remote", "add", "upstream", os.path.join(self.tmp, "upstream.git"), cwd=clone)
            sh("git", "fetch", "upstream", cwd=clone)
            code, out, err = run("-C", clone, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("mirror  main -", out)

        def test_no_upstream_remote_is_exit_2(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "remove", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertIn("forkflow setup --upstream-url", err)

    # ------------------------------------------------------------------- #
    # check
    # ------------------------------------------------------------------- #

    class TestCheck(Base):
        def feature(self, fork: str, name: str = "feat/x", start: Optional[str] = None) -> None:
            args = ["git", "switch", "-c", name] + ([start] if start else [])
            sh(*args, cwd=fork)

        def test_no_gate_configured(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            commit_fork(fork, "ours/new.txt", "one\n", "one")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err)
            self.assertIn("none configured (.forkflow.toml gate = [...])", out)
            self.assertIn("check    ok", out)

        @needs_tomllib
        def test_passing_gate_runs_in_the_repo_root(self):
            fork = make_fork(self.tmp, config='gate = ["test -f README.md", "true"]\n')
            self.feature(fork)
            commit_fork(fork, "ours/new.txt", "one\n", "one")
            code, out, err = run("-C", os.path.join(fork, "src"), "check")
            self.assertEqual(code, 0, err)
            self.assertIn("sh -c 'test -f README.md'  -> passed", out)
            self.assertIn("sh -c 'true'  -> passed", out)

        @needs_tomllib
        def test_failing_gate_is_exit_3_with_its_output_tail(self):
            fork = make_fork(
                self.tmp,
                config='gate = ["echo first line; echo boom >&2; exit 2", "touch second-ran"]\n')
            self.feature(fork)
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 3)
            self.assertIn("FAILED (exit 2)", out)
            self.assertIn("boom", out)
            self.assertIn("first line", out)
            self.assertIn("check    FAILED:", out)
            # the first failure is the one to fix: later gate commands do not run
            self.assertFalse(os.path.exists(os.path.join(fork, "second-ran")))

        @needs_tomllib
        def test_dry_run_never_executes_a_gate(self):
            fork = make_fork(self.tmp, config='gate = ["touch gate-ran; exit 2"]\n')
            self.feature(fork)
            code, out, err = run("-C", fork, "check", "--dry-run")
            self.assertEqual(code, 0, err)
            self.assertIn("would:", out)
            self.assertIn("not run (dry run)", out)
            self.assertFalse(os.path.exists(os.path.join(fork, "gate-ran")))

        @needs_tomllib
        def test_gate_of_a_wrong_type_is_exit_2(self):
            for value in ("3", '"make test"', '["ok", 3]'):     # a bare string included
                fork = make_fork(tempfile.mkdtemp(dir=self.tmp), config="gate = %s\n" % value)
                code, out, err = run("-C", fork, "check")
                self.assertEqual(code, 2, value)
                self.assertIn("list of shell commands", err)

        @needs_tomllib
        def test_only_the_last_lines_of_a_failing_gate_are_shown(self):
            self.assertEqual(TAIL_LINES, 12)         # pinned: what a failing gate is judged by
            fork = make_fork(self.tmp, config='gate = ["seq 1 40; exit 2"]\n')
            self.feature(fork)
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 3)
            shown = [ln.strip() for ln in out.splitlines() if re.fullmatch(r"\s+\d+", ln)]
            self.assertEqual(len(shown), 12)
            self.assertEqual(shown[-1], "40")
            self.assertEqual(shown[0], "29")

        def test_a_trunk_that_cannot_be_compared_is_exit_2(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            ctx = ctx_for(fork, need_trunk=False, trunk="no-such-trunk")
            with self.assertRaises(Fail) as cm:
                capture(run_check, ctx)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("cannot compare HEAD with origin/no-such-trunk", str(cm.exception))

        def test_on_the_trunks_tip_is_exit_0(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            commit_fork(fork, "ours/new.txt", "one\n", "one")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err)
            self.assertIn("on origin/develop's tip", out)

        def test_diverged_from_the_trunk_is_exit_3_with_both_numbers(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            commit_fork(fork, "ours/new.txt", "one\n", "one")
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n")
            push_upstream_into_origin(self.tmp, "develop")
            sh("git", "fetch", "origin", cwd=fork)
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 3)
            self.assertIn("not on origin/develop's tip (behind by 1, ahead by 1)", out)
            self.assertIn("check    FAILED:", out)

        def test_tip_is_checked_against_the_trunk_not_the_mirror(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "ours/trunk.txt", "trunk\n", "trunk moves", push=True)
            sh("git", "fetch", "origin", cwd=fork)
            self.feature(fork, start="main")               # on the mirror's tip, behind the trunk
            commit_fork(fork, "ours/new.txt", "one\n", "one")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 3)
            self.assertIn("not on origin/develop's tip (behind by 1, ahead by 1)", out)
            self.assertNotIn("origin/main's tip", out)

        def test_skipped_on_the_trunk_even_when_behind(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n")
            push_upstream_into_origin(self.tmp, "develop")
            sh("git", "fetch", "origin", cwd=fork)
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err)
            self.assertIn("skipped (on the trunk `develop`)", out)
            self.assertIn("check    ok", out)

        def test_upstream_tracked_warning_does_not_fail_the_check(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            commit_fork(fork, "src/app.py", "def main():\n    return 7\n", "touch theirs")
            commit_fork(fork, "ours/new.txt", "ours only\n", "add ours")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err)
            self.assertIn("touches upstream-tracked files (WARNING, 1)", out)
            self.assertIn("    src/app.py", out)
            self.assertNotIn("ours/new.txt", out)
            self.assertIn("check    ok", out)

        @needs_tomllib
        def test_custom_trunk_name_from_config(self):
            fork = make_fork(self.tmp, trunk="trunk", mirror="upstream-main",
                             config='trunk = "trunk"\nmirror = "upstream-main"\n')
            self.feature(fork)
            commit_fork(fork, "ours/new.txt", "one\n", "one")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err)
            self.assertIn("on origin/trunk's tip", out)

        def test_missing_trunk_on_origin_is_exit_2(self):
            fork = make_fresh_fork(self.tmp)
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 2)
            self.assertIn("forkflow setup", err)

        def test_diverged_mirror_is_exit_2(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours.txt", "ours\n", "work on the mirror")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 2)
            self.assertIn("Adopting forkflow", err)

        def test_check_is_read_only(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            commit_fork(fork, "ours/new.txt", "one\n", "one")
            before = sh("git", "for-each-ref", cwd=fork)
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)
            self.assertNotIn("git fetch", out)

    # ------------------------------------------------------------------- #
    # sync - the clean path
    # ------------------------------------------------------------------- #

    class TestSync(Base):
        def sync_name(self, remote: str = "upstream") -> str:
            return sync_branch_name(remote)

        def ahead_upstream(self, fork: str, n: int = 1) -> str:
            for i in range(n):
                commit_upstream(self.tmp, "docs/theirs%d.md" % i, "theirs %d\n" % i,
                                "theirs: doc %d" % i)
            return sh("git", "rev-parse", "HEAD", cwd=os.path.join(self.tmp, "seed"))

        def test_clean_sync_end_to_end(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "ours/feature.txt", "ours\n", "ours: feature", push=True)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            commit_upstream(self.tmp, "README.md", "# project moved\n", "theirs: readme")
            second_clone_commit(self.tmp)          # origin/develop moved behind the fork's back
            before_trunk = origin_sha(fork, "develop")
            name = self.sync_name()

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)

            target = rev(fork, "upstream/main")
            self.assertEqual(origin_sha(fork, "main"), target)     # mirror is a pure copy again
            self.assertEqual(rev(fork, "refs/heads/main"), target)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            self.assertEqual(rev(fork, name + "^1"), before_trunk)  # branched off origin/develop
            self.assertEqual(rev(fork, name + "^2"), target)        # one merge of upstream
            msg = sh("git", "log", "-1", "--format=%B", name, cwd=fork)
            self.assertIn("Merge main (mirror of upstream/main) into " + name, msg)
            self.assertIn("theirs: docs", msg)
            self.assertIn("theirs: readme", msg)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)   # the trunk is never pushed
            self.assertTrue(sh("git", "ls-remote", "--heads", "origin",
                               "refs/heads/backup/*", cwd=fork))
            self.assertEqual(checked_out(fork), name)
            self.assertIn("open the merge request manually", out)
            fetch_line = [ln for ln in out.splitlines() if "git fetch --multiple" in ln][0]
            self.assertNotIn("upstream/main unchanged", fetch_line)
            self.assertNotIn("origin/develop unchanged", fetch_line)

        def test_mirror_is_pushed_before_already_in_sync(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            push_upstream_into_origin(self.tmp, "develop")     # the trunk already has it
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, "main"), rev(fork, "upstream/main"))
            self.assertIn("already in sync", out)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertNotIn(self.sync_name(), local_branches(fork))
            self.assertEqual(origin_sha(fork, self.sync_name()), "")
            self.assertNotIn("backup/", local_branches(fork))

        def test_fully_in_sync_moves_nothing(self):
            fork = make_fork(self.tmp)
            before = sh("git", "for-each-ref", cwd=fork)
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertIn("already in sync", out)
            self.assertIn("up to date", out)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)

        def test_dry_run_previews_the_pending_merge_and_moves_nothing(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf",
                        'resource "null_resource" "a" {\n  count = 2\n}\n', "ours: shared",
                        push=True)
            commit_upstream(self.tmp, "shared.tf",
                            'resource "null_resource" "a" {\n  count = 3\n}\n', "theirs: shared")
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            before_trunk = origin_sha(fork, "develop")
            before_mirror = origin_sha(fork, "main")
            before_local_mirror = rev(fork, "refs/heads/main")

            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("2 upstream commit(s) to take", out)
            self.assertIn("theirs: shared", out)
            self.assertIn("theirs: docs", out)
            self.assertIn("would:", out)
            if git_version() >= MERGE_TREE_GIT:
                self.assertIn("1 conflicting file(s)", out)
                self.assertIn("    shared.tf", out)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertEqual(origin_sha(fork, "main"), before_mirror)
            self.assertEqual(rev(fork, "refs/heads/main"), before_local_mirror)
            self.assertNotIn("backup/", local_branches(fork))
            self.assertNotIn(self.sync_name(), local_branches(fork))
            self.assertEqual(checked_out(fork), "develop")

        def test_existing_sync_branch_is_exit_2(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            sh("git", "branch", self.sync_name(), "develop", cwd=fork)
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2)
            self.assertIn("already exists", err)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            # refused before the backup: a rerun leaves no orphan backup branch on origin
            self.assertNotIn("backup/", local_branches(fork))
            self.assertEqual(sh("git", "ls-remote", "--heads", "origin",
                                "refs/heads/backup/*", cwd=fork), "")

        def test_a_sync_branch_only_on_origin_is_exit_2_before_the_backup(self):
            """With no local branch there is nothing for `--continue` to resume, so refusing
            at the push would leave a backup and a merge behind and a hint that cannot fix
            it. Asked of origin itself: this runs before the fetch, and another clone's sync
            counts too - the remote-tracking ref is deliberately not refreshed here."""
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            name = self.sync_name()
            sh("git", "push", "origin", "refs/heads/develop:refs/heads/" + name, cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/origin/" + name, cwd=fork)
            before_trunk = origin_sha(fork, "develop")
            self.assertFalse(has_ref(fork, "refs/remotes/origin/" + name))

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, err + out)
            self.assertIn("already on origin", err)
            self.assertIn("sync --force", err)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertNotIn("backup/", local_branches(fork))
            self.assertEqual(sh("git", "ls-remote", "--heads", "origin",
                                "refs/heads/backup/*", cwd=fork), "")

        def test_force_recreates_a_sync_branch_that_is_not_checked_out(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            name = self.sync_name()
            sh("git", "branch", name, "develop", cwd=fork)      # stale, and we are on the trunk
            code, out, err = run("-C", fork, "sync", "--force")
            self.assertEqual(code, 0, err + out)
            self.assertIn("recreated", out)
            self.assertEqual(rev(fork, name + "^2"), rev(fork, "upstream/main"))
            self.assertEqual(checked_out(fork), name)

        def test_a_clone_without_a_local_mirror_syncs(self):
            """The normal shape of a fresh clone once `origin/HEAD` is the trunk: only the
            trunk is local, and the mirror is created by this run."""
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            clone = os.path.join(self.tmp, "plain")
            sh("git", "clone", os.path.join(self.tmp, "origin.git"), clone)
            identity(clone)
            sh("git", "remote", "add", "upstream", os.path.join(self.tmp, "upstream.git"),
               cwd=clone)
            sh("git", "fetch", "upstream", cwd=clone)
            self.assertNotIn("refs/heads/main", local_branches(clone))

            code, out, err = run("-C", clone, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would:", out)
            self.assertNotIn("refs/heads/main", local_branches(clone))

            code, out, err = run("-C", clone, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(clone, "refs/heads/main"), rev(clone, "upstream/main"))
            self.assertEqual(origin_sha(clone, "main"), rev(clone, "upstream/main"))
            self.assertEqual(origin_sha(clone, self.sync_name()),
                             rev(clone, "refs/heads/" + self.sync_name()))

        def test_force_recreates_the_sync_branch_we_are_on(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            name = self.sync_name()
            sh("git", "branch", name, "develop", cwd=fork)
            sh("git", "checkout", name, cwd=fork)
            code, out, err = run("-C", fork, "sync", "--force")
            self.assertEqual(code, 0, err + out)
            self.assertIn("recreated", out)
            self.assertEqual(rev(fork, name + "^2"), rev(fork, "upstream/main"))
            self.assertEqual(checked_out(fork), name)

        def test_force_redoes_a_published_sync_under_a_free_name(self):
            """The documented recovery for a sync MR the trunk moved under. The dated name is
            already on origin, its tip is a merge off the *old* trunk, and a sync branch is
            never rebased or force-pushed - so the rerun publishes the next free name instead
            of being rejected with nothing left to try."""
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            first = self.sync_name()
            self.assertEqual(run("-C", fork, "sync")[0], 0)
            published = origin_sha(fork, first)
            self.assertNotEqual(published, "")

            second_clone_commit(self.tmp)                  # the trunk moves under the MR
            next_utc_second()                              # a second pre-sync backup
            code, out, err = run("-C", fork, "sync", "--force")
            self.assertEqual(code, 0, err + out)
            self.assertIn("this sync becomes `%s-2`" % first, out)
            self.assertEqual(origin_sha(fork, first), published)      # the stale MR untouched
            self.assertEqual(origin_sha(fork, first + "-2"),
                             rev(fork, "refs/heads/" + first + "-2"))
            self.assertEqual(checked_out(fork), first + "-2")
            self.assertEqual(rev(fork, first + "-2^1"),
                             rev(fork, "refs/remotes/origin/develop"))
            self.assertEqual(rev(fork, first + "-2^2"), rev(fork, "upstream/main"))

        def test_dirty_tree_is_exit_2(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            write(fork, "README.md", "# dirty\n")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2)
            self.assertIn("uncommitted changes", err)
            self.assertEqual(origin_sha(fork, "main"), rev(fork, "refs/heads/main"))

        def test_detached_head_is_exit_2(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            sh("git", "checkout", "--detach", "develop", cwd=fork)
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2)
            self.assertIn("detached", err)

        def test_mirror_checked_out_here_is_fast_forwarded(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "main", cwd=fork)
            self.ahead_upstream(fork)
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertIn("git merge --ff-only", out)
            self.assertEqual(rev(fork, "refs/heads/main"), rev(fork, "upstream/main"))
            self.assertEqual(origin_sha(fork, "main"), rev(fork, "upstream/main"))
            # `--ff-only` (not `update-ref`) is what keeps the checked-out files in step
            self.assertTrue(os.path.exists(os.path.join(fork, "docs", "theirs0.md")))
            self.assertEqual(sh("git", "status", "--porcelain", cwd=fork), "")

        def test_mirror_in_a_linked_worktree_is_exit_2_before_anything_is_pushed(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            sh("git", "worktree", "add", os.path.join(self.tmp, "wt"), "main", cwd=fork)
            before_mirror = origin_sha(fork, "main")
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2)
            self.assertIn("checked out in", err)
            self.assertEqual(origin_sha(fork, "main"), before_mirror)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertNotIn("backup/", local_branches(fork))
            self.assertNotIn(self.sync_name(), local_branches(fork))

        @needs_tomllib
        def test_custom_names_from_config(self):
            fork = make_fork(self.tmp, trunk="trunk", mirror="upstream-main",
                             config='trunk = "trunk"\nmirror = "upstream-main"\n')
            self.ahead_upstream(fork)
            before_trunk = origin_sha(fork, "trunk")
            name = self.sync_name()
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            target = rev(fork, "upstream/main")
            self.assertEqual(origin_sha(fork, "upstream-main"), target)
            self.assertEqual(rev(fork, name + "^1"), before_trunk)
            self.assertEqual(rev(fork, name + "^2"), target)
            self.assertEqual(origin_sha(fork, "trunk"), before_trunk)
            self.assertIn("Merge upstream-main (mirror of upstream/main)",
                          sh("git", "log", "-1", "--format=%B", name, cwd=fork))

        def test_mirror_push_rejection_is_exit_5_before_any_backup(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            reject_pushes(self.tmp, "refs/heads/main")
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 5)
            self.assertIn("mirror", err)
            self.assertNotIn("backup/", local_branches(fork))
            self.assertNotIn(self.sync_name(), local_branches(fork))
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)

        def test_mirror_with_its_own_commit_is_exit_2_and_touches_nothing(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "main", cwd=fork)
            commit_fork(fork, "ours.txt", "ours\n", "work on the mirror")
            before = sh("git", "for-each-ref", cwd=fork)
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2)
            self.assertIn("Adopting forkflow", err)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)

        def test_backup_rejection_is_exit_5_with_no_sync_branch(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            reject_pushes(self.tmp, "refs/heads/backup/*")
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 5)
            self.assertNotIn(self.sync_name(), local_branches(fork))
            self.assertNotIn("backup/", local_branches(fork))
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            # the mirror step ran first and is unaffected by a failure further down
            self.assertEqual(origin_sha(fork, "main"), rev(fork, "upstream/main"))

        def test_push_of_the_sync_branch_rejected_is_exit_5_with_the_continue_hint(self):
            fork = make_fork(self.tmp)
            self.ahead_upstream(fork)
            reject_pushes(self.tmp, "refs/heads/sync/*")
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 5)
            self.assertIn("forkflow sync --continue", out)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertEqual(rev(fork, self.sync_name() + "^2"), rev(fork, "upstream/main"))

        @needs_tomllib
        def test_an_untracked_file_upstream_tracks_is_refused_before_the_backup(self):
            """`setup` writes `.forkflow.toml` untracked, and an upstream that uses forkflow
            too tracks it - the case the gate guard exists for. git refuses a merge that
            would write over an untracked file, so on the default setup path every later
            sync died on raw git output and left one more orphan backup on origin."""
            fork = make_fork(self.tmp)
            self.assertEqual(run("-C", fork, "setup")[0], 0)
            self.assertTrue(os.path.exists(os.path.join(fork, CONFIG_FILE)))
            commit_upstream(self.tmp, CONFIG_FILE, "gate = []\n", "theirs: forkflow too")
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, err + out)
            self.assertIn(CONFIG_FILE, out + err)
            self.assertIn("untracked", out + err)
            self.assertNotIn("backup/", local_branches(fork))          # nothing to clean up
            self.assertEqual(origin_sha(fork, self.sync_name()), "")
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)

            # the remedy the message names, run literally, unblocks it
            os.unlink(os.path.join(fork, CONFIG_FILE))
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, self.sync_name() + "^2"), rev(fork, "upstream/main"))

        @needs_tomllib
        def test_the_untracked_merge_fallback_names_a_rerun_that_is_not_a_closed_loop(self):
            """`cmd_sync`'s preflight answers this before the backup; the fallback in
            `merge_upstream` is what is left when the tree changes under the run, and by then
            the sync branch exists. Naming plain `forkflow sync` there is a closed loop -
            that run refuses the existing branch and sends the user to `--continue`, which
            has no merge to resume. Both halves of the loop are walked here."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, "# ours, untracked\n")
            commit_upstream(self.tmp, CONFIG_FILE, "gate = []\n", "theirs: forkflow too")
            with mock.patch.object(sys.modules[__name__], "untracked_in_the_way",
                                   lambda ctx, target: []):
                code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, out + err)
            self.assertIn("forkflow sync --force", err)
            self.assertIn(self.sync_name(), local_branches(fork))    # the branch is there now

            # the remedy cleared, the two commands the old message pointed at are the loop
            os.unlink(os.path.join(fork, CONFIG_FILE))
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, out + err)
            self.assertIn("already exists", err)
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 2, out + err)
            self.assertIn("nothing to continue", err)
            # and the one it names now gets out of it
            next_utc_second()
            code, out, err = run("-C", fork, "sync", "--force")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, self.sync_name() + "^2"), rev(fork, "upstream/main"))

        def test_a_file_upstream_renamed_onto_an_untracked_path_is_refused(self):
            """git detects renames by default (2.9+), so an upstream `git mv` onto a path
            this fork holds untracked reports the new path as `R`, not `A`, and the preflight
            saw nothing: the backup was pushed and the sync branch created before the merge
            died on it - the orphan-backup outcome the preflight exists to prevent."""
            fork = make_fork(self.tmp)
            seed = os.path.join(self.tmp, "seed")
            sh("git", "fetch", "origin", cwd=seed)
            sh("git", "checkout", "-B", "main", "origin/main", cwd=seed)
            os.makedirs(os.path.join(seed, "docs"), exist_ok=True)
            sh("git", "mv", "README.md", "docs/README.md", cwd=seed)
            sh("git", "commit", "-m", "theirs: move the readme", cwd=seed)
            sh("git", "push", "origin", "main", cwd=seed)
            write(fork, "docs/README.md", "# ours, untracked\n")
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, err + out)
            self.assertIn("docs/README.md", out + err)
            self.assertIn("untracked", out + err)
            self.assertNotIn("backup/", local_branches(fork))           # nothing to clean up
            self.assertEqual(sh("git", "ls-remote", "origin", "refs/heads/backup/*",
                                cwd=fork), "")
            self.assertNotIn(self.sync_name(), local_branches(fork))
            self.assertEqual(origin_sha(fork, self.sync_name()), "")
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)

        @needs_tomllib
        def test_failing_gate_is_exit_3_with_the_continue_hint(self):
            fork = make_fork(self.tmp, config='gate = ["exit 2"]\n')
            self.ahead_upstream(fork)
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 3)
            self.assertIn("forkflow sync --continue", out)
            self.assertEqual(origin_sha(fork, self.sync_name()), "")   # nothing was pushed
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)

    @needs_tomllib
    class TestSyncGateOnACleanMerge(Base):
        """The `gate` guard on the path where the merge does not conflict.

        `resolve_ctx` reads `.forkflow.toml` before that merge, so everything here is about
        the config the merge *produced* rather than the one the run started with."""

        def trunk_from_upstream(self, fork: str) -> None:
            """Publish upstream's tip as the trunk, as a merged sync MR would: the fork then
            carries upstream's `.forkflow.toml` and the next change to it merges clean."""
            push_upstream_into_origin(self.tmp, "develop")
            sh("git", "fetch", "origin", cwd=fork)
            sh("git", "checkout", "develop", cwd=fork)
            sh("git", "reset", "--hard", "origin/develop", cwd=fork)

        def test_the_gate_shown_is_the_one_the_merge_brought(self):
            """The guard exists so nobody runs upstream's unread shell. Showing the fork's
            own pre-merge command and then naming `forkflow check` - which runs the one that
            arrived - is that failure, not a display wart."""
            fork = make_fork(self.tmp)
            ours = os.path.join(self.tmp, "OLD-ran")
            theirs = os.path.join(self.tmp, "NEW-ran")
            commit_upstream(self.tmp, CONFIG_FILE, 'gate = ["touch %s"]\n' % ours,
                            "theirs: a gate")
            self.trunk_from_upstream(fork)
            commit_upstream(self.tmp, CONFIG_FILE, 'gate = ["touch %s"]\n' % theirs,
                            "theirs: a different gate")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertIn("NOT RUN", out)
            self.assertIn("would have run: sh -c 'touch %s'" % theirs, out)
            self.assertNotIn(ours, out)                    # never the pre-merge command
            self.assertFalse(os.path.exists(ours))
            self.assertFalse(os.path.exists(theirs))

            # `forkflow check` is the command the guard names: it runs what it showed
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err + out)
            self.assertTrue(os.path.exists(theirs))
            self.assertFalse(os.path.exists(ours))

        def test_a_gate_the_fork_did_not_have_is_shown_and_not_run(self):
            """The fork has no `gate` at all and upstream's merge adds one: there is nothing
            to suppress, and the NOT-RUN block is the whole point of the run."""
            fork = make_fork(self.tmp)
            flag = os.path.join(self.tmp, "theirs-gate-ran")
            commit_upstream(self.tmp, CONFIG_FILE, 'gate = ["touch %s"]\n' % flag,
                            "theirs: a gate")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertIn("NOT RUN", out)
            self.assertIn("would have run: sh -c 'touch %s'" % flag, out)
            self.assertNotIn("none configured", out)
            self.assertFalse(os.path.exists(flag))

        def test_an_unchanged_gate_runs_even_when_upstream_edits_the_file(self):
            """The inverse mistake: this fork's own preflight silently skipped because
            upstream touched an unrelated line. The MR would be opened with `check ok`
            printed and nothing checked."""
            fork = make_fork(self.tmp)
            flag = os.path.join(self.tmp, "ours-gate-ran")
            gate = 'gate = ["touch %s"]\n' % flag
            commit_upstream(self.tmp, CONFIG_FILE, gate, "theirs: a gate")
            self.trunk_from_upstream(fork)
            commit_upstream(self.tmp, CONFIG_FILE, "# upstream added a comment\n" + gate,
                            "theirs: a comment, and nothing else")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn("NOT RUN", out)
            self.assertIn("sh -c 'touch %s'" % flag, out)
            self.assertTrue(os.path.exists(flag))
            # the file did change, so the reviewer is still told to read the diff
            self.assertIn("this sync changes `%s`" % CONFIG_FILE, out)

        def test_the_untracked_gate_setup_leaves_behind_still_runs(self):
            """The default post-setup state, and the one `setup/SKILL.md` sends the user to:
            `.forkflow.toml` is untracked and holds this fork's own `gate`. Nothing in the
            merge touches it, so the preflight must run and the reviewer must not be sent to
            a diff that prints nothing. Reading the working tree on one side of the guard and
            a commit on the other made an untracked file read as "the merge changed it": the
            gate was skipped and the CHECK advisory fired on every sync, forever."""
            fork = make_fork(self.tmp)
            self.assertEqual(run("-C", fork, "setup")[0], 0)
            flag = os.path.join(self.tmp, "ours-gate-ran")
            with open(os.path.join(fork, CONFIG_FILE), "a") as fh:
                fh.write('gate = ["touch %s"]\n' % flag)
            self.assertIn("?? " + CONFIG_FILE,
                          sh("git", "status", "--porcelain", cwd=fork))     # still untracked
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: a doc")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn("NOT RUN", out)
            self.assertNotIn("this sync changes `%s`" % CONFIG_FILE, out)
            self.assertIn("sh -c 'touch %s'  -> passed" % flag, out)
            self.assertTrue(os.path.exists(flag))

            # `check` in the same clone runs the same gate: `sync` and `check` must not
            # disagree about whose shell the file holds
            os.unlink(flag)
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err + out)
            self.assertTrue(os.path.exists(flag))

        def test_a_merged_config_that_cannot_be_read_names_the_state_and_the_way_out(self):
            """`finish_sync` reloads the config from the merged tree, so an ordinary typo
            arriving in `.forkflow.toml` aborts the run *after* the mirror push, the backup,
            the merge commit and the branch. A bare parse error there said nothing about what
            had been done or how to get out, and every later command died on the same line."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, CONFIG_FILE, "gate = []\n", "theirs: forkflow too")
            self.trunk_from_upstream(fork)
            commit_upstream(self.tmp, CONFIG_FILE, "gate = [\n", "theirs: a typo")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, out + err)
            self.assertIn("cannot be read", err)
            self.assertIn("the merge commit and `sync/upstream-", err)     # what was done
            self.assertIn("forkflow sync --continue", err)                 # the way out
            self.assertEqual(origin_sha(fork, sync_branch_name()), "")     # nothing published

            # the route it names, followed literally
            write(fork, CONFIG_FILE, "gate = []\n")
            sh("git", "commit", "-am", "ours: fix the config the merge broke", cwd=fork)
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, sync_branch_name()), rev(fork, "HEAD"))

    class TestSyncConflicts(Base):
        """The conflict path, `sync --continue`, and the both-sides table."""

        BASE_TF = 'resource "null_resource" "a" {\n  count = 1\n}\n'

        def block(self, who: str) -> str:
            return '\nresource "null_resource" "%s" {\n  count = 1\n}\n' % who

        def conflicting_fork(self) -> str:
            """Both sides change the same line of shared.tf."""
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "shared.tf",
                            self.BASE_TF.replace("count = 1", "count = 3"), "theirs: shared")
            return fork

        def appending_fork(self) -> str:
            """Both sides append a different block: a conflict a resolution can keep both of."""
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf", self.BASE_TF + self.block("ours"),
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF + self.block("theirs"),
                            "theirs: shared")
            return fork

        def take_upstream_into_trunk(self, fork: str) -> None:
            """Test setup: publish upstream's tip as the trunk, as a merged sync MR would,
            so a later merge base carries the files both sides then change."""
            push_upstream_into_origin(self.tmp, "develop")
            sh("git", "fetch", "origin", cwd=fork)
            sh("git", "checkout", "develop", cwd=fork)
            sh("git", "reset", "--hard", "origin/develop", cwd=fork)

        def resolve(self, fork: str, text, path: str = "shared.tf") -> None:
            write(fork, path, text)
            sh("git", "add", path, cwd=fork)

        def row_for(self, out: str, path: str) -> str:
            rows = [ln.strip() for ln in out.splitlines()
                    if ln.startswith("    " + path) and ("ours " in ln or "CHECK" in ln)]
            self.assertTrue(rows, "no both-sides row for %s in:\n%s" % (path, out))
            return rows[0]

        def counts(self, row: str):
            m = re.search(r"ours (\d+)/(\d+)\s+theirs (\d+)/(\d+)", row)
            self.assertIsNotNone(m, "no counts in row: " + row)
            return tuple(int(g) for g in m.groups())

        def test_conflict_is_exit_4_with_markers_and_an_untouched_trunk(self):
            fork = self.conflicting_fork()
            name = sync_branch_name()
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 4, err + out)
            self.assertIn("forkflow sync --continue", err)
            self.assertIn("conflicting file(s)", out)
            self.assertIn("    shared.tf", out)
            with open(os.path.join(fork, "shared.tf")) as fh:
                self.assertIn("<<<<<<<", fh.read())
            self.assertEqual(checked_out(fork), name)                  # left on the sync branch
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertEqual(origin_sha(fork, name), "")               # nothing pushed yet
            # the mirror step ran before the merge and is complete on its own
            self.assertEqual(rev(fork, "refs/heads/main"), rev(fork, "upstream/main"))
            self.assertEqual(origin_sha(fork, "main"), rev(fork, "upstream/main"))

        @needs_tomllib
        def test_a_gate_that_arrived_in_the_merge_is_shown_and_not_run(self):
            """`gate` is the one config key that is *run* (`sh -c`), and `.forkflow.toml` is a
            tracked file a sync is designed to bring in from the original project. The run
            whose own merge wrote it prints the commands instead of obeying them."""
            fork = self.conflicting_fork()
            flag = os.path.join(self.tmp, "gate-ran")
            commit_upstream(self.tmp, CONFIG_FILE, 'gate = ["touch %s"]\n' % flag,
                            "theirs: a gate")
            self.assertEqual(run("-C", fork, "sync")[0], 4)          # shared.tf conflicts
            self.resolve(fork, self.BASE_TF.replace("count = 1", "count = 4"))

            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("this sync changes `%s`" % CONFIG_FILE, out)
            self.assertIn("NOT RUN", out)
            self.assertIn("would have run: sh -c 'touch %s'" % flag, out)
            self.assertFalse(os.path.exists(flag))

            # and it is only that run: a gate the fork now carries runs like any other, with
            # the provenance of the file it comes from said out loud
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 0, err + out)
            self.assertTrue(os.path.exists(flag))
            self.assertIn("is tracked by `upstream/main` too", out)

        @needs_tomllib
        def test_a_gate_that_arrived_stays_shown_after_a_commit_on_the_merge(self):
            """`--continue` supports a commit made on top of the merge - that is how a
            refused `check` is fixed. The merge is then no longer `HEAD`, and the gate it
            brought must still be shown rather than run."""
            fork = self.conflicting_fork()
            flag = os.path.join(self.tmp, "gate-ran")
            commit_upstream(self.tmp, CONFIG_FILE, 'gate = ["touch %s"]\n' % flag,
                            "theirs: a gate")
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            self.resolve(fork, self.BASE_TF.replace("count = 1", "count = 4"))
            self.assertEqual(run("-C", fork, "sync", "--continue")[0], 0)
            self.assertFalse(os.path.exists(flag))

            commit_fork(fork, "ours/fix.txt", "fixed\n", "ours: fix what check refused")
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("NOT RUN", out)
            self.assertIn("would have run: sh -c 'touch %s'" % flag, out)
            self.assertIn("this sync changes `%s`" % CONFIG_FILE, out)
            self.assertFalse(os.path.exists(flag))

        @needs_tomllib
        def test_a_gate_that_arrived_stays_shown_after_a_merge_on_the_merge(self):
            """The same fix, with a `git merge` on top instead of a commit: that is a merge
            commit too, and the *newest* merge on the branch is then not the sync merge.
            Resuming from it made `--continue` compare the gate against the merge's own first
            parent - the sync merge, which already carries upstream's `gate` - so the guard
            said nothing had changed and `sh -c` ran shell nobody had read."""
            fork = self.conflicting_fork()
            flag = os.path.join(self.tmp, "gate-ran")
            commit_upstream(self.tmp, CONFIG_FILE, 'gate = ["touch %s"]\n' % flag,
                            "theirs: a gate")
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            self.resolve(fork, self.BASE_TF.replace("count = 1", "count = 4"))
            self.assertEqual(run("-C", fork, "sync", "--continue")[0], 0)
            self.assertFalse(os.path.exists(flag))

            name = checked_out(fork)
            sh("git", "switch", "-c", "fix/x", cwd=fork)
            commit_fork(fork, "ours/fix.txt", "fixed\n", "ours: fix what check refused")
            sh("git", "switch", name, cwd=fork)
            sh("git", "merge", "--no-ff", "--no-edit", "fix/x", cwd=fork)

            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("NOT RUN", out)
            self.assertIn("would have run: sh -c 'touch %s'" % flag, out)
            self.assertIn("this sync changes `%s`" % CONFIG_FILE, out)
            self.assertFalse(os.path.exists(flag))

        def test_continue_after_a_resolution_that_keeps_both_sides(self):
            fork = self.appending_fork()
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            name = sync_branch_name()
            target = rev(fork, "upstream/main")
            before_trunk = origin_sha(fork, "develop")
            self.resolve(fork, self.BASE_TF + self.block("ours") + self.block("theirs"))

            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("merge commit", out)
            self.assertEqual(rev(fork, name + "^2"), target)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            row = self.row_for(out, "shared.tf")
            ours_met, ours_made, theirs_met, theirs_made = self.counts(row)
            self.assertTrue(ours_made and theirs_made, row)
            self.assertEqual((ours_met, theirs_met), (ours_made, theirs_made), row)
            self.assertNotIn("CHECK", row)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertEqual(checked_out(fork), name)

        def test_clean_file_changed_on_both_sides_is_listed_with_full_counts(self):
            fork = make_fork(self.tmp)
            base = ["line %02d" % i for i in range(1, 21)]
            commit_upstream(self.tmp, "notes.txt", "\n".join(base) + "\n", "theirs: notes")
            self.take_upstream_into_trunk(fork)
            ours = list(base)
            ours[1] = "line 02 - ours"
            commit_fork(fork, "notes.txt", "\n".join(ours) + "\n", "ours: top", push=True)
            theirs = list(base)
            theirs[18] = "line 19 - theirs"
            commit_upstream(self.tmp, "notes.txt", "\n".join(theirs) + "\n", "theirs: bottom")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            row = self.row_for(out, "notes.txt")
            self.assertEqual(self.counts(row), (2, 2, 2, 2), row)   # one added, one removed each
            self.assertNotIn("CHECK", row)
            merged = sh("git", "show", "HEAD:notes.txt", cwd=fork)
            self.assertIn("line 02 - ours", merged)
            self.assertIn("line 19 - theirs", merged)

        def test_a_deleted_line_is_judged_by_count_not_by_presence(self):
            """Task 6: a side's removed line counts as gone only when it occurs fewer times
            than in the base - presence alone would flag every removed `}` as surviving."""
            def blocks(pairs):
                out = []
                for i, n in pairs:
                    out += ["block %02d {" % i, "  n = %s" % n, "}"]
                return "\n".join(out) + "\n"

            fork = make_fork(self.tmp)
            base = [(i, i) for i in range(1, 7)]
            commit_upstream(self.tmp, "braces.txt", blocks(base), "theirs: braces")
            self.take_upstream_into_trunk(fork)
            commit_fork(fork, "braces.txt", blocks([p for p in base if p[0] != 2]),
                        "ours: drop block 02", push=True)
            commit_upstream(self.tmp, "braces.txt", blocks(base[:-1] + [(6, 60)]),
                            "theirs: last block")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            merged = sh("git", "show", "HEAD:braces.txt", cwd=fork)
            self.assertEqual(merged.count("}"), 5)      # one of the six `}` really is gone
            row = self.row_for(out, "braces.txt")
            ours_met, ours_made, theirs_met, theirs_made = self.counts(row)
            self.assertEqual(ours_made, 3, row)         # the block's three lines
            self.assertEqual((ours_met, theirs_met), (ours_made, theirs_made), row)
            self.assertNotIn("CHECK", row)

        def test_a_file_renamed_on_one_side_is_flagged_renamed(self):
            fork = make_fork(self.tmp)
            sh("git", "mv", "README.md", "DOCS.md", cwd=fork)
            sh("git", "commit", "-m", "ours: rename the readme", cwd=fork)
            sh("git", "push", "origin", "develop:refs/heads/develop", cwd=fork)
            commit_upstream(self.tmp, "README.md", "# project\n\nand more\n", "theirs: readme")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            row = self.row_for(out, "README.md")
            self.assertIn("CHECK renamed on one side", row)

        def test_refs_moving_between_the_runs_stay_out_of_the_table(self):
            fork = self.appending_fork()
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            name = sync_branch_name()
            target = rev(fork, "upstream/main")
            self.resolve(fork, self.BASE_TF + self.block("ours") + self.block("theirs"))

            # both long-lived refs move behind the sync branch's back
            commit_upstream(self.tmp, "docs/later.md", "later\n", "theirs: later")
            sh("git", "fetch", "upstream", cwd=fork)
            sh("git", "update-ref", "refs/heads/main", rev(fork, "upstream/main"), cwd=fork)
            second_clone_commit(self.tmp)
            self.assertNotEqual(rev(fork, "refs/heads/main"), target)

            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, name + "^2"), target)     # the merge that was made
            row = self.row_for(out, "shared.tf")
            ours_met, ours_made, theirs_met, theirs_made = self.counts(row)
            self.assertEqual((ours_met, theirs_met), (ours_made, theirs_made), row)
            self.assertNotIn("later.md", out)                    # not part of this sync

        def test_continue_names_the_backup_its_own_run_made(self):
            """The restore point a `--continue` reports must be this run's, not the newest
            `backup/*-pre-sync` branch in the repo."""
            fork = self.appending_fork()
            sh("git", "push", "origin", "develop:refs/heads/backup/29991231-000000-pre-sync",
               cwd=fork)
            sh("git", "branch", "backup/29991231-000000-pre-sync", "develop", cwd=fork)
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            mine = [b for b in local_branches(fork).splitlines()
                    if "-pre-sync" in b and "2999" not in b]
            self.assertEqual(len(mine), 1, local_branches(fork))
            mine = mine[0][len("refs/heads/"):]
            self.resolve(fork, self.BASE_TF + self.block("ours") + self.block("theirs"))

            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            body = [ln.strip()[len("description: "):] for ln in out.splitlines()
                    if ln.strip().startswith("description: ")][0]
            with open(body) as fh:
                text = fh.read()
            self.assertIn("`%s`" % mine, text)
            self.assertNotIn("2999", text)

        def test_continue_after_a_commit_on_top_of_the_merge(self):
            """Fixing what `check` refused means a commit on top of the merge: `--continue`
            has to resume from the merge, not refuse because HEAD is no longer it."""
            fork = self.appending_fork()
            name = sync_branch_name()
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            target = rev(fork, "upstream/main")          # what that run fetched and merged
            self.resolve(fork, self.BASE_TF + self.block("ours") + self.block("theirs"))
            sh("git", "commit", "--no-edit", cwd=fork)                  # the merge commit
            commit_fork(fork, "ours/fix.txt", "the gate is happy now\n", "ours: fix the gate")

            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("resuming from the merge commit", out)
            self.assertEqual(rev(fork, name + "^^2"), target)   # HEAD^ is the merge
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            row = self.row_for(out, "shared.tf")
            self.assertNotIn("CHECK", row)

        def test_continue_in_a_dry_run_over_an_uncommitted_merge(self):
            fork = self.appending_fork()
            name = sync_branch_name()
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            self.resolve(fork, self.BASE_TF + self.block("ours") + self.block("theirs"))
            before = rev(fork, "refs/heads/" + name)

            code, out, err = run("-C", fork, "sync", "--continue", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("the merge is not committed", out)
            self.assertTrue(merge_in_progress(ctx_for(fork)))    # still uncommitted
            self.assertEqual(rev(fork, "refs/heads/" + name), before)
            self.assertEqual(origin_sha(fork, name), "")

        def test_two_branches_with_no_merge_base_are_reported(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            sh("git", "checkout", "--orphan", "unrelated", cwd=fork)
            write(fork, "only.txt", "unrelated\n")
            sh("git", "add", "-A", cwd=fork)
            sh("git", "commit", "-m", "unrelated root", cwd=fork)
            rows, out, _ = capture(both_sides_survived, ctx, "develop", "unrelated")
            self.assertEqual(rows, [])
            self.assertIn("no merge base", out)

        def test_continue_with_unmerged_paths_is_exit_2(self):
            fork = self.conflicting_fork()
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 2)
            self.assertIn("still unmerged", out)
            self.assertIn("    shared.tf", out)
            self.assertIn("resolve the conflicts", err)
            self.assertEqual(origin_sha(fork, sync_branch_name()), "")

        def test_continue_off_a_sync_branch_and_with_nothing_to_continue(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 2)
            self.assertIn("--continue", err)
            self.assertIn("develop", err)

            sh("git", "checkout", "-b", sync_branch_name(), "develop", cwd=fork)
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 2)
            self.assertIn("nothing to continue", err)

        def test_a_resolution_that_drops_one_side_is_flagged_check(self):
            fork = self.appending_fork()
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            self.resolve(fork, self.BASE_TF + self.block("theirs"))   # ours' block dropped
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)                      # advisory, never a failure
            row = self.row_for(out, "shared.tf")
            self.assertIn("of ours did not survive", row)
            self.assertIn("CHECK: look at shared.tf", out)

        def test_delete_modify_collision_is_flagged_deleted(self):
            fork = make_fork(self.tmp)
            sh("git", "rm", "README.md", cwd=fork)
            sh("git", "commit", "-m", "ours: drop the readme", cwd=fork)
            sh("git", "push", "origin", "develop:refs/heads/develop", cwd=fork)
            commit_upstream(self.tmp, "README.md", "# project moved\n", "theirs: readme")

            self.assertEqual(run("-C", fork, "sync")[0], 4)
            sh("git", "rm", "-f", "README.md", cwd=fork)              # keep the deletion
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("CHECK deleted", self.row_for(out, "README.md"))

        def test_binary_changed_on_both_sides_is_flagged_binary(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "logo.bin", b"\x00\x01 base\n", "theirs: logo")
            self.take_upstream_into_trunk(fork)
            commit_fork(fork, "logo.bin", b"\x00\x01 ours\n", "ours: logo", push=True)
            commit_upstream(self.tmp, "logo.bin", b"\x00\x01 theirs\n", "theirs: logo again")

            self.assertEqual(run("-C", fork, "sync")[0], 4)
            sh("git", "checkout", "--ours", "--", "logo.bin", cwd=fork)
            sh("git", "add", "logo.bin", cwd=fork)
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("CHECK binary", self.row_for(out, "logo.bin"))

        def test_content_that_looks_like_a_diff_header_still_counts(self):
            """A content line is `+`/`-` plus the line itself, so a real `++x` reads as
            `+++x` and a real `--x` as `---x`. Skipping those as headers drops the very
            lines a side changed, and every check that side made then trivially passes."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            write(fork, "diffy.txt", "--- keep\nplain\n")
            sh("git", "add", "-A", cwd=fork)
            sh("git", "commit", "-m", "base", cwd=fork)
            mb = rev(fork, "HEAD")
            write(fork, "diffy.txt", "++ added\nplain\n")
            sh("git", "add", "-A", cwd=fork)
            sh("git", "commit", "-m", "side", cwd=fork)

            added, removed = side_lines(ctx, mb, "HEAD", "diffy.txt")
            self.assertEqual(added, ["++ added"])
            self.assertEqual(removed, ["--- keep"])
            # and a side whose only change was dropped must not read as "survived"
            merged = line_counts("plain\n")
            self.assertEqual(side_survived(added, removed, line_counts("--- keep\nplain\n"),
                                           merged), (1, 2))

    # ------------------------------------------------------------------- #
    # ship
    # ------------------------------------------------------------------- #

    class ShipBase(Base):
        BASE_TF = 'resource "null_resource" "a" {\n  count = 1\n}\n'

        def feature(self, fork: str, name: str = "feat/x", commits: int = 1,
                    push: bool = False) -> str:
            sh("git", "checkout", "-b", name, "develop", cwd=fork)
            for i in range(commits):
                commit_fork(fork, "ours/f%d.txt" % i, "line %d\n" % i,
                            "ours: step %d" % i, push=push)
            return name

        def backup_branch(self, fork: str) -> str:
            out = sh("git", "for-each-ref", "--format=%(refname:short)",
                     "refs/heads/" + DEFAULT_BACKUP_PREFIX + "*", cwd=fork)
            return out.splitlines()[0] if out else ""

        def message_of(self, fork: str, ref: str = "HEAD") -> str:
            return sh("git", "log", "-1", "--format=%B", ref, cwd=fork).strip()

        def conflicting_ship(self) -> Tuple[str, str]:
            """A feature branch and origin/develop that changed the same line."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=0)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared")
            second_clone_commit(self.tmp, path="shared.tf",
                                content=self.BASE_TF.replace("count = 1", "count = 9"))
            return fork, name

    class TestShip(ShipBase):
        def test_three_commits_become_one_on_the_trunk_tip(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=3)
            second_clone_commit(self.tmp)                  # origin/develop moved underneath us
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)

            self.assertEqual(rev(fork, "HEAD^"), before_trunk)          # on the trunk's tip
            self.assertEqual(sh("git", "rev-list", "--count",
                                before_trunk + "..HEAD", cwd=fork), "1")
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)  # the trunk is never pushed
            self.assertEqual(sorted(sh("git", "diff", "--name-only", before_trunk, "HEAD",
                                       cwd=fork).splitlines()),
                             ["ours/f0.txt", "ours/f1.txt", "ours/f2.txt"])
            self.assertTrue(os.path.exists(os.path.join(fork, "ours", "other.txt")))

            backup_ref = self.backup_branch(fork)
            self.assertTrue(backup_ref)
            self.assertEqual(origin_sha(fork, backup_ref), rev(fork, backup_ref))
            # everything the branch contributed survived the rebase and the squash
            self.assertEqual(sh("git", "diff", backup_ref, "HEAD", "--", "ours/f0.txt",
                                "ours/f1.txt", "ours/f2.txt", cwd=fork), "")
            self.assertIn("unchanged", out)

            msg = self.message_of(fork)
            self.assertTrue(msg.startswith("ours: step 0"), msg)        # oldest subject
            self.assertIn("Squashed from 3 commits (oldest first):", msg)
            self.assertLess(msg.index("- ours: step 0"), msg.index("- ours: step 2"))

        def test_branch_already_on_origin_is_pushed_with_a_lease(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=2, push=True)
            second_clone_commit(self.tmp)
            before = origin_sha(fork, name)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--force-with-lease=%s:%s" % (name, before), out)
            self.assertNotIn("--force ", out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))

        def test_single_commit_keeps_its_message(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            write(fork, "ours/a.txt", "a\n")
            sh("git", "add", "-A", cwd=fork)
            sh("git", "commit", "-m", "ours: only one\n\nwith a body line", cwd=fork)
            second_clone_commit(self.tmp)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.message_of(fork), "ours: only one\n\nwith a body line")
            self.assertNotIn("Squashed from", self.message_of(fork))

        def test_message_file_is_used_verbatim(self):
            fork = make_fork(self.tmp)
            self.feature(fork, commits=2)
            second_clone_commit(self.tmp)
            path = write(self.tmp, "msg.txt", "ship: a hand written subject\n\nand a body\n")

            code, out, err = run("-C", fork, "ship", "--message-file", path)
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.message_of(fork), "ship: a hand written subject\n\nand a body")
            self.assertIn("ship: a hand written subject", out)     # the MR title

        def test_nothing_to_ship_creates_no_backup(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertIn("nothing to ship", out)
            self.assertNotIn(DEFAULT_BACKUP_PREFIX, local_branches(fork))
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertEqual(origin_sha(fork, "feat/x"), "")

        def test_already_upstream_commits_are_nothing_to_ship_after_the_rebase(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            # the same change reaches origin/develop by another route: the rebase drops it
            sh("git", "push", os.path.join(self.tmp, "origin.git"),
               "%s:refs/heads/develop" % name, cwd=fork)
            sh("git", "fetch", "origin", cwd=fork)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertIn("nothing to ship", out)

        def test_dry_run_changes_nothing(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=2)
            second_clone_commit(self.tmp)
            before_head = rev(fork, "HEAD")
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "ship", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would:", out)
            self.assertEqual(rev(fork, "HEAD"), before_head)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertEqual(origin_sha(fork, name), "")
            self.assertNotIn(DEFAULT_BACKUP_PREFIX, local_branches(fork))

        def test_dry_run_shows_the_upstream_tracked_warning(self):
            """`ship/SKILL.md` step 2 tells Claude to show that list from the dry run."""
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            commit_fork(fork, "src/app.py", "def main():\n    return 42\n", "ours: bump app")
            commit_fork(fork, "ours/new.txt", "ours only\n", "ours: new")
            code, out, err = run("-C", fork, "ship", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("touches upstream-tracked files (WARNING, 1)", out)
            self.assertIn("    src/app.py", out)
            self.assertNotIn("ours/new.txt", out)

        @needs_tomllib
        def test_custom_trunk_name_is_the_rebase_target(self):
            fork = make_fork(self.tmp, trunk="trunk", config='trunk = "trunk"\n')
            sh("git", "checkout", "-b", "feat/x", "trunk", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n", "ours: first")
            second_clone_commit(self.tmp, branch="trunk")
            before_trunk = origin_sha(fork, "trunk")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "HEAD^"), before_trunk)
            self.assertEqual(origin_sha(fork, "trunk"), before_trunk)

    class TestShipErrors(ShipBase):
        def test_on_the_trunk_is_exit_2(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("is the trunk", err)

        def test_on_the_mirror_is_exit_2(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "main", cwd=fork)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("is the mirror", err)

        def test_on_a_sync_branch_is_exit_2(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", sync_branch_name(), "develop", cwd=fork)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("sync branch", err)

        def test_on_a_backup_branch_is_exit_2(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", DEFAULT_BACKUP_PREFIX + "20260101-000000-pre-ship",
               "develop", cwd=fork)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("backup branch", err)

        def test_dirty_tree_is_exit_2(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            write(fork, "README.md", "# dirty\n")
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("uncommitted changes", err)

        def test_detached_head_is_exit_2(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            sh("git", "checkout", "--detach", "HEAD", cwd=fork)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("detached", err)

        def test_rebase_conflict_then_continue(self):
            fork, name = self.conflicting_ship()
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 4, err + out)
            self.assertIn("git rebase --continue", err)
            self.assertIn("forkflow ship --continue", err)
            self.assertIn("shared.tf", out)
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertEqual(origin_sha(fork, name), "")
            self.assertTrue(self.backup_branch(fork))

            # a second `ship` while the rebase is stopped refuses to do anything, and this
            # clone does have a ship to resume - so the hint names `--continue`
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("rebase is in progress", err)
            self.assertIn("forkflow ship --continue", err)

            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"))
            sh("git", "add", "shared.tf", cwd=fork)
            sh("git", "rebase", "--continue", cwd=fork)

            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "HEAD^"), before_trunk)
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertIn("count = 2", sh("git", "show", "HEAD:shared.tf", cwd=fork))

        def test_the_rebase_hint_names_plain_ship_when_there_is_no_ship_to_resume(self):
            """`ship`'s own advice sends the user into `git pull --rebase` when a teammate's
            commit is on `origin/<branch>`. That rebase is not a ship: `--continue` has no
            backup and no lease to resume from and refuses, so the hint must not name it."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=0)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            second_clone_commit(self.tmp, branch=name, path="shared.tf",
                                content=self.BASE_TF.replace("count = 1", "count = 9"))
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 5"),
                        "ours: shared again")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2, err + out)
            self.assertIn("git pull --rebase origin " + name, err)

            sh("git", "pull", "--rebase", "origin", name, cwd=fork, check=False)
            self.assertTrue(rebase_in_progress(ctx_for(fork)))
            self.assertEqual(rebasing_branch(ctx_for(fork)), name)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2, err + out)
            self.assertIn("rebase is in progress", err)
            self.assertIn("`forkflow ship`", err)
            self.assertNotIn("ship --continue", err)      # `git rebase --continue` is fine
            # and the tool agrees: `--continue` is not the command this state resumes
            self.resolve_conflict(fork)
            sh("git", "rebase", "--continue", cwd=fork)
            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 2, err + out)
            self.assertIn("no ship to continue", err)

        def resolve_conflict(self, fork: str, count: str = "5") -> None:
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = " + count))
            sh("git", "add", "shared.tf", cwd=fork)

        def test_continue_on_the_trunk_is_exit_2(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 2)
            self.assertIn("is the trunk", err)

        def test_continue_after_rebase_abort_is_exit_2(self):
            fork, name = self.conflicting_ship()
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 4, err + out)
            sh("git", "rebase", "--abort", cwd=fork)

            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 2)
            self.assertIn("rebase did not complete", err)
            self.assertEqual(origin_sha(fork, name), "")

        def test_continue_without_a_recorded_ship_is_refused(self):
            """`--continue` force-pushes; without the run that made the backup there is no
            restore point behind it, so it must refuse before anything is rewritten."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            theirs = second_clone_commit(self.tmp, branch=name, path="ours/theirs.txt",
                                         content="theirs\n")
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 2, err + out)
            self.assertIn("no ship to continue", err)
            self.assertEqual(origin_sha(fork, name), theirs)       # their commit survives
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertNotIn(DEFAULT_BACKUP_PREFIX, local_branches(fork))

        def test_a_branch_amended_after_it_was_published_is_this_clone_s_own(self):
            """Rewriting a published feature branch and force-pushing it behind a lease and a
            backup is what `ship` is for (rule 4). `origin/<branch>` not being an ancestor of
            HEAD is that ordinary case, not a reason to refuse."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            published = origin_sha(fork, name)
            sh("git", "commit", "--amend", "-m", "ours: step 0, amended", cwd=fork)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertIn("this clone's own earlier", out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))
            self.assertNotEqual(origin_sha(fork, name), published)
            self.assertIn("--force-with-lease=%s:%s" % (name, published), out)

        def test_a_pulled_then_reset_teammate_commit_is_still_refused(self):
            """An ordinary `git pull` puts a teammate's commit into `refs/heads/<branch>`'s
            reflog. Having once been in that reflog is not permission to force-push it away:
            only a push this clone made, or a backup that still carries it, is."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            mine = rev(fork, "HEAD")
            theirs = second_clone_commit(self.tmp, branch=name, path="ours/theirs.txt",
                                         content="theirs\n")
            sh("git", "pull", "--ff-only", "origin", name, cwd=fork)
            self.assertIn(theirs, sh("git", "reflog", "show", "--format=%H",
                                     "refs/heads/" + name, cwd=fork))
            sh("git", "reset", "--hard", mine, cwd=fork)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2, err + out)
            self.assertIn("no record of publishing", err)
            self.assertEqual(origin_sha(fork, name), theirs)       # their commit survives
            self.assertNotIn(DEFAULT_BACKUP_PREFIX, local_branches(fork))

        def test_a_tip_kept_in_a_backup_on_origin_may_be_replaced(self):
            """The refusal above names a way through, and it has to be a real one: a tip a
            backup on origin still carries loses nothing by being replaced, which is the whole
            of what rule 4 asks of a rewrite."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            theirs = second_clone_commit(self.tmp, branch=name, path="ours/theirs.txt",
                                         content="theirs\n")
            sh("git", "fetch", "origin", cwd=fork)
            keep = DEFAULT_BACKUP_PREFIX + "kept-theirs"
            sh("git", "branch", keep, theirs, cwd=fork)
            sh("git", "push", "origin", "refs/heads/%s:refs/heads/%s" % (keep, keep), cwd=fork)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, keep), theirs)       # still there afterwards
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))

        def test_a_hand_pushed_tip_is_this_clone_s_own_without_any_state_file(self):
            """`git push` writes `update by push` on the remote-tracking ref, and a fetch or a
            pull does not: that is what tells our own publication from an arrival, whether or
            not the branch was ever published through forkflow."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            published = origin_sha(fork, name)
            self.assertFalse(os.path.exists(state_path(ctx_for(fork))))
            self.assertTrue(pushed_from_here(ctx_for(fork), name, published))
            sh("git", "commit", "--amend", "-m", "ours: step 0, amended", cwd=fork)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--force-with-lease=%s:%s" % (name, published), out)

        def test_ship_records_what_it_published(self):
            """The record outlives a reflog that expired or was switched off."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            ctx = ctx_for(fork)
            self.assertTrue(published_here(ctx, name, origin_sha(fork, name)))
            self.assertFalse(published_here(ctx, name, rev(fork, "refs/heads/develop")))
            # backups are pushed once and never rewritten: nothing has to prove one is ours
            self.assertNotIn(DEFAULT_BACKUP_PREFIX,
                             str(read_state(ctx).get("published")))

        def test_a_commit_only_on_origin_is_refused_before_any_backup(self):
            """A lease proves nobody pushed after our fetch - not that what it found is ours."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            theirs = second_clone_commit(self.tmp, branch=name, path="ours/theirs.txt",
                                         content="theirs\n")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2, err + out)
            self.assertIn("carries commits", err)
            self.assertEqual(origin_sha(fork, name), theirs)
            self.assertNotIn(DEFAULT_BACKUP_PREFIX, local_branches(fork))

        def test_stale_lease_is_exit_5_with_the_trunk_unchanged(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=1, push=True)
            before_trunk = origin_sha(fork, "develop")
            stale = origin_sha(fork, name)
            # a narrowed refspec keeps `git fetch origin` from refreshing origin/<feature>,
            # so the lease the push carries is the one this clone last saw
            sh("git", "config", "remote.origin.fetch",
               "+refs/heads/develop:refs/remotes/origin/develop", cwd=fork)
            second_clone_commit(self.tmp, branch=name, path="ours/rival.txt", content="rival\n")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 5, err + out)
            self.assertEqual(rev(fork, "refs/remotes/origin/" + name), stale)
            self.assertNotEqual(origin_sha(fork, name), rev(fork, "HEAD"))
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertIn("rollback: git reset --hard origin/" + DEFAULT_BACKUP_PREFIX, out)

        @needs_tomllib
        def test_gate_failure_after_the_squash_is_exit_3_with_the_rollback_line(self):
            fork = make_fork(self.tmp, config='gate = ["echo gate-said-no; exit 2"]\n')
            name = self.feature(fork, commits=2)
            second_clone_commit(self.tmp)
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 3, err + out)
            self.assertIn("gate-said-no", out)
            self.assertIn("rollback: git reset --hard origin/" + DEFAULT_BACKUP_PREFIX, out)
            self.assertEqual(origin_sha(fork, name), "")           # nothing was pushed
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            # the squash happened, so the rollback line is the way back
            self.assertEqual(sh("git", "rev-list", "--count",
                                "origin/develop..HEAD", cwd=fork), "1")

        @needs_tomllib
        def test_a_gate_failure_on_a_published_branch_is_resumable(self):
            """exit 3 leaves the branch squashed, so `origin/<branch>` is no longer in it. The
            hint has to name `--continue`, and neither that nor a fresh `ship` may refuse the
            branch's own pre-ship state - the old advice was to delete the published branch,
            which closes the open merge request."""
            flag = os.path.join(self.tmp, "gate-ok")
            fork = make_fork(self.tmp, config='gate = ["test -f %s"]\n' % flag)
            name = self.feature(fork, commits=2, push=True)
            published = origin_sha(fork, name)

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 3, err + out)
            self.assertIn("forkflow ship --continue", out)
            self.assertEqual(origin_sha(fork, name), published)      # nothing was pushed

            write(self.tmp, "gate-ok", "")
            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))
            self.assertEqual(sh("git", "rev-list", "--count",
                                "origin/develop..HEAD", cwd=fork), "1")

        @needs_tomllib
        def test_ship_run_again_after_a_gate_failure_is_not_refused(self):
            """The same state, resumed the other way: a plain rerun of `ship`. Its own squash
            is what put `origin/<branch>` outside the branch, so refusing it would make exit 3
            a dead end for every published branch."""
            flag = os.path.join(self.tmp, "gate-ok")
            fork = make_fork(self.tmp, config='gate = ["test -f %s"]\n' % flag)
            name = self.feature(fork, commits=2, push=True)
            published = origin_sha(fork, name)
            self.assertEqual(run("-C", fork, "ship")[0], 3)

            write(self.tmp, "gate-ok", "")
            next_utc_second()                              # a second pre-ship backup
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn("push %s :%s" % ("origin", name), out)   # no "delete it" advice
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))
            self.assertNotEqual(origin_sha(fork, name), published)

        @needs_tomllib
        def test_the_recorded_lease_resumes_a_rerun_when_the_reflog_is_gone(self):
            """Reflogs expire and can be switched off, so the run's own record has to stand
            on its own: the lease the interrupted `ship` fetched is the tip it may replace."""
            flag = os.path.join(self.tmp, "gate-ok")
            fork = make_fork(self.tmp, config='gate = ["test -f %s"]\n' % flag)
            name = self.feature(fork, commits=2, push=True)
            published = origin_sha(fork, name)
            self.assertEqual(run("-C", fork, "ship")[0], 3)

            os.remove(os.path.join(fork, ".git", "logs", "refs", "remotes", "origin", name))
            ctx = ctx_for(fork)
            self.assertFalse(pushed_from_here(ctx, name, published))
            self.assertFalse(published_here(ctx, name, published))
            self.assertEqual(resumable(ctx, "ship", name)["lease"], published)

            write(self.tmp, "gate-ok", "")
            next_utc_second()                              # a second pre-ship backup
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))

        def test_continue_after_a_rebase_that_dropped_everything_is_nothing_to_ship(self):
            """`git rebase --skip` can leave the branch exactly on the trunk's tip; there is
            then nothing to squash, and that is exit 0, not a failure to squash nothing."""
            fork, name = self.conflicting_ship()
            self.assertEqual(run("-C", fork, "ship")[0], 4)
            sh("git", "rebase", "--skip", cwd=fork)
            self.assertEqual(rev(fork, "HEAD"), rev(fork, "refs/remotes/origin/develop"))

            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertIn("nothing to ship", out)
            self.assertEqual(origin_sha(fork, name), "")

        def test_a_branch_git_reads_as_a_force_flag_is_refused_by_the_preflight(self):
            """`+plus` would make `push`'s refspec a force-push of `plus`. Refused before the
            backup and the squash, not after them."""
            fork = make_fork(self.tmp)
            sh("git", "branch", "--no-track", "plus", "develop", cwd=fork)
            sh("git", "switch", "-c", "+plus", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n", "ours: a")
            head = rev(fork, "HEAD")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2, err + out)
            self.assertIn("refspec grammar", err)
            self.assertEqual(rev(fork, "HEAD"), head)                # nothing was squashed
            self.assertEqual(origin_sha(fork, "+plus"), "")
            self.assertEqual(origin_sha(fork, "plus"), "")
            self.assertNotIn(DEFAULT_BACKUP_PREFIX, local_branches(fork))

        def test_a_squash_that_cannot_be_committed_leaves_the_branch_as_it_was(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=2)
            second_clone_commit(self.tmp)
            real_rc = git_rc

            def refusing_commit(*args, **kw):
                if args[:1] == ("commit",):
                    return (1, "", "fatal: the test refuses this commit")
                return real_rc(*args, **kw)

            with mock.patch.object(sys.modules[__name__], "git_rc", refusing_commit):
                code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 5, err + out)
            self.assertIn("cannot commit the squashed change", err)
            # `reset --soft` put the branch's own commits back
            self.assertEqual(sh("git", "rev-list", "--count",
                                "origin/develop..HEAD", cwd=fork), "2")
            self.assertEqual(origin_sha(fork, name), "")

        def test_a_squash_that_changed_the_tree_is_exit_5_and_pushes_nothing(self):
            """The tree hash is the proof the squash lost nothing (plan, Task 7): a differing
            one stops the run before the push and leaves no squashed commit behind."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=2)
            second_clone_commit(self.tmp)
            before_trunk = origin_sha(fork, "develop")
            module = sys.modules[__name__]
            real_git, seen = git, []

            def lying_git(*args, **kw):
                out = real_git(*args, **kw)
                if args[:2] == ("rev-parse", "HEAD^{tree}"):
                    seen.append(out)
                    if len(seen) > 1:               # the tree read back after the squash commit
                        return "0" * 40
                return out

            with mock.patch.object(module, "git", lying_git):
                code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 5, err + out)
            self.assertIn("TREE MISMATCH", out)
            self.assertIn("refusing to push a squash that changed the result", err)
            self.assertIn("rollback: git reset --hard origin/" + DEFAULT_BACKUP_PREFIX, out)
            self.assertEqual(origin_sha(fork, name), "")            # nothing reached origin
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            # the branch is back on its own commits: no squash was left behind
            self.assertEqual(sh("git", "rev-list", "--count",
                                "origin/develop..HEAD", cwd=fork), "2")

        def test_push_rejection_is_exit_5_with_the_rollback_line(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=2)
            second_clone_commit(self.tmp)
            reject_pushes(self.tmp, "refs/heads/feat/*")
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 5, err + out)
            self.assertIn("rollback: git reset --hard origin/" + DEFAULT_BACKUP_PREFIX, out)
            self.assertEqual(origin_sha(fork, name), "")
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)

    # ------------------------------------------------------------------- #
    # setup: remotes, names, mirror, trunk bootstrap, config, template
    # ------------------------------------------------------------------- #

    class SetupBase(Base):
        def cfg(self, repo: str, key: str) -> str:
            return sh("git", "config", "--get", key, cwd=repo, check=False)

        def push_url(self, repo: str, remote: str = "upstream") -> str:
            return sh("git", "remote", "get-url", "--push", remote, cwd=repo, check=False)

        def toml_path(self, repo: str) -> str:
            return os.path.join(repo, CONFIG_FILE)

        def untouched(self, repo: str) -> None:
            """What every failing `setup` must leave exactly as it found it."""
            remotes = sh("git", "remote", cwd=repo).split()
            if "upstream" in remotes:            # with no such remote there is no URL to check
                self.assertNotEqual(self.push_url(repo), "DISABLED")
            self.assertEqual(self.cfg(repo, "pull.ff"), "")
            self.assertEqual(self.cfg(repo, "rerere.enabled"), "")
            self.assertFalse(os.path.exists(self.toml_path(repo)))

    class TestSetup(SetupBase):
        def test_configures_the_clone_and_is_idempotent(self):
            fork = make_fork(self.tmp)
            sh("git", "symbolic-ref", "refs/remotes/origin/HEAD",
               "refs/remotes/origin/main", cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(sh("git", "symbolic-ref", "refs/remotes/origin/HEAD", cwd=fork),
                             "refs/remotes/origin/develop")      # set-head -a corrected it
            self.assertEqual(self.push_url(fork), "DISABLED")
            self.assertEqual(self.cfg(fork, "branch.develop.mergeOptions"), "--ff-only")
            self.assertEqual(self.cfg(fork, "branch.main.mergeOptions"), "--ff-only")
            self.assertEqual(self.cfg(fork, "pull.ff"), "only")
            self.assertEqual(self.cfg(fork, "rerere.enabled"), "true")
            with open(self.toml_path(fork)) as fh:
                template = fh.read()
            self.assertIn('# trunk = "develop"', template)
            self.assertIn('# mirror = "main"', template)
            self.assertIn('# merge = "manual"', template)
            self.assertIn("enables --merge", template)
            self.assertIn("commit " + CONFIG_FILE, out)
            self.assertNotIn("would:", out)

            branches, trunk_sha = local_branches(fork), origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertIn("already DISABLED", out)
            self.assertIn("already set", out)
            self.assertIn("already there", out)
            self.assertEqual(local_branches(fork), branches)
            self.assertEqual(origin_sha(fork, "develop"), trunk_sha)
            with open(self.toml_path(fork)) as fh:
                self.assertEqual(fh.read(), template)

        def test_no_template_is_written_on_a_python_that_cannot_read_one(self):
            """`load_config` refuses a config file it cannot parse, so the template `setup`
            leaves behind would turn every later command - `setup` included - into exit 2 on
            the 3.9/3.10 the script otherwise supports."""
            fork = make_fork(self.tmp)
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                code, out, err = run("-C", fork, "setup")
                self.assertEqual(code, 0, err + out)
                self.assertIn("needs Python 3.11+", out)
                self.assertFalse(os.path.exists(self.toml_path(fork)))
                for argv in (("status",), ("check",), ("setup",)):
                    code, out, err = run("-C", fork, *argv)
                    self.assertEqual(code, 0, " ".join(argv) + ": " + err + out)

        def test_every_upstream_push_url_is_disabled_not_only_the_first(self):
            """`remote.<name>.pushurl` is multi-valued and git pushes to all of them, while
            `set-url --push` replaces only the first: a second live URL would survive a run
            that reports `DISABLED`."""
            fork = make_fork(self.tmp)
            first = sh("git", "remote", "get-url", "upstream", cwd=fork)
            sh("git", "config", "remote.upstream.pushurl", first, cwd=fork)
            sh("git", "config", "--add", "remote.upstream.pushurl",
               "https://example.invalid/second.git", cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(push_urls(fork, "upstream"), ["DISABLED"])
            self.assertIn("--unset-all", out)

        def test_a_non_standard_remote_name_is_used_as_it_is(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "rename", "upstream", "original", cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertIn("git remote rename original upstream", out)
            self.assertEqual(self.push_url(fork, "original"), "DISABLED")
            self.assertIn("upstream=original/main", out)

        @needs_tomllib
        def test_the_upstream_flag_is_used_and_written_to_the_config(self):
            """With more than one non-origin remote nothing can be inferred: the name the
            flag gave has to outlive the run that gave it."""
            fork = make_fork(self.tmp)
            sh("git", "remote", "rename", "upstream", "up", cwd=fork)
            sh("git", "remote", "add", "vendor", os.path.join(self.tmp, "upstream.git"), cwd=fork)
            code, out, err = run("-C", fork, "setup", "--upstream", "up")
            self.assertEqual(code, 0, err + out)
            self.assertIn("upstream=up/main", out)
            self.assertEqual(load_config(fork)["upstream"], "up")
            self.assertEqual(self.push_url(fork, "up"), "DISABLED")
            # the next run needs no flag: without the config key it would be exit 2
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err + out)
            self.assertIn("up/main", out)

        def test_an_upstream_url_for_an_existing_remote_is_reported_and_ignored(self):
            fork = make_fork(self.tmp)
            before = sh("git", "remote", "get-url", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "setup",
                                 "--upstream-url", "https://example.invalid/other.git")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--upstream-url is ignored", out)
            self.assertEqual(sh("git", "remote", "get-url", "upstream", cwd=fork), before)

        @needs_tomllib
        def test_trunk_and_mirror_flags_are_written_and_other_keys_survive(self):
            fork = make_fork(self.tmp, trunk="trunk", mirror="upstream-main",
                             config='gate = ["true"]\n')
            code, out, err = run("-C", fork, "setup",
                                 "--trunk", "trunk", "--mirror", "upstream-main")
            self.assertEqual(code, 0, err + out)
            cfg = load_config(fork)
            self.assertEqual(cfg["trunk"], "trunk")
            self.assertEqual(cfg["mirror"], "upstream-main")
            self.assertEqual(cfg["gate"], ["true"])
            self.assertEqual(self.cfg(fork, "branch.trunk.mergeOptions"), "--ff-only")
            self.assertEqual(self.cfg(fork, "branch.upstream-main.mergeOptions"), "--ff-only")

        def test_config_with_keys_rewrites_in_place_and_keeps_comments(self):
            text = ('# forkflow\n'
                    '# trunk = "develop"    # protected, MR-only\n'
                    'gate = ["true"]\n')
            out = config_with_keys(text, [("trunk", "mainline"), ("mirror", "vendor")])
            self.assertIn('trunk = "mainline"    # protected, MR-only', out)
            self.assertIn('gate = ["true"]', out)
            self.assertIn('mirror = "vendor"', out)        # appended: it was not there
            self.assertNotIn('"develop"', out)

        def test_config_with_keys_leaves_a_key_inside_a_table_alone(self):
            """`load_config` reads top-level keys only, so `trunk` inside a table is a
            different setting: rewriting that one would report a persistence no later
            command ever reads back."""
            text = 'gate = ["true"]\n\n[extra]\ntrunk = "in-a-table"\n'
            out = config_with_keys(text, [("trunk", "mainline")])
            self.assertIn('trunk = "in-a-table"', out)          # left exactly as it was
            self.assertLess(out.index('trunk = "mainline"'), out.index("[extra]"))
            if has_tomllib:
                import tomllib
                data = tomllib.loads(out)
                self.assertEqual(data["trunk"], "mainline")
                self.assertEqual(data["extra"]["trunk"], "in-a-table")

        def test_a_trunk_that_cannot_be_confirmed_is_not_bootstrapped(self):
            """`ls-remote` failing is not "the trunk is absent". The remote-tracking refs
            have already said nothing, so bootstrapping on a guess pushes a trunk that may
            be there - outside a merge request, the one way it may move (rule 2)."""
            fork = make_fresh_fork(self.tmp)
            ctx = ctx_for(fork, need_trunk=False, strict_mirror=False)
            target = rev(fork, "refs/remotes/upstream/main")
            sh("git", "remote", "set-url", "origin",
               os.path.join(self.tmp, "no-such-repo.git"), cwd=fork)
            with self.assertRaises(Fail) as cm:
                capture(setup_trunk, ctx, target)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("refusing to create the trunk", str(cm.exception))
            self.assertNotIn("refs/heads/develop", local_branches(fork))

        def test_config_with_keys_escapes_and_stays_out_of_a_table(self):
            text = 'gate = ["true"]\n\n[extra]\nkey = "value"\n'
            out = config_with_keys(text, [("trunk", 'we"ird\\name')])
            self.assertIn('trunk = "we\\"ird\\\\name"', out)
            self.assertLess(out.index("trunk ="), out.index("[extra]"))
            if has_tomllib:
                import tomllib
                self.assertEqual(tomllib.loads(out)["trunk"], 'we"ird\\name')

        def test_upstream_url_adds_the_remote_sets_both_heads_and_bootstraps(self):
            fork = make_fresh_fork(self.tmp)
            sh("git", "remote", "remove", "upstream", cwd=fork)
            url = os.path.join(self.tmp, "upstream.git")
            code, out, err = run("-C", fork, "setup", "--upstream-url", url)
            self.assertEqual(code, 0, err + out)
            self.assertEqual(sh("git", "remote", "get-url", "upstream", cwd=fork), url)
            self.assertEqual(sh("git", "symbolic-ref", "refs/remotes/upstream/HEAD", cwd=fork),
                             "refs/remotes/upstream/main")
            self.assertEqual(sh("git", "symbolic-ref", "refs/remotes/origin/HEAD", cwd=fork),
                             "refs/remotes/origin/main")
            target = rev(fork, "refs/remotes/upstream/main")
            self.assertEqual(origin_sha(fork, "develop"), target)

        def test_a_fresh_fork_gets_a_trunk_equal_to_upstream(self):
            fork = make_fresh_fork(self.tmp)
            self.assertEqual(origin_sha(fork, "develop"), "")
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            target = rev(fork, "refs/remotes/upstream/main")
            self.assertEqual(rev(fork, "refs/heads/develop"), target)
            self.assertEqual(origin_sha(fork, "develop"), target)
            self.assertEqual(self.cfg(fork, "branch.develop.remote"), "")     # --no-track
            self.assertEqual(self.cfg(fork, "branch.develop.merge"), "")

        def thin_clone(self, name: str = "thin") -> str:
            """A `clone --single-branch -b develop` of the fork: `origin/main` is on origin,
            but this clone's fetch refspec hides it.

            `--no-local`: a local clone hardlinks the whole object database, so only a real
            transfer leaves this clone without the objects the other branches carry."""
            thin = os.path.join(self.tmp, name)
            sh("git", "clone", "--no-local", "--single-branch", "--branch", "develop",
               os.path.join(self.tmp, "origin.git"), thin)
            identity(thin)
            sh("git", "remote", "add", "upstream", os.path.join(self.tmp, "upstream.git"), cwd=thin)
            self.assertEqual(rev(thin, "refs/heads/main"), "")
            self.assertEqual(rev(thin, "refs/remotes/origin/main"), "")
            return thin

        def test_a_single_branch_clone_gets_a_local_mirror(self):
            make_fork(self.tmp)
            thin = self.thin_clone()

            code, out, err = run("-C", thin, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(thin, "refs/heads/main"), rev(thin, "refs/remotes/upstream/main"))
            self.assertEqual(self.cfg(thin, "branch.main.remote"), "")        # --no-track
            # origin was asked, and answered with a mirror that is a pure copy of upstream
            self.assertIn("git ls-remote --heads origin main", out)
            self.assertIn("widen the refspec", out)

        def test_a_published_mirror_that_carries_work_is_refused_in_a_thin_clone(self):
            """`origin/<mirror>` missing here is not proof the fork has no mirror: creating
            one on that silence accepts a fork whose published mirror carries its own work,
            and defers the migration to a `sync` that dies on a non-fast-forward push."""
            fork = make_fork(self.tmp)
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours/local.txt", "ours\n", "ours: on the mirror", push=True)
            thin = self.thin_clone()
            self.assertEqual(sh("git", "cat-file", "-t", origin_sha(fork, "main"),
                                cwd=thin, check=False), "")   # a single-branch clone lacks it

            code, out, err = run("-C", thin, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("Adopting forkflow", err)
            self.assertIn("does not have", err)
            self.assertIn("Widen the refspec and fetch (git config --add "
                          "remote.origin.fetch +refs/heads/main:refs/remotes/origin/main", err)
            self.assertEqual(rev(thin, "refs/heads/main"), "")
            self.untouched(thin)

        def test_a_published_mirror_ahead_of_upstream_is_refused_when_it_is_known(self):
            """The same refusal with the commit in hand: `origin/<mirror>` is not in
            `upstream/<branch>`, so it is not a mirror - and forkflow never resets it."""
            fork = make_fork(self.tmp)
            commit_fork(fork, "ours/feature.txt", "ours\n", "ours: on the trunk", push=True)
            sh("git", "push", "origin", "develop:refs/heads/main", cwd=fork)
            thin = self.thin_clone()
            self.assertEqual(sh("git", "cat-file", "-t", origin_sha(fork, "main"),
                                cwd=thin, check=False), "commit")   # develop's tip, fetched

            code, out, err = run("-C", thin, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("Adopting forkflow", err)
            self.assertIn("never resets", err)
            self.assertEqual(rev(thin, "refs/heads/main"), "")
            self.untouched(thin)

        def test_a_mirror_that_carries_work_is_refused(self):
            fork = make_fork(self.tmp)
            sh("git", "switch", "main", cwd=fork)
            commit_fork(fork, "ours/local.txt", "ours\n", "ours: on the mirror")
            sh("git", "switch", "develop", cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("Adopting forkflow", err)
            self.assertIn("never resets", err)
            self.untouched(fork)

        def test_a_local_only_trunk_is_refused(self):
            fork = make_fresh_fork(self.tmp)
            sh("git", "branch", "--no-track", "develop", "main", cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("push it yourself", err)
            self.assertEqual(origin_sha(fork, "develop"), "")
            self.untouched(fork)

        def test_a_trunk_hidden_by_a_narrow_refspec_is_not_bootstrapped(self):
            """`origin/<trunk>` being invisible here is not proof the trunk does not exist:
            bootstrapping would push a trunk that is already published."""
            fork = make_fork(self.tmp)
            trunk_sha = origin_sha(fork, "develop")
            sh("git", "switch", "main", cwd=fork)
            sh("git", "branch", "-D", "develop", cwd=fork)
            sh("git", "config", "remote.origin.fetch",
               "+refs/heads/main:refs/remotes/origin/main", cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/origin/develop", cwd=fork)

            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertIn("does not fetch it", out)
            self.assertIn("git config --add remote.origin.fetch", out)
            self.assertEqual(origin_sha(fork, "develop"), trunk_sha)   # untouched on origin
            self.assertEqual(rev(fork, "refs/heads/develop"), "")      # and not created here

        def test_a_failing_fetch_changes_nothing(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "set-url", "upstream",
               os.path.join(self.tmp, "nowhere.git"), cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("fetch failed", err)
            self.untouched(fork)

        def test_no_upstream_remote_and_no_url_is_exit_2_with_the_hint(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "remove", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("--upstream-url", err)
            self.untouched(fork)

        def test_dry_run_changes_nothing(self):
            fork = make_fork(self.tmp)
            # a wrong origin/HEAD that only `set-head -a` would correct (a fetch only ever
            # fills in a missing one), so its survival proves set-head was not run
            sh("git", "symbolic-ref", "refs/remotes/origin/HEAD",
               "refs/remotes/origin/main", cwd=fork)
            branches = local_branches(fork)
            code, out, err = run("-C", fork, "setup", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would:", out)
            self.assertEqual(local_branches(fork), branches)
            self.assertEqual(sh("git", "symbolic-ref", "refs/remotes/origin/HEAD", cwd=fork),
                             "refs/remotes/origin/main")
            self.assertEqual(self.cfg(fork, "branch.develop.mergeOptions"), "")
            self.untouched(fork)

        def test_dry_run_on_a_fresh_fork_previews_the_bootstrap(self):
            fork = make_fresh_fork(self.tmp)
            code, out, err = run("-C", fork, "setup", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would create develop", out)
            self.assertEqual(origin_sha(fork, "develop"), "")
            self.assertNotIn("refs/heads/develop", local_branches(fork))

        def test_dry_run_stops_before_a_remote_it_would_have_to_add(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "remove", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "setup", "--dry-run",
                                 "--upstream-url", os.path.join(self.tmp, "upstream.git"))
            self.assertEqual(code, 0, err + out)
            self.assertIn("would add the remote", out)
            self.assertEqual(sh("git", "remote", cwd=fork).split(), ["origin"])
            self.untouched(fork)

    # ------------------------------------------------------------------- #
    # setup: the pre-push hook, exercised as a hook by real `git push` runs
    # ------------------------------------------------------------------- #

    class TestHookUrlNormaliser(Base):
        """`ff_repo_id` (sh, in the hook) and `repo_id` (here) must read a URL the same way.

        The hook is the only layer that sees a `git push <url>` typed by hand, and rule 1 is
        about the repository: if the two normalisers drift, one spelling is refused by the
        script and waved through by the hook, or the other way round."""

        SPELLINGS = ["https://github.com/o/r.git", "https://GitHub.com/O/R",
                     "https://git@github.com/o/r/", "git@github.com:o/r.git",
                     "ssh://git@github.com:22/o/r", "ssh://github.com/o/r",
                     "git://github.com:9418/o/r", "http://github.com:80/o/r",
                     "https://github.com:443/o/r", "https://github.com:8080/o/r",
                     "HTTPS://GitHub.com:443/O/R",
                     "/tmp/Case/upstream.git", "/tmp/Case/upstream.git/",
                     "/tmp/Case/upstream.git/.", "file:///tmp/Case/upstream.git",
                     "file://localhost/tmp/Case/upstream.git",
                     "file://LOCALHOST/tmp/Case/upstream.git",
                     "file://LocalHost/tmp/Case/upstream.git",
                     "file://localhost./tmp/Case/upstream.git", "/srv/a b/repo.git",
                     "host:o/r", "host:/abs/path.git", "../Other.git", "./../Other.git",
                     "DISABLED", ""]

        def norm(self, url: str, cwd: Optional[str] = None) -> str:
            path = os.path.join(self.tmp, "ff-repo-id.sh")
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n" + HOOK_REPO_ID + '\nff_repo_id "$1"\n')
            return sh("sh", path, url, cwd=cwd)

        def test_the_hooks_url_normaliser_matches_repo_id(self):
            for url in self.SPELLINGS:
                self.assertEqual(self.norm(url), repo_id(url), url)

        def test_every_spelling_of_a_local_upstream_is_one_repository(self):
            """A fork whose upstream is a local path or a `file:` URL is an ordinary case -
            every fixture this suite builds is one - and `..`, a `/.` suffix, a symlink and
            `file://localhost` all reach the same repository. Agreeing with `repo_id` proves
            nothing on its own if both are wrong, so this asserts what they must *say*.

            `LOCALHOST` and `localhost.` are that host too (RFC 3986 §3.2.2), and git takes
            both: while either was compared as written it read as a host and the push went
            through - see `test_every_spelling_of_the_upstream_url_is_refused`."""
            lab = os.path.join(self.tmp, "lab")
            here = os.path.join(lab, "sub")
            real = os.path.join(lab, "upstream.git")
            os.makedirs(here)
            os.makedirs(real)
            link = os.path.join(lab, "up-link.git")
            os.symlink(real, link)
            spellings = [real, real + "/", real + "/.", real + "/./", link,
                         os.path.join(here, "..", "upstream.git"),
                         "file://" + real, "file://localhost" + real,
                         "file://LOCALHOST" + real, "file://LocalHost" + real,
                         "file://localhost." + real,
                         "../upstream.git", "./../upstream.git"]
            for url in spellings:
                self.assertEqual(self.norm(url, cwd=here), repo_id(url, here), url)
            self.assertEqual({repo_id(u, here) for u in spellings}, {real[:-len(".git")]})
            # and a neighbour of it is still a different repository
            other = os.path.join(lab, "other.git")
            os.makedirs(other)
            self.assertNotEqual(repo_id("../other.git", here), repo_id("../upstream.git", here))

        def test_it_folds_the_spellings_of_one_repository_together(self):
            same = ["https://github.com/o/r.git", "https://GitHub.com/O/R",
                    "git@github.com:o/r.git", "ssh://git@github.com:22/o/r",
                    "https://github.com:443/o/r"]
            self.assertEqual({self.norm(u) for u in same}, {"github.com/o/r"})
            self.assertNotEqual(self.norm("https://github.com/o/r"),
                                self.norm("https://github.com/o/fork"))

    class HookBase(SetupBase):
        def hook_path(self, repo: str) -> str:
            return os.path.join(repo, ".git", "hooks", "pre-push")

        def read_hook(self, repo: str) -> str:
            with open(self.hook_path(repo)) as fh:
                return fh.read()

        def setup_ok(self, repo: str, *extra: str) -> str:
            code, out, err = run("-C", repo, "setup", *extra)
            self.assertEqual(code, 0, err + out)
            return out

        def push(self, repo: str, *args: str):
            """A real `git push` from the fork - the only way to test a hook as a hook."""
            rc, out, err = git_rc("push", *args, cwd=repo)
            return rc, out + err

        def upstream_url(self, repo: str) -> str:
            return sh("git", "remote", "get-url", "upstream", cwd=repo)

        def advance_mirror_to_upstream(self, repo: str) -> str:
            """Upstream moves, the fork fetches and fast-forwards `main`: now a push of the
            mirror carries a real ref line (an up-to-date push feeds the hook nothing)."""
            commit_upstream(self.tmp, "src/app.py", "def main():\n    return 3\n")
            sh("git", "fetch", "upstream", cwd=repo)
            sh("git", "switch", "main", cwd=repo)
            sh("git", "merge", "--ff-only", "upstream/main", cwd=repo)
            return rev(repo, "refs/heads/main")

    class TestSetupHookInstall(HookBase):
        def test_the_hook_is_installed_marked_and_executable(self):
            fork = make_fork(self.tmp)
            out = self.setup_ok(fork)
            text = self.read_hook(fork)
            self.assertIn(HOOK_MARK, text)
            self.assertTrue(os.access(self.hook_path(fork), os.X_OK))
            self.assertIn("refs/remotes/upstream/main", text)
            self.assertIn("refs/heads/develop", text)
            self.assertIn("refs/heads/main", text)
            self.assertIn(self.upstream_url(fork), text)
            self.assertIn("fetch before pushing the mirror", text)
            self.assertIn("hook", out)
            self.assertEqual(hook_state(ctx_for(fork)), "installed")

        def test_the_names_of_a_renamed_layout_are_baked_in(self):
            fork = make_fork(self.tmp, trunk="trunk", mirror="vendor")
            self.setup_ok(fork, "--trunk", "trunk", "--mirror", "vendor")
            text = self.read_hook(fork)
            self.assertIn("trunk_ref='refs/heads/trunk'", text)
            self.assertIn("mirror_ref='refs/heads/vendor'", text)

        def test_a_second_setup_does_not_duplicate_the_hook(self):
            fork = make_fork(self.tmp)
            self.setup_ok(fork)
            first = self.read_hook(fork)
            self.setup_ok(fork)
            self.assertEqual(self.read_hook(fork), first)
            self.assertEqual(first.count(HOOK_MARK), 1)
            self.assertFalse(os.path.exists(self.hook_path(fork) + ".pre-forkflow"))

        def test_a_foreign_hook_is_refused_and_kept_by_force(self):
            fork = make_fork(self.tmp)
            os.makedirs(os.path.dirname(self.hook_path(fork)), exist_ok=True)
            with open(self.hook_path(fork), "w") as fh:
                fh.write("#!/bin/sh\n# someone else's hook\nexit 0\n")
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("--force", err)
            self.assertIn("someone else's hook", self.read_hook(fork))
            self.assertEqual(self.cfg(fork, "pull.ff"), "")        # stopped before the config step

            self.setup_ok(fork, "--force")
            self.assertIn(HOOK_MARK, self.read_hook(fork))
            with open(self.hook_path(fork) + ".pre-forkflow") as fh:
                self.assertIn("someone else's hook", fh.read())

        def test_core_hookspath_is_refused_with_a_warning_and_forced(self):
            fork = make_fork(self.tmp)
            shared = os.path.join(self.tmp, "shared-hooks")
            os.makedirs(shared)
            sh("git", "config", "core.hooksPath", shared, cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 2, out)
            self.assertIn("WARNING", out)
            self.assertIn("core.hooksPath", out)
            self.assertFalse(os.path.exists(os.path.join(shared, "pre-push")))

            out = self.setup_ok(fork, "--force")
            self.assertIn("WARNING", out)
            with open(os.path.join(shared, "pre-push")) as fh:
                self.assertIn(HOOK_MARK, fh.read())

        def test_dry_run_writes_no_hook(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "setup", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would install it", out)
            self.assertFalse(os.path.exists(self.hook_path(fork)))

    class TestSetupHookRefuses(HookBase):
        def setUp(self):
            super().setUp()
            self.fork = make_fork(self.tmp)
            self.setup_ok(self.fork)

        def test_pushing_to_upstream_by_name_fails_before_the_hook(self):
            rc, out = self.push(self.fork, "upstream", "main")
            self.assertEqual(rc, 128, out)
            self.assertIn("DISABLED", out)
            self.assertNotIn("forkflow:", out)          # the push URL stopped it, not the hook

        def test_pushing_to_the_upstream_url_is_refused_by_the_hook(self):
            rc, out = self.push(self.fork, self.upstream_url(self.fork), "main")
            self.assertEqual(rc, 1, out)
            self.assertIn("never push to upstream", out)

        def test_upstream_under_another_remote_name_is_refused_by_url(self):
            sh("git", "remote", "add", "mirror-src", self.upstream_url(self.fork), cwd=self.fork)
            rc, out = self.push(self.fork, "mirror-src", "main")
            self.assertEqual(rc, 1, out)
            self.assertIn("never push to upstream", out)

        def test_every_spelling_of_the_upstream_url_is_refused(self):
            """Rule 1 is about the repository. A byte-for-byte compare refuses one spelling
            and lets `<url>/` and `file://<url>` through - both really did create refs on the
            original project before the hook normalised what it compares.

            A relative path, a `/.` suffix, a symlink and `file://localhost` reach it just as
            well: each of these really did land a ref in `upstream.git` until the hook
            canonicalised the two sides before comparing them.

            `file://LOCALHOST/...` and `file://localhost./...` are the same authority (a host
            is case-insensitive and may carry the root-label dot) and git takes both: while
            the hook matched `localhost` as it was written, each of these landed
            `refs/heads/probe` in `upstream.git`."""
            url = self.upstream_url(self.fork)
            link = os.path.join(self.tmp, "up-link.git")
            os.symlink(url, link)
            sh("git", "switch", "-c", "feat/x", cwd=self.fork)
            commit_fork(self.fork, "ours/feature.txt", "ours\n", "ours: a feature")
            spellings = [url + "/", url + "//", "file://" + url, "file://" + url + "/",
                         url + "/.", url + "/./", link, "file://localhost" + url,
                         "file://LOCALHOST" + url, "file://LocalHost" + url,
                         "file://localhost." + url,
                         "../upstream.git", "./../upstream.git"]
            for spelling in spellings:
                rc, out = self.push(self.fork, spelling, "feat/x:refs/heads/probe")
                self.assertEqual(rc, 1, "%s: %s" % (spelling, out))
                self.assertIn("never push to upstream", out)
            up = os.path.join(self.tmp, "upstream.git")
            self.assertEqual(sh("git", "--git-dir=" + up, "for-each-ref",
                                "--format=%(refname)", "refs/heads/probe"), "")

        def test_the_trunk_cannot_be_pushed(self):
            before = origin_sha(self.fork, "develop")
            commit_fork(self.fork, "ours/feature.txt", "ours\n", "ours: on the trunk")
            rc, out = self.push(self.fork, "origin", "develop")
            self.assertEqual(rc, 1, out)
            self.assertIn("never push the trunk", out)
            self.assertEqual(origin_sha(self.fork, "develop"), before)

        def test_the_trunk_cannot_be_deleted(self):
            before = origin_sha(self.fork, "develop")
            rc, out = self.push(self.fork, "origin", ":develop")
            self.assertEqual(rc, 1, out)
            self.assertIn("never push the trunk", out)
            self.assertEqual(origin_sha(self.fork, "develop"), before)

        def test_a_commit_on_the_mirror_cannot_be_pushed(self):
            before = origin_sha(self.fork, "main")
            sh("git", "switch", "main", cwd=self.fork)
            commit_fork(self.fork, "ours/local.txt", "ours\n", "ours: on the mirror")
            rc, out = self.push(self.fork, "origin", "main")
            self.assertEqual(rc, 1, out)
            self.assertIn("pure copy of upstream", out)
            self.assertEqual(origin_sha(self.fork, "main"), before)

        def test_the_mirror_cannot_be_deleted(self):
            before = origin_sha(self.fork, "main")
            rc, out = self.push(self.fork, "origin", ":main")
            self.assertEqual(rc, 1, out)
            self.assertIn("refusing to delete the mirror", out)
            self.assertEqual(origin_sha(self.fork, "main"), before)

        def test_the_mirror_cannot_be_rewound(self):
            """Rule 6 both ways: an older commit is still an ancestor of upstream, so only
            "the remote's tip must be in what I push" catches a forced rewind."""
            self.advance_mirror_to_upstream(self.fork)
            rc, out = self.push(self.fork, "origin", "main")
            self.assertEqual(rc, 0, out)                          # forward is fine
            forward = origin_sha(self.fork, "main")
            rc, out = self.push(self.fork, "--force", "origin", "main~1:refs/heads/main")
            self.assertEqual(rc, 1, out)
            self.assertIn("only moves forward (rule 6)", out)
            self.assertEqual(origin_sha(self.fork, "main"), forward)

        def test_the_mirror_cannot_be_rewound_onto_a_tip_this_clone_lacks(self):
            """The state a teammate's `sync` leaves behind: `origin/<mirror>` is at an
            upstream commit this clone has never fetched, so `merge-base --is-ancestor`
            cannot answer. An unanswerable question is not a yes - the older local mirror is
            still a pure copy of upstream, so nothing else in the hook catches the rewind."""
            self.advance_mirror_to_upstream(self.fork)
            rc, out = self.push(self.fork, "origin", "main")
            self.assertEqual(rc, 0, out)
            forward = origin_sha(self.fork, "main")

            # upstream moves again and reaches origin/main through another clone; this one
            # never fetches it, so origin's tip is not an object here
            ahead = commit_upstream(self.tmp, "src/app.py", "def main():\n    return 5\n")
            push_upstream_into_origin(self.tmp)
            self.assertEqual(origin_sha(self.fork, "main"), ahead)
            self.assertEqual(sh("git", "cat-file", "-t", ahead, cwd=self.fork, check=False), "")

            rc, out = self.push(self.fork, "--force", "origin", "main")
            self.assertEqual(rc, 1, out)
            self.assertIn("cannot verify that the mirror only moves forward", out)
            self.assertIn("git fetch origin", out)
            self.assertEqual(origin_sha(self.fork, "main"), ahead)
            self.assertNotEqual(ahead, forward)

        def test_the_mirror_is_refused_when_upstream_is_not_fetched(self):
            self.advance_mirror_to_upstream(self.fork)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=self.fork)
            before = origin_sha(self.fork, "main")
            rc, out = self.push(self.fork, "origin", "main")
            self.assertEqual(rc, 1, out)
            self.assertIn("cannot verify the mirror", out)
            self.assertIn("git fetch upstream", out)
            self.assertEqual(origin_sha(self.fork, "main"), before)

    class TestSetupHookAllows(HookBase):
        def setUp(self):
            super().setUp()
            self.fork = make_fork(self.tmp)
            self.setup_ok(self.fork)

        def test_a_mirror_that_is_a_copy_of_upstream_is_pushed(self):
            target = self.advance_mirror_to_upstream(self.fork)
            self.assertNotEqual(origin_sha(self.fork, "main"), target)   # a real ref line
            rc, out = self.push(self.fork, "origin", "main")
            self.assertEqual(rc, 0, out)
            self.assertEqual(origin_sha(self.fork, "main"), target)

        def test_feature_branches_are_pushed_and_can_be_deleted(self):
            sh("git", "switch", "-c", "feat/x", cwd=self.fork)
            commit_fork(self.fork, "ours/feature.txt", "ours\n", "ours: a feature")
            rc, out = self.push(self.fork, "origin", "feat/x")
            self.assertEqual(rc, 0, out)
            self.assertNotEqual(origin_sha(self.fork, "feat/x"), "")

            rc, out = self.push(self.fork, "origin", "HEAD:refs/heads/sync/stale")
            self.assertEqual(rc, 0, out)
            rc, out = self.push(self.fork, "origin", ":sync/stale")
            self.assertEqual(rc, 0, out)
            self.assertEqual(origin_sha(self.fork, "sync/stale"), "")

        def test_sync_still_works_with_the_hook_installed(self):
            commit_upstream(self.tmp, "src/app.py", "def main():\n    return 4\n")
            code, out, err = run("-C", self.fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(self.fork, "main"), rev(self.fork, "refs/remotes/upstream/main"))
            self.assertNotEqual(origin_sha(self.fork, sync_branch_name()), "")

    # ------------------------------------------------------------------- #
    # setup: the platform report - fake `glab`/`gh`, no network, no real tool
    # ------------------------------------------------------------------- #

    # the fork's own origin URL - a subgroup on GitLab, so every path in these tests carries
    # one - and the project path the report has to build out of it. Neither tool's own
    # placeholder appears: both expand it to the original project (see `forge_path`)
    GL_ORIGIN = "git@gitlab.example.com:acme/team/widget.git"
    GL_PROJECT = "projects/acme%2Fteam%2Fwidget"
    GH_ORIGIN = "https://github.com/acme/widget.git"
    GH_PROJECT = "repos/acme/widget"

    # (stdout or stderr, exit code) of one faked API call
    GL_OK = ('{"default_branch":"develop","merge_method":"ff"}', 0)
    GH_OK = ('{"default_branch":"develop","allow_merge_commit":true,'
             '"allow_rebase_merge":true}', 0)
    UNPROTECTED = ("404 Not Found", 1)
    FORBIDDEN = ("403 Forbidden", 1)
    GL_PROTECTED = ('{"name":"b","allow_force_push":false,'
                    '"push_access_levels":[{"access_level":0}]}', 0)
    GL_FORCE = ('{"name":"b","allow_force_push":true,'
                '"push_access_levels":[{"access_level":0}]}', 0)
    # protected, force-push off - and a Maintainer may still push straight to it
    GL_PUSHABLE = ('{"name":"b","allow_force_push":false,'
                   '"push_access_levels":[{"access_level":40}]}', 0)
    GH_PROTECTED = ('{"required_pull_request_reviews":{"required_approving_review_count":1},'
                    '"enforce_admins":{"enabled":true},'
                    '"allow_force_pushes":{"enabled":false}}', 0)
    GH_FORCE = ('{"required_pull_request_reviews":{"required_approving_review_count":1},'
                '"enforce_admins":{"enabled":true},'
                '"allow_force_pushes":{"enabled":true}}', 0)
    # protected, force-push off - and no merge request required, so a direct push still lands
    GH_NO_PR = ('{"allow_force_pushes":{"enabled":false}}', 0)
    # a required pull request that every administrator is exempt from
    GH_ADMIN_BYPASS = ('{"required_pull_request_reviews":{"required_approving_review_count":1},'
                       '"enforce_admins":{"enabled":false},'
                       '"allow_force_pushes":{"enabled":false}}', 0)
    # a required pull request with a named allowance that pushes straight past it
    GH_PR_BYPASS = ('{"required_pull_request_reviews":{"required_approving_review_count":1,'
                    '"bypass_pull_request_allowances":{"users":[{"login":"ada"}],'
                    '"teams":[],"apps":[]}},'
                    '"enforce_admins":{"enabled":true},'
                    '"allow_force_pushes":{"enabled":false}}', 0)
    # a required check bound to the app that may report it (`checks`, not flat `contexts`)
    GH_APP_CHECK = ('{"required_status_checks":{"strict":true,"contexts":["ci"],'
                    '"checks":[{"context":"ci","app_id":15368}]},'
                    '"enforce_admins":{"enabled":true},'
                    '"required_pull_request_reviews":{"required_approving_review_count":1},'
                    '"allow_force_pushes":{"enabled":true}}', 0)
    # a protected branch that already carries the settings a full PUT would wipe
    GH_FORCE_KEPT = ('{"required_status_checks":{"strict":true,"contexts":["ci"]},'
                     '"enforce_admins":{"enabled":true},'
                     '"required_pull_request_reviews":{"required_approving_review_count":2,'
                     '"dismiss_stale_reviews":true,"require_code_owner_reviews":false},'
                     '"restrictions":{"users":[{"login":"ada"}],"teams":[{"slug":"core"}],'
                     '"apps":[]},'
                     '"allow_force_pushes":{"enabled":true}}', 0)

    class TestApiStatus(unittest.TestCase):
        def test_both_tools_wording_is_understood(self):
            self.assertEqual(api_status("gh: Not Found (HTTP 404)"), 404)
            self.assertEqual(api_status("HTTP/1.1 403 Forbidden"), 403)
            self.assertEqual(api_status("404 Not Found"), 404)          # glab
            self.assertEqual(api_status("500 Internal Server Error"), 500)
            self.assertIsNone(api_status("could not resolve host: gitlab.example.com"))

        def test_a_number_that_is_not_a_status_is_not_read_as_one(self):
            self.assertIsNone(api_status("dial tcp gitlab.example.com:443: timeout"))
            self.assertIsNone(api_status("error at line 404 of the config"))
            self.assertIsNone(api_status("read 500 bytes"))

    class PlatformBase(Base):
        def api_tool(self, name: str, routes: Sequence[Tuple[str, str, int]]) -> None:
            """A fake glab/gh answering per API path; every argument of every call is
            recorded, so `the report never writes` can be proved."""
            self.log = os.path.join(self.tmp, name + "-argv.txt")
            lines = ['for a in "$@"; do echo "$a"; done >> %s' % shlex.quote(self.log),
                     'case "$2" in']
            for pattern, text, rc in routes:
                answer = ("echo %s" % shlex.quote(text) if rc == 0
                          else "echo %s >&2; exit %d" % (shlex.quote(text), rc))
                lines.append("%s) %s;;" % (pattern, answer))
            lines += ['*) echo "unexpected path: $2" >&2; exit 9;;', "esac"]
            fake_tool(os.path.join(self.tmp, "bin"), name, "\n".join(lines) + "\n")

        def gitlab_tool(self, project=GL_OK, trunk=GL_PROTECTED, mirror=GL_PROTECTED) -> None:
            self.api_tool("glab", [
                ("*protected_branches/develop", trunk[0], trunk[1]),
                ("*protected_branches/main", mirror[0], mirror[1]),
                ("projects/*", project[0], project[1]),
            ])

        def github_tool(self, repo=GH_OK, trunk=GH_PROTECTED, mirror=GH_PROTECTED) -> None:
            self.api_tool("gh", [
                ("*branches/develop/protection", trunk[0], trunk[1]),
                ("*branches/main/protection", mirror[0], mirror[1]),
                ("repos/*", repo[0], repo[1]),
            ])

        def argv(self) -> list:
            with open(self.log) as fh:
                return [ln.rstrip("\n") for ln in fh]

        def report(self, platform: str, origin: Optional[str] = None) -> str:
            """The report for a fork whose origin really is a hosted one: the paths it prints
            are built from that URL, so a Ctx without one proves nothing."""
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = platform
            if origin is not None:
                ctx.origin_url = origin
            elif platform in ("gitlab", "github"):
                ctx.origin_url = GL_ORIGIN if platform == "gitlab" else GH_ORIGIN
            _, out, err = capture(platform_report, ctx)
            self.assertEqual(err, "")
            return out

        def fixes(self, out: str) -> list:
            return [ln.strip()[len("fix: "):] for ln in out.splitlines()
                    if ln.strip().startswith("fix: ")]

    class TestPlatformReportGitlab(PlatformBase):
        def test_everything_matching_is_ok_and_suggests_nothing(self):
            self.gitlab_tool()
            out = self.report("gitlab")
            self.assertIn("default branch: `develop` is the trunk: ok", out)
            self.assertIn("merge method: `ff`: ok", out)
            self.assertIn("trunk `develop`: protected, force-push disallowed, "
                          "direct push blocked: ok", out)
            self.assertIn("mirror `main`: protected, force-push disallowed: ok", out)
            self.assertEqual(self.fixes(out), [])

        def test_a_trunk_anyone_may_push_to_is_a_finding_with_a_reprotect(self):
            """Rule 2 is not "protected", it is "only a merge request reaches the trunk":
            a protected branch whose push level is Maintainer takes a direct push."""
            self.gitlab_tool(trunk=GL_PUSHABLE, mirror=GL_PUSHABLE)
            out = self.report("gitlab")
            self.assertIn("trunk `develop`: protected, but a direct push is still allowed", out)
            # the mirror is pushed by `sync` itself: it must stay directly pushable
            self.assertIn("mirror `main`: protected, force-push disallowed: ok", out)
            self.assertEqual(self.fixes(out), [
                "glab api --method DELETE projects/acme%2Fteam%2Fwidget/protected_branches/develop && "
                "glab api --method POST projects/acme%2Fteam%2Fwidget/protected_branches "
                "-f name='develop' -F push_access_level=0 -F merge_access_level=40 "
                "-F allow_force_push=false"])

        def test_a_push_level_that_cannot_be_read_is_reported_as_unchecked(self):
            self.gitlab_tool(trunk=('{"name":"b","allow_force_push":false}', 0))
            out = self.report("gitlab")
            self.assertIn("trunk `develop`: protected, force-push disallowed, "
                          "direct push not checked: ok", out)
            self.assertEqual(self.fixes(out), [])

        def test_a_branch_name_that_is_shell_syntax_is_quoted_in_the_fix(self):
            """The names come from `.forkflow.toml`, which a sync can bring in from upstream,
            and the fix is a command the user is told to paste into a shell."""
            self.gitlab_tool(project=('{"default_branch":"main","merge_method":"ff"}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            ctx = ctx_for(make_fork(self.tmp))
            ctx.origin_url = GL_ORIGIN
            ctx.platform, ctx.trunk = "gitlab", "dev;touch /tmp/pwned"
            _, out, _ = capture(platform_report, ctx)
            self.assertTrue(self.fixes(out))
            for line in self.fixes(out):
                self.assertIn("'dev;touch /tmp/pwned'", line)
                self.assertNotIn("=dev;touch", line)

        def test_the_report_only_ever_reads(self):
            self.gitlab_tool()
            self.report("gitlab")
            self.assertEqual(self.argv(), [
                "api", "projects/acme%2Fteam%2Fwidget",
                "api", "projects/acme%2Fteam%2Fwidget/protected_branches/develop",
                "api", "projects/acme%2Fteam%2Fwidget/protected_branches/main"])

        def test_a_wrong_default_branch_and_merge_method_share_one_fix(self):
            self.gitlab_tool(project=('{"default_branch":"main","merge_method":"merge"}', 0))
            out = self.report("gitlab")
            self.assertIn("default branch: `main` - it must be the trunk `develop`", out)
            self.assertIn("merge method: `merge` -", out)
            self.assertEqual(self.fixes(out), [
                "glab api --method PUT projects/acme%2Fteam%2Fwidget "
                "-f default_branch='develop' -f merge_method=ff"])

        def test_an_unprotected_trunk_gets_a_post_and_the_mirror_only_advice(self):
            self.gitlab_tool(trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("gitlab")
            self.assertIn("trunk `develop`: NOT protected", out)
            self.assertIn("mirror `main`: not protected (advisory", out)
            self.assertEqual(self.fixes(out), [
                "glab api --method POST projects/acme%2Fteam%2Fwidget/protected_branches "
                "-f name='develop' -F push_access_level=0 -F merge_access_level=40 "
                "-F allow_force_push=false"])

        def test_force_push_allowed_gets_a_patch_on_either_branch(self):
            self.gitlab_tool(trunk=GL_FORCE, mirror=GL_FORCE)
            out = self.report("gitlab")
            self.assertIn("trunk `develop`: protected, but force-push is ALLOWED", out)
            self.assertIn("mirror `main`: protected, but force-push is ALLOWED", out)
            self.assertEqual(self.fixes(out), [
                "glab api --method PATCH projects/acme%2Fteam%2Fwidget/protected_branches/develop "
                "-F allow_force_push=false",
                "glab api --method PATCH projects/acme%2Fteam%2Fwidget/protected_branches/main "
                "-F allow_force_push=false"])

        def test_output_that_is_not_json_is_not_checked(self):
            self.gitlab_tool(project=("<html>login page</html>", 0))
            out = self.report("gitlab")
            self.assertIn("not checked (glab did not answer with JSON)", out)
            self.assertEqual(self.fixes(out), [])

        def test_json_that_is_not_an_object_is_not_checked(self):
            self.gitlab_tool(project=("[1, 2]", 0))
            out = self.report("gitlab")
            self.assertIn("not checked (glab did not answer with an object)", out)

        def test_403_on_protection_is_not_checked_and_suggests_nothing(self):
            self.gitlab_tool(trunk=FORBIDDEN, mirror=FORBIDDEN)
            out = self.report("gitlab")
            self.assertIn("trunk `develop`: not checked (insufficient rights)", out)
            self.assertIn("mirror `main`: not checked (insufficient rights)", out)
            self.assertEqual(self.fixes(out), [])

        def test_a_failing_project_call_stops_the_report(self):
            self.gitlab_tool(project=("500 Internal Server Error", 1))
            out = self.report("gitlab")
            self.assertIn("not checked (HTTP 500)", out)
            self.assertNotIn("trunk `develop`", out)
            self.assertEqual(self.argv(), ["api", "projects/acme%2Fteam%2Fwidget"])

        def test_a_missing_tool_is_not_checked(self):
            ctx = ctx_for(make_fork(self.tmp))
            ctx.origin_url, ctx.platform = GL_ORIGIN, "gitlab"
            missing = FileNotFoundError(2, "No such file or directory")
            with mock.patch.object(subprocess, "run", side_effect=missing):
                _, out, _ = capture(platform_report, ctx)
            self.assertIn("not checked (glab unavailable: No such file or directory)", out)
            self.assertNotIn("trunk `develop`", out)

    class TestPlatformReportGithub(PlatformBase):
        def test_everything_matching_is_ok_and_suggests_nothing(self):
            self.github_tool()
            out = self.report("github")
            self.assertIn("default branch: `develop` is the trunk: ok", out)
            self.assertIn("merge commits: allowed: ok", out)
            self.assertIn("rebase merges: allowed: ok", out)
            self.assertIn("trunk `develop`: protected, force-push disallowed, "
                          "direct push blocked: ok", out)
            self.assertEqual(self.fixes(out), [])
            self.assertEqual(self.argv(), [
                "api", "repos/acme/widget",
                "api", "repos/acme/widget/branches/develop/protection",
                "api", "repos/acme/widget/branches/main/protection"])

        def test_disabled_merge_options_are_fixed_together_with_the_default_branch(self):
            self.github_tool(repo=('{"default_branch":"main","allow_merge_commit":false,'
                                   '"allow_rebase_merge":false}', 0))
            out = self.report("github")
            self.assertIn("merge commits: NOT allowed", out)
            self.assertIn("rebase merges: NOT allowed", out)
            self.assertEqual(self.fixes(out), [
                "gh api -X PATCH repos/acme/widget -f default_branch='develop' "
                "-F allow_merge_commit=true -F allow_rebase_merge=true"])

        def test_an_unprotected_trunk_gets_the_put_and_the_mirror_only_advice(self):
            self.github_tool(trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("github")
            self.assertIn("trunk `develop`: NOT protected", out)
            self.assertIn("mirror `main`: not protected (advisory", out)
            body = github_protection_body({"enforce_admins": True}, require_pr=True)
            self.assertEqual(self.fixes(out), [
                "echo '%s' | gh api -X PUT "
                "repos/acme/widget/branches/develop/protection --input -" % body])
            # the body the fix pastes, pinned against the output rather than against the
            # function that built it: force-push off, admins included, and a required review
            self.assertIn('"allow_force_pushes":false', out)
            self.assertIn('"enforce_admins":true', out)
            # a protected branch with no required review still takes a direct push (rule 2)
            self.assertIn('"required_pull_request_reviews":'
                          '{"dismiss_stale_reviews":false,"require_code_owner_reviews":false,'
                          '"required_approving_review_count":0}', out)
            self.assertNotIn('"required_pull_request_reviews":null', out)

        def test_a_protected_trunk_without_a_required_pr_is_a_finding(self):
            """"Protected" with force-push off still lets everyone with write access push
            straight to the trunk; only a required pull request stops that."""
            self.github_tool(trunk=GH_NO_PR, mirror=GH_NO_PR)
            out = self.report("github")
            self.assertIn("trunk `develop`: protected, but a direct push is still allowed", out)
            # the mirror is pushed by `sync` itself: requiring a PR on it would break sync
            self.assertIn("mirror `main`: protected, force-push disallowed: ok", out)
            fixes = self.fixes(out)
            self.assertEqual(len(fixes), 1)
            self.assertIn('"required_pull_request_reviews":'
                          '{"dismiss_stale_reviews":false,"require_code_owner_reviews":false,'
                          '"required_approving_review_count":0}', fixes[0])
            self.assertIn('"allow_force_pushes":false', fixes[0])

        def test_admins_exempted_from_the_required_pr_are_a_finding(self):
            """`enforce_admins: false` exempts every administrator from the whole protection
            object, so a required pull request stops nobody who has that role."""
            self.github_tool(trunk=GH_ADMIN_BYPASS, mirror=GH_ADMIN_BYPASS)
            out = self.report("github")
            self.assertIn("trunk `develop`: protected, but a direct push is still allowed", out)
            self.assertIn("`enforce_admins` is false", out)
            self.assertIn("mirror `main`: protected, force-push disallowed: ok", out)
            fixes = self.fixes(out)
            self.assertEqual(len(fixes), 1)
            self.assertIn('"enforce_admins":true', fixes[0])

        def test_a_pr_bypass_allowance_is_a_finding_and_the_fix_drops_it(self):
            self.github_tool(trunk=GH_PR_BYPASS, mirror=GH_PR_BYPASS)
            out = self.report("github")
            self.assertIn("trunk `develop`: protected, but a direct push is still allowed", out)
            self.assertIn("lets ada through", out)
            fixes = self.fixes(out)
            self.assertEqual(len(fixes), 1)
            self.assertNotIn("bypass_pull_request_allowances", fixes[0])
            self.assertIn('"enforce_admins":true', fixes[0])

        def test_a_check_bound_to_an_app_is_carried_back_as_a_check(self):
            """`contexts` is the flat legacy list of the same names: carrying only that one
            back unbinds every required check from the app allowed to report it."""
            self.github_tool(trunk=GH_APP_CHECK, mirror=UNPROTECTED)
            out = self.report("github")
            fixes = self.fixes(out)
            self.assertEqual(len(fixes), 1)
            body = json.loads(fixes[0].split("echo '", 1)[1].split("' |", 1)[0])
            self.assertEqual(body["required_status_checks"],
                             {"strict": True, "checks": [{"context": "ci", "app_id": 15368}]})
            self.assertNotIn("contexts", body["required_status_checks"])

        def test_a_flat_context_list_is_still_carried_back(self):
            body = json.loads(github_protection_body(
                {"required_status_checks": {"strict": False, "contexts": ["ci", "lint"]}}))
            self.assertEqual(body["required_status_checks"],
                             {"strict": False, "contexts": ["ci", "lint"]})

        def test_the_put_carries_back_the_settings_a_full_replace_would_reset(self):
            """`PUT .../protection` replaces the whole object: every flag the GET returned has
            to come back, or the fix silently weakens the branch it claims to tighten."""
            body = json.loads(github_protection_body(json.loads(
                '{"allow_force_pushes":{"enabled":true},'
                '"required_linear_history":{"enabled":true},'
                '"required_conversation_resolution":{"enabled":true},'
                '"block_creations":{"enabled":true},"lock_branch":{"enabled":true},'
                '"allow_deletions":{"enabled":false},"allow_fork_syncing":{"enabled":true},'
                '"required_pull_request_reviews":{"required_approving_review_count":1,'
                '"require_last_push_approval":true,'
                '"dismissal_restrictions":{"users":[{"login":"ada"}],"teams":[],"apps":[]},'
                '"bypass_pull_request_allowances":{"users":[],"teams":[{"slug":"core"}],'
                '"apps":[]}}}')))
            for key in ("required_linear_history", "required_conversation_resolution",
                        "block_creations", "lock_branch", "allow_fork_syncing"):
                self.assertIs(body[key], True, key)
            self.assertIs(body["allow_deletions"], False)
            self.assertIs(body["allow_force_pushes"], False)
            prr = body["required_pull_request_reviews"]
            self.assertIs(prr["require_last_push_approval"], True)
            self.assertEqual(prr["dismissal_restrictions"]["users"], ["ada"])
            self.assertEqual(prr["bypass_pull_request_allowances"]["teams"], ["core"])

        def test_enabled_force_pushes_are_flagged(self):
            self.github_tool(trunk=GH_FORCE, mirror=UNPROTECTED)
            out = self.report("github")
            self.assertIn("trunk `develop`: protected, but force-push is ALLOWED", out)
            self.assertEqual(len(self.fixes(out)), 1)

        def test_the_force_push_fix_keeps_the_protection_the_branch_already_has(self):
            """GitHub's PUT is a full replace: the plugin's own fix must not drop a review
            requirement or a required status check in exchange for disabling force-push."""
            self.github_tool(trunk=GH_FORCE_KEPT, mirror=UNPROTECTED)
            out = self.report("github")
            self.assertIn("replaces the whole protection object", out)
            fix = self.fixes(out)[0]
            self.assertIn('"required_approving_review_count":2', fix)
            self.assertIn('"contexts":["ci"]', fix)
            self.assertIn('"strict":true', fix)
            self.assertIn('"users":["ada"]', fix)
            self.assertIn('"teams":["core"]', fix)
            self.assertIn('"allow_force_pushes":false', fix)
            self.assertNotIn('"required_pull_request_reviews":null', fix)
            self.assertNotIn('"required_status_checks":null', fix)

        def test_403_on_protection_is_not_checked(self):
            self.github_tool(trunk=FORBIDDEN, mirror=FORBIDDEN)
            out = self.report("github")
            self.assertIn("trunk `develop`: not checked (insufficient rights)", out)
            self.assertEqual(self.fixes(out), [])

    class TestPlatformPathsNameTheFork(PlatformBase):
        """The one thing a fake `gh`/`glab` cannot show: what a placeholder means to the real
        tool. `{owner}/{repo}` and `:fullpath` are the *base* repository, and in a fork with
        an `upstream` remote - the remote `setup` itself adds - that is the original project.
        A fix command carrying one reads upstream's settings and, pasted by somebody who is a
        Maintainer there too, writes them: rule 1, undone by this tool's own advice.

        So every assertion here is about the fork's own `owner/repo`, and about neither
        placeholder surviving anywhere in the output."""

        def printed(self, out: str) -> list:
            """Every command the report offers: the `$ ...` it ran, and each `fix:`."""
            ran = [ln.split("  $ ", 1)[1].split("  -> ")[0]
                   for ln in out.splitlines() if "  $ " in ln]
            return ran + self.fixes(out)

        def assert_names_the_fork(self, out: str, project: str) -> None:
            cmds = self.printed(out)
            self.assertTrue(cmds, "no command printed at all")
            self.assertTrue(self.fixes(out), "no fix command printed")
            for cmd in cmds:
                self.assertIn(project, cmd)
                for placeholder in ("{owner}", "{repo}", ":fullpath"):
                    self.assertNotIn(placeholder, cmd)

        def test_github_ssh_origin_reads_and_fixes_the_forks_own_repo(self):
            self.github_tool(repo=('{"default_branch":"main","allow_merge_commit":false,'
                                   '"allow_rebase_merge":true}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("github", origin="git@github.com:acme/widget.git")
            self.assert_names_the_fork(out, "repos/acme/widget")
            self.assertIn("gh api -X PATCH repos/acme/widget -f default_branch=", out)
            self.assertIn("gh api -X PUT repos/acme/widget/branches/develop/protection", out)
            # and the reads the findings came from went to the fork as well
            self.assertEqual(self.argv(), [
                "api", "repos/acme/widget",
                "api", "repos/acme/widget/branches/develop/protection",
                "api", "repos/acme/widget/branches/main/protection"])

        def test_github_https_origin_names_the_same_repo(self):
            self.github_tool(repo=('{"default_branch":"main","allow_merge_commit":true,'
                                   '"allow_rebase_merge":true}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("github", origin="https://github.com/acme/widget.git")
            self.assert_names_the_fork(out, "repos/acme/widget")

        def test_the_repository_is_addressed_as_the_origin_spells_it(self):
            """A GET of a differently-cased repository is answered with a redirect, which the
            PATCH and PUT below do not follow."""
            self.github_tool(repo=('{"default_branch":"main","allow_merge_commit":true,'
                                   '"allow_rebase_merge":true}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("github", origin="git@github.com:Acme/Widget.git")
            self.assert_names_the_fork(out, "repos/Acme/Widget")

        def test_gitlab_addresses_the_fork_with_every_subgroup_kept(self):
            """`projects/:id` takes one url-encoded full path: every segment of it belongs,
            and a `/` inside it has to be encoded or it addresses another endpoint."""
            self.gitlab_tool(project=('{"default_branch":"main","merge_method":"merge"}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("gitlab", origin="git@gitlab.example.com:acme/team/widget.git")
            self.assert_names_the_fork(out, "projects/acme%2Fteam%2Fwidget")
            self.assertIn("glab api --method PUT projects/acme%2Fteam%2Fwidget ", out)
            self.assertEqual(self.argv(), [
                "api", "projects/acme%2Fteam%2Fwidget",
                "api", "projects/acme%2Fteam%2Fwidget/protected_branches/develop",
                "api", "projects/acme%2Fteam%2Fwidget/protected_branches/main"])

        def test_gitlab_https_origin_names_the_same_project(self):
            self.gitlab_tool(project=('{"default_branch":"main","merge_method":"merge"}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("gitlab",
                              origin="https://gitlab.example.com/acme/team/widget.git")
            self.assert_names_the_fork(out, "projects/acme%2Fteam%2Fwidget")

        def test_it_is_the_origin_url_that_is_read_and_never_the_upstream_one(self):
            """The fork, not the project it was forked from - which is what both tools would
            have answered with, and the whole point of building the path here."""
            self.github_tool(repo=('{"default_branch":"main","allow_merge_commit":true,'
                                   '"allow_rebase_merge":true}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = "github"
            ctx.origin_url = "git@github.com:acme/widget.git"
            ctx.upstream_url = "https://github.com/original/widget.git"
            _, out, err = capture(platform_report, ctx)
            self.assertEqual(err, "")
            self.assert_names_the_fork(out, "repos/acme/widget")
            self.assertNotIn("original/widget", out)

        def test_an_origin_that_names_no_project_prints_no_command_at_all(self):
            """Where the placeholder used to stand there is now nothing to put: the report
            says what it could not address and asks for the check to be done by hand, rather
            than printing a command that would reach some other repository."""
            self.gitlab_tool()
            out = self.report("gitlab", origin="/srv/mirrors/widget.git")
            self.assertIn("gitlab, but `/srv/mirrors/widget.git` names no project there", out)
            self.assertIn("check yourself that the default branch is `develop`", out)
            self.assertEqual(self.fixes(out), [])
            self.assertFalse(os.path.exists(self.log))     # and nothing was even read

        def test_a_github_url_with_more_than_owner_and_repo_is_not_guessed_at(self):
            """`repos/` takes exactly two segments; a third would address an endpoint of its
            own, so the report leaves it to the user instead."""
            self.github_tool()
            out = self.report("github", origin="https://github.com/acme/team/widget.git")
            self.assertIn("names no project there", out)
            self.assertEqual(self.fixes(out), [])
            self.assertFalse(os.path.exists(self.log))

    class TestPlatformReportInSetup(PlatformBase):
        def as_gitlab(self):
            """The fork under test pushes to a local origin: pretend it is a GitLab one, both
            for the host and for the project path the report builds out of it."""
            return mock.patch.multiple(sys.modules[__name__],
                                       detect_platform=lambda url: "gitlab",
                                       forge_path=lambda url: "acme/team/widget")

        def test_an_unknown_host_says_check_it_yourself(self):
            out = self.report("unknown")
            self.assertIn("unknown host", out)
            self.assertIn("`develop`", out)

        def test_a_fresh_fork_is_told_its_default_branch_is_wrong(self):
            fork = make_fresh_fork(self.tmp)
            self.gitlab_tool(project=('{"default_branch":"main","merge_method":"ff"}', 0),
                             trunk=UNPROTECTED, mirror=UNPROTECTED)
            with self.as_gitlab():
                code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertIn("default branch: `main` - it must be the trunk `develop`", out)
            self.assertIn("-f default_branch='develop'", out)
            self.assertIn("trunk `develop`: NOT protected", out)

        def test_a_dry_run_still_runs_the_read_only_report(self):
            fork = make_fork(self.tmp)
            self.gitlab_tool()
            with self.as_gitlab():
                code, out, err = run("-C", fork, "setup", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("read-only: default_branch=develop  merge_method=ff", out)
            self.assertIn("trunk `develop`: protected, force-push disallowed, "
                          "direct push blocked: ok", out)
            self.assertEqual(self.argv(), [
                "api", "projects/acme%2Fteam%2Fwidget",
                "api", "projects/acme%2Fteam%2Fwidget/protected_branches/develop",
                "api", "projects/acme%2Fteam%2Fwidget/protected_branches/main"])

    # ------------------------------------------------------------------- #
    # merge requests
    # ------------------------------------------------------------------- #

    class TestMrCommand(unittest.TestCase):
        def ctx(self, url: str, trunk: str = DEFAULT_TRUNK) -> Ctx:
            """A Ctx driven by the origin URL alone - no repository needed."""
            c = Ctx(root="/repo", origin_url=url, trunk=trunk)
            c.platform = detect_platform(url)
            return c

        def test_gitlab_command(self):
            ctx = self.ctx("git@gitlab.com:group/proj.git")
            self.assertEqual(
                mr_command(ctx, "sync/upstream-20260101", "sync: title", "/tmp/body.md"),
                ["glab", "mr", "create",
                 "--repo", "ssh://gitlab.com/group/proj",
                 "--source-branch", "sync/upstream-20260101",
                 "--target-branch", "develop",
                 "--title", "sync: title",
                 "--description-file", "/tmp/body.md",
                 "--remove-source-branch",
                 "--yes"])

        def test_github_command(self):
            ctx = self.ctx("https://github.com/owner/repo.git")
            self.assertEqual(
                mr_command(ctx, "feat/x", "ship: title", "/tmp/body.md"),
                ["gh", "pr", "create",
                 "--repo", "https://github.com/owner/repo",
                 "--head", "feat/x", "--base", "develop",
                 "--title", "ship: title", "--body-file", "/tmp/body.md"])

        def test_the_target_is_the_trunk_even_when_renamed(self):
            for url, flag in (("git@gitlab.com:g/p.git", "--target-branch"),
                              ("git@github.com:g/p.git", "--base")):
                cmd = mr_command(self.ctx(url, trunk="mainline"), "feat/x", "t", "/b.md")
                self.assertEqual(cmd[cmd.index(flag) + 1], "mainline", cmd)

        def test_unknown_platform_has_no_command(self):
            self.assertEqual(mr_command(self.ctx("/srv/git/repo.git"), "feat/x", "t", "/b.md"), [])

        def test_merge_buttons(self):
            gitlab, github = (self.ctx("https://gitlab.com/g/p"),
                              self.ctx("https://github.com/g/p"))
            unknown = self.ctx("/srv/git/p.git")
            for c in (gitlab, github, unknown):
                self.assertIn("never squash", merge_button(c, "sync"), c.platform)
                self.assertIn("never rebase", merge_button(c, "sync"), c.platform)
            self.assertIn("fast-forward", merge_button(gitlab, "ship"))
            self.assertIn("Rebase and merge", merge_button(github, "ship"))

    class TestMergeCommand(unittest.TestCase):
        """The merge command carries the method rule 5 requires and the head-commit guard,
        names the fork, and addresses the merge request by its source branch."""

        HEAD = "0123456789abcdef0123456789abcdef01234567"

        def ctx(self, url: str, trunk: str = DEFAULT_TRUNK) -> Ctx:
            c = Ctx(root="/repo", origin_url=url, trunk=trunk)
            c.platform = detect_platform(url)
            return c

        def test_gitlab_is_the_projects_method_with_the_guard_and_no_auto_merge(self):
            """No --squash and no --rebase: on GitLab the project's `merge_method` decides.
            `--auto-merge=false` is what makes glab merge now rather than arm
            merge-when-pipeline-succeeds, and `--yes` skips a confirmation nobody is at."""
            ctx = self.ctx("git@gitlab.com:group/proj.git")
            for kind in ("ship", "sync"):
                self.assertEqual(
                    merge_command(ctx, kind, "feat/x", self.HEAD),
                    ["glab", "mr", "merge", "feat/x",
                     "--repo", "ssh://gitlab.com/group/proj",
                     "--sha", self.HEAD,
                     "--auto-merge=false", "--remove-source-branch", "--yes"], kind)

        def test_github_is_told_the_method_per_call(self):
            """GitHub has no project-level method: `--merge` keeps a sync's merge commit,
            `--rebase` fast-forwards a ship's one commit (the "Rebase and merge" button)."""
            ctx = self.ctx("https://github.com/owner/repo.git")
            self.assertEqual(
                merge_command(ctx, "sync", "sync/upstream-20260101", self.HEAD),
                ["gh", "pr", "merge", "sync/upstream-20260101",
                 "--repo", "https://github.com/owner/repo",
                 "--match-head-commit", self.HEAD, "--merge"])
            self.assertEqual(
                merge_command(ctx, "ship", "feat/x", self.HEAD),
                ["gh", "pr", "merge", "feat/x",
                 "--repo", "https://github.com/owner/repo",
                 "--match-head-commit", self.HEAD, "--rebase"])
            for kind in ("ship", "sync"):
                self.assertNotIn("--squash", merge_command(ctx, kind, "feat/x", self.HEAD))

        def test_the_guard_is_the_head_it_was_given(self):
            for url, flag in (("git@gitlab.com:g/p.git", "--sha"),
                              ("git@github.com:g/p.git", "--match-head-commit")):
                cmd = merge_command(self.ctx(url), "ship", "feat/x", "abc123")
                self.assertEqual(cmd[cmd.index(flag) + 1], "abc123", cmd)
                self.assertEqual(cmd[3], "feat/x", cmd)          # addressed by branch

        def test_no_fork_to_name_means_no_command(self):
            self.assertEqual(merge_command(self.ctx("/srv/git/repo.git"), "ship", "feat/x",
                                           self.HEAD), [])
            # more than owner/repo on GitHub: gh refuses it, so `mr_target` names nothing
            self.assertEqual(merge_command(self.ctx("https://github.com/a/b/c.git"), "ship",
                                           "feat/x", self.HEAD), [])

    class TestMrCommandsNameTheFork(unittest.TestCase):
        """Every merge request command names the fork itself.

        Without `--repo` both tools resolve the repository from the remotes and answer with
        the one named `upstream` - the original project (verified on a real fork: `gh repo
        view --json nameWithOwner` in `aws-simple/tutorials` answers `antonputra/tutorials`).
        A printed command would then open the merge request there, and `--mr` would open it
        there itself: rule 1. The fake `gh`/`glab` of the end-to-end tests never resolves a
        base repository, so only the argv itself can prove this."""

        def ctx(self, origin: str, platform: str = "", upstream: str = "") -> Ctx:
            c = Ctx(root="/repo", origin_url=origin, upstream_url=upstream)
            c.platform = platform or detect_platform(origin)
            return c

        def target_of(self, cmd: Sequence[str]) -> str:
            self.assertIn("--repo", cmd)
            return cmd[list(cmd).index("--repo") + 1]

        def command(self, origin: str, platform: str = "", upstream: str = "") -> list:
            return mr_command(self.ctx(origin, platform, upstream), "feat/x", "t", "/b.md")

        def test_the_fork_is_named_however_the_origin_is_spelled(self):
            for origin, target in (
                    ("git@github.com:acme/widget.git", "ssh://github.com/acme/widget"),
                    ("https://github.com/acme/widget.git", "https://github.com/acme/widget"),
                    ("ssh://git@github.com/acme/widget", "ssh://github.com/acme/widget"),
                    ("git@gitlab.example.com:acme/team/widget.git",
                     "ssh://gitlab.example.com/acme/team/widget"),
                    ("https://gitlab.example.com/acme/team/widget.git",
                     "https://gitlab.example.com/acme/team/widget")):
                cmd = self.command(origin)
                self.assertEqual(self.target_of(cmd), target, origin)

        def test_it_is_the_fork_and_never_the_project_it_was_forked_from(self):
            """What both tools would have answered with, and the whole point of the flag."""
            for origin, upstream, project in (
                    ("git@github.com:acme/widget.git",
                     "https://github.com/original/widget.git", "acme/widget"),
                    ("https://gitlab.example.com/acme/team/widget.git",
                     "git@gitlab.example.com:original/widget.git", "acme/team/widget")):
                cmd = self.command(origin, upstream=upstream)
                self.assertIn(project, self.target_of(cmd))
                self.assertNotIn("original/widget", " ".join(cmd))

        def test_the_host_is_part_of_it(self):
            """A bare `owner/repo` is resolved against the tool's own default host, not the
            fork's: on a self-hosted GitLab `glab --repo acme/team/widget` asks gitlab.com
            (verified with glab 1.109), which is a different project altogether."""
            cmd = self.command("git@gitlab.example.com:acme/team/widget.git")
            self.assertTrue(self.target_of(cmd).startswith("ssh://gitlab.example.com/"), cmd)

        def test_a_subgroup_keeps_every_segment(self):
            cmd = self.command("https://gitlab.example.com/g/s/deep/proj.git")
            self.assertEqual(self.target_of(cmd), "https://gitlab.example.com/g/s/deep/proj")

        def test_the_spelling_of_the_project_is_kept(self):
            cmd = self.command("git@github.com:Acme/Widget.git")
            self.assertEqual(self.target_of(cmd), "ssh://github.com/Acme/Widget")

        def test_a_port_the_scheme_does_not_imply_is_kept(self):
            for origin, target in (
                    ("ssh://git@gitlab.example.com:2222/acme/widget.git",
                     "ssh://gitlab.example.com:2222/acme/widget"),
                    ("https://gitlab.example.com:8443/acme/widget.git",
                     "https://gitlab.example.com:8443/acme/widget")):
                self.assertEqual(self.target_of(self.command(origin, "gitlab")), target)

        def test_an_origin_that_names_no_project_gets_no_command_at_all(self):
            """A command without `--repo` is not merely unhelpful - it is aimed at the
            original project, so there is nothing safe left to print."""
            for origin, platform in (("/srv/mirrors/widget.git", "gitlab"),
                                     ("file:///srv/mirrors/widget.git", "github"),
                                     ("https://github.com/", "github"),
                                     ("", "gitlab")):
                self.assertEqual(self.command(origin, platform), [], origin)

        def test_a_github_url_with_more_than_owner_and_repo_is_not_guessed_at(self):
            """gh reads a third segment as a host (`-R a/b/c` asks `https://a/`) and refuses
            the same path in a URL outright ("invalid path"); GitLab nests, GitHub does not."""
            self.assertEqual(self.command("https://github.com/acme/team/widget.git"), [])

        def test_the_reason_names_the_url_that_could_not_be_addressed(self):
            self.assertEqual(mr_target(self.ctx("/srv/mirrors/widget.git", "gitlab")),
                             ("", "gitlab, but `/srv/mirrors/widget.git` names no project "
                                  "there"))
            self.assertEqual(mr_target(self.ctx("/srv/mirrors/widget.git"))[1], "unknown host")

    class TestOpenMr(Base):
        def test_unknown_platform_prints_the_manual_note(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            shown, out, _ = capture(open_mr, ctx, "feat/x", "a title", "body\n", True)
            self.assertEqual(shown, ("", ""))
            self.assertIn("open the merge request manually: feat/x -> develop", out)
            self.assertIn("a title", out)

        def test_what_is_shown_and_what_is_run_both_name_the_fork(self):
            """One command, printed and executed - and it names the fork, not the remote
            called `upstream`, which is what glab and gh resolve to by themselves."""
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = "gitlab"
            ctx.origin_url = "git@gitlab.example.com:acme/team/widget.git"
            ctx.upstream_url = "git@gitlab.example.com:original/widget.git"
            done = subprocess.CompletedProcess([], 0, b"https://gitlab.example.com/mr/1\n", b"")
            with mock.patch.object(subprocess, "run", return_value=done) as ran:
                (shown, url), out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            argv = list(ran.call_args[0][0])
            self.assertEqual(argv[argv.index("--repo") + 1],
                             "ssh://gitlab.example.com/acme/team/widget")
            self.assertNotIn("original/widget", " ".join(argv))
            self.assertIn("--repo ssh://gitlab.example.com/acme/team/widget", shown)
            self.assertIn("created", out)
            self.assertEqual(url, "https://gitlab.example.com/mr/1")

        def test_the_url_is_the_first_http_line_the_tool_prints_and_only_on_success(self):
            """glab and gh both print the merge request's URL on its own line, glab after a
            "Creating merge request for ..." line. Anything else - no such line, or a tool
            that failed after printing one - is no URL: the `pending` record must not name
            a merge request that does not exist."""
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = "gitlab"
            ctx.origin_url = "git@gitlab.example.com:acme/team/widget.git"
            chatty = subprocess.CompletedProcess(
                [], 0, b"Creating merge request for feat/x into develop in acme/team/widget\n"
                       b"https://gitlab.example.com/acme/team/widget/-/merge_requests/2\n"
                       b"  more\n", b"")
            with mock.patch.object(subprocess, "run", return_value=chatty):
                (shown, url), _, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertTrue(shown)
            self.assertEqual(url, "https://gitlab.example.com/acme/team/widget/-/merge_requests/2")
            silent = subprocess.CompletedProcess([], 0, b"done\n", b"")
            with mock.patch.object(subprocess, "run", return_value=silent):
                (shown, url), _, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertTrue(shown)
            self.assertEqual(url, "")
            failed = subprocess.CompletedProcess([], 1, b"https://gitlab.example.com/x\n",
                                                 b"glab: pipeline required\n")
            with mock.patch.object(subprocess, "run", return_value=failed):
                (shown, url), out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertTrue(shown)
            self.assertEqual(url, "")
            self.assertIn("FAILED (exit 1)", out)

        def test_the_tool_runs_with_its_confirmation_skipped_and_stdin_closed(self):
            """--mr has no terminal behind it. glab's "create this merge request?" prompt is
            skipped with --yes, and stdin is closed so any prompt the flags did not cover
            fails fast as an exit code instead of waiting. The spy is on the call rather than
            on a fake that reads stdin: a fake that blocks on a real terminal would hang a
            developer's suite, which is the failure being guarded against."""
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = "gitlab"
            ctx.origin_url = "git@gitlab.example.com:acme/team/widget.git"
            ctx.upstream_url = "git@gitlab.example.com:original/widget.git"
            done = subprocess.CompletedProcess([], 0, b"https://gitlab.example.com/mr/1\n", b"")
            with mock.patch.object(subprocess, "run", return_value=done) as ran:
                (shown, _url), out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            argv = list(ran.call_args[0][0])
            self.assertIn("--yes", argv)
            self.assertIs(ran.call_args[1].get("stdin"), subprocess.DEVNULL)
            self.assertIn("--yes", shown)         # the printed command is the runnable one
            ctx.platform = "github"
            ctx.origin_url = "git@github.com:acme/widget.git"
            with mock.patch.object(subprocess, "run", return_value=done) as ran:
                capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertNotIn("--yes", list(ran.call_args[0][0]))   # gh has no such flag
            self.assertIs(ran.call_args[1].get("stdin"), subprocess.DEVNULL)

        def test_a_known_platform_that_names_no_project_runs_nothing(self):
            """--mr with nothing to aim at: the manual note, and no tool run at all - a
            command without the flag would have gone to the original project."""
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = "github"          # every fixture fork pushes to a local origin
            with mock.patch.object(subprocess, "run") as ran:
                shown, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            ran.assert_not_called()
            self.assertEqual(shown, ("", ""))
            self.assertIn("names no project there", out)
            self.assertIn("open the merge request manually: feat/x -> develop", out)

        def test_missing_tool_is_reported_and_never_raises(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            ctx.platform = "gitlab"
            ctx.origin_url = "git@gitlab.example.com:acme/team/widget.git"
            missing = FileNotFoundError(2, "No such file or directory")
            with mock.patch.object(subprocess, "run", side_effect=missing):
                (shown, url), out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("glab mr create --repo ssh://gitlab.example.com/acme/team/widget",
                          shown)
            self.assertEqual(url, "")
            self.assertIn("glab unavailable", out)

        def temp_files(self) -> list:
            return sorted(f for f in os.listdir(tempfile.gettempdir())
                          if f.startswith("forkflow-"))

        def test_dry_run_writes_no_body_file(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork, dry_run=True)
            ctx.platform = "github"
            ctx.origin_url = "https://github.com/acme/widget.git"
            before = self.temp_files()
            (shown, url), out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("gh pr create --repo https://github.com/acme/widget", shown)
            self.assertIn("<description file>", shown)
            self.assertEqual(url, "")
            self.assertIn("not run (dry run)", out)
            self.assertEqual(self.temp_files(), before)      # nothing was written anywhere

    class TestRunTool(Base):
        """`run_tool` is the one place glab and gh are run from (`TestSourceInvariants` pins
        it): output captured, stdin closed, and a tool that cannot run is None, not a
        raise - the command is still worth printing for the user to run by hand."""

        def test_a_missing_tool_is_none_and_never_raises(self):
            ctx = ctx_for(make_fork(self.tmp))
            self.assertIsNone(run_tool(ctx, ["forkflow-no-such-tool", "mr", "create"]))

        def test_a_present_tool_answers_with_its_process(self):
            ctx = ctx_for(make_fork(self.tmp))
            p = run_tool(ctx, ["sh", "-c", "echo out; echo err >&2; exit 3"])
            self.assertEqual((p.returncode, p.stdout, p.stderr), (3, b"out\n", b"err\n"))

        def test_stdin_is_closed_so_a_prompt_cannot_wait(self):
            """A tool that asks a question reads end-of-file at once, not the developer's
            terminal - the failure guarded against is a suite (or a `--mr`) that hangs."""
            ctx = ctx_for(make_fork(self.tmp))
            p = run_tool(ctx, ["sh", "-c", 'read answer; echo "got:$answer"'])
            self.assertEqual(p.stdout, b"got:\n")

    class TestMrEndToEnd(Base):
        def record(self, name: str) -> str:
            """A fake glab/gh that records its argv one argument per line, keeps a copy of the
            description file it was handed - forkflow deletes that file once the merge request
            exists - and prints a URL."""
            log = os.path.join(self.tmp, name + "-argv.txt")
            self.body_copy = os.path.join(self.tmp, name + "-body-copy.md")
            fake_tool(os.path.join(self.tmp, "bin"), name,
                      'for a in "$@"; do echo "$a"; done > %s\n'
                      'for a in "$@"; do [ -f "$a" ] && cp "$a" %s; done\n'
                      'echo "https://example.invalid/merge_requests/1"\n'
                      % (shlex.quote(log), shlex.quote(self.body_copy)))
            return log

        def argv(self, log: str) -> list:
            with open(log) as fh:
                return [ln.rstrip("\n") for ln in fh]

        def value(self, argv: Sequence[str], flag: str) -> str:
            self.assertIn(flag, argv)
            return argv[list(argv).index(flag) + 1]

        def body_of(self, argv: Sequence[str], flag: str) -> str:
            self.value(argv, flag)                       # the file was named on the command line
            with open(self.body_copy) as fh:
                return fh.read()

        # the fixture fork really does push to a local origin, so the platform and the fork
        # it names are both supplied here; `TestMrCommandsNameTheFork` proves the URL these
        # stand for is built from the origin. What these tests prove is that the argv the
        # tool receives carries it - the fake `gh`/`glab` resolves no base repository itself
        GL_FORK = "ssh://gitlab.example.com/acme/team/widget"
        GH_FORK = "https://github.com/acme/widget"

        def as_gitlab(self):
            return mock.patch.multiple(sys.modules[__name__],
                                       detect_platform=lambda url: "gitlab",
                                       mr_target=lambda ctx: (self.GL_FORK, ""))

        def as_github(self):
            return mock.patch.multiple(sys.modules[__name__],
                                       detect_platform=lambda url: "github",
                                       mr_target=lambda ctx: (self.GH_FORK, ""))

        def test_sync_mr_runs_glab_with_the_sync_body(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf",
                        'resource "null_resource" "a" {\n  count = 2\n}\n',
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            log = self.record("glab")
            name = sync_branch_name()

            with self.as_gitlab():
                code, out, err = run("-C", fork, "sync", "--mr")
            self.assertEqual(code, 0, err + out)

            argv = self.argv(log)
            self.assertEqual(argv[:2], ["mr", "create"])
            self.assertIn("--remove-source-branch", argv)
            # no terminal answers glab's confirmation under --mr; on the first real fork the
            # printed command was rerun by hand with --yes for all eight merge requests
            self.assertIn("--yes", argv)
            # the merge request is opened on the fork; without this glab would resolve the
            # project from the remotes and answer with `upstream` - the original project
            self.assertEqual(self.value(argv, "--repo"), self.GL_FORK)
            self.assertEqual(self.value(argv, "--source-branch"), name)
            self.assertEqual(self.value(argv, "--target-branch"), "develop")
            self.assertEqual(self.value(argv, "--title"),
                             "sync: upstream/main %s (1 commits)" % UTC_DATE)

            body = self.body_of(argv, "--description-file")
            self.assertIn("theirs: docs", body)                     # upstream commits taken
            self.assertIn("Upstream commits taken (1)", body)
            self.assertIn("Files changed on both sides", body)
            self.assertIn("Mirror `main`:", body)                   # the mirror advance
            self.assertIn(DEFAULT_BACKUP_PREFIX, body)              # backup name
            self.assertIn("Rollback: `git reset --hard origin/" + DEFAULT_BACKUP_PREFIX, body)
            self.assertIn("never squash it, never rebase it", body)  # merge button
            self.assertIn("created", out)
            self.assertIn("https://example.invalid/merge_requests/1", out)

        def test_ship_mr_runs_gh_with_the_commit_message_as_the_body(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            commit_fork(fork, "src/app.py", "def main():\n    return 42\n", "ours: bump app")
            second_clone_commit(self.tmp)
            log = self.record("gh")

            with self.as_github():
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)

            argv = self.argv(log)
            self.assertEqual(argv[:2], ["pr", "create"])
            self.assertNotIn("--yes", argv)      # gh has no such flag; --title/--body-file suffice
            self.assertEqual(self.value(argv, "--repo"), self.GH_FORK)   # fork, not upstream
            self.assertEqual(self.value(argv, "--head"), "feat/x")
            self.assertEqual(self.value(argv, "--base"), "develop")
            self.assertEqual(self.value(argv, "--title"), "ours: bump app")

            body = self.body_of(argv, "--body-file")
            self.assertIn("ours: bump app", body)
            self.assertIn("Upstream-tracked files touched (1)", body)   # the WARNING list
            self.assertIn("- `src/app.py`", body)
            self.assertIn("Rebase and merge", body)                     # merge button
            self.assertIn("created", out)
            # the platform has the description now: no temp file is left behind
            self.assertFalse(os.path.exists(self.value(argv, "--body-file")))

        def test_titles_are_overridden_and_the_command_is_only_printed_without_mr(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            with self.as_gitlab():
                code, out, err = run("-C", fork, "sync", "--title", "sync: hand written")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--title 'sync: hand written'", out)
            self.assertIn("not run (add --mr to run it)", out)

        def test_ship_title_is_overridden(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n", "ours: a")
            second_clone_commit(self.tmp)
            with self.as_github():
                code, out, err = run("-C", fork, "ship", "--title", "ship: hand written")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--title 'ship: hand written'", out)

        def test_a_failing_tool_leaves_the_branch_pushed_and_exits_0(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            fake_tool(os.path.join(self.tmp, "bin"), "glab",
                      'echo "glab: not authenticated" >&2\nexit 1\n')
            name = sync_branch_name()

            with self.as_gitlab():
                code, out, err = run("-C", fork, "sync", "--mr")
            self.assertEqual(code, 0, err + out)
            self.assertIn("FAILED (exit 1) - open it yourself", out)
            self.assertIn("glab: not authenticated", out)
            self.assertIn("description:", out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))

        def test_a_missing_tool_exits_0_with_the_command(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n", "ours: a")
            second_clone_commit(self.tmp)
            absent = ["forkflow-no-such-tool", "pr", "create"]

            with self.as_github(), mock.patch.object(
                    sys.modules[__name__], "mr_command", lambda *a: absent):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            self.assertIn("forkflow-no-such-tool unavailable", out)
            self.assertEqual(origin_sha(fork, "feat/x"), rev(fork, "HEAD"))

    class TestPendingRecord(ShipBase):
        """Every `ship` and `sync` that pushed a branch leaves the `pending` record `land`
        works from - with or without `--mr`, whether or not the merge request could be
        opened, and with the URL the tool printed when it could."""

        FORKS = {"gitlab": "ssh://gitlab.example.com/acme/team/widget",
                 "github": "https://github.com/acme/widget"}

        def on(self, platform: str):
            return mock.patch.multiple(sys.modules[__name__],
                                       detect_platform=lambda url: platform,
                                       mr_target=lambda ctx: (self.FORKS[platform], ""))

        def url_tool(self, name: str, url: str) -> None:
            """A glab/gh that prints a chatty line first, then the URL - as both tools do."""
            fake_tool(os.path.join(self.tmp, "bin"), name,
                      'echo "Creating a merge request for $2"\necho %s\n' % shlex.quote(url))

        @staticmethod
        def pending_of(fork: str) -> dict:
            return pending_entry(ctx_for(fork, need_trunk=False, strict_mirror=False))

        @staticmethod
        def parents_of(fork: str, sha: str) -> list:
            return sh("git", "rev-list", "--parents", "-1", sha, cwd=fork).split()[1:]

        def test_ship_records_the_squashed_tip_and_the_trunk_it_was_built_on(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=2)
            base = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual(entry, {"kind": "ship", "branch": name, "commit": rev(fork, name),
                                     "base": base, "mr": ""})
            self.assertEqual(entry["commit"], origin_sha(fork, name))     # the pushed tip
            self.assertEqual(self.parents_of(fork, entry["commit"]), [base])   # one squash
            self.assertEqual(resumable(ctx_for(fork), "ship", name), {})   # still cleared

        def test_ship_base_is_the_fetched_trunk_not_the_stale_local_one(self):
            """`base` is `origin/<trunk>` after the fetch the run made - the commit the
            squash sits on - not the local trunk, which may be behind."""
            fork = make_fork(self.tmp)
            name = self.feature(fork)
            moved = second_clone_commit(self.tmp)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual(entry["base"], moved)
            self.assertEqual(self.parents_of(fork, entry["commit"]), [moved])
            self.assertNotEqual(rev(fork, "develop"), moved)              # local trunk untouched

        def test_sync_records_the_merge_commit(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            base = origin_sha(fork, "develop")
            name = sync_branch_name()
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual(entry, {"kind": "sync", "branch": name, "commit": rev(fork, name),
                                     "base": base, "mr": ""})
            self.assertEqual(entry["commit"], origin_sha(fork, name))
            parents = self.parents_of(fork, entry["commit"])
            self.assertEqual(len(parents), 2)                             # the merge commit
            self.assertEqual(parents[0], base)
            self.assertEqual(resumable(ctx_for(fork, strict_mirror=False), "sync", name), {})

        def test_the_url_gh_prints_is_recorded_for_a_ship(self):
            fork = make_fork(self.tmp)
            name = self.feature(fork)
            self.url_tool("gh", "https://github.com/acme/widget/pull/7")
            with self.on("github"):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["mr"]),
                             ("ship", name, "https://github.com/acme/widget/pull/7"))
            self.assertEqual(entry["commit"], origin_sha(fork, name))

        def test_the_url_glab_prints_is_recorded_for_a_sync(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            url = "https://gitlab.example.com/acme/team/widget/-/merge_requests/3"
            self.url_tool("glab", url)
            with self.on("gitlab"):
                code, out, err = run("-C", fork, "sync", "--mr")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["mr"]),
                             ("sync", sync_branch_name(), url))

        def test_a_failed_or_missing_tool_still_leaves_the_record(self):
            """The branch is pushed either way; the merge request is what is missing, and
            `land` after a by-hand merge needs the record just the same."""
            fork = make_fork(self.tmp)
            name = self.feature(fork)
            fake_tool(os.path.join(self.tmp, "bin"), "glab",
                      'echo "https://example.invalid/-/merge_requests/9"\n'
                      'echo "glab: not authenticated" >&2\nexit 1\n')
            with self.on("gitlab"):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["mr"]), ("ship", name, ""))
            self.assertEqual(entry["commit"], origin_sha(fork, name))

            sh("git", "checkout", "-b", "feat/y", "develop", cwd=fork)
            commit_fork(fork, "ours/y.txt", "y\n", "ours: y")
            absent = ["forkflow-no-such-tool", "mr", "create"]
            with self.on("gitlab"), mock.patch.object(
                    sys.modules[__name__], "mr_command", lambda *a: absent):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)                  # the most recent run, URL-less
            self.assertEqual((entry["kind"], entry["branch"], entry["mr"]),
                             ("ship", "feat/y", ""))

        def test_a_dry_run_writes_no_record(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            code, out, err = run("-C", fork, "ship", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertFalse(os.path.exists(git_path(fork, STATE_FILE)))
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertFalse(os.path.exists(git_path(fork, STATE_FILE)))

    @needs_tomllib
    class MergeBase(ShipBase):
        """A `merge = "self"` fork on a named platform, with `merging_tool` as the platform:
        what the `--merge` tests and the `land`-after-`--merge` tests share. The fixture's
        origin is a local path, so the platform and the fork it names are supplied
        (`TestMrCommandsNameTheFork` proves the URL)."""

        FORKS = {"gitlab": "ssh://gitlab.example.com/acme/team/widget",
                 "github": "https://github.com/acme/widget"}
        TOOLS = {"gitlab": "glab", "github": "gh"}

        def on(self, platform: str):
            return mock.patch.multiple(sys.modules[__name__],
                                       detect_platform=lambda url: platform,
                                       mr_target=lambda ctx: (self.FORKS[platform], ""))

        def self_fork(self) -> str:
            return make_fork(self.tmp, config='merge = "self"\n')

        def platform(self, name: str) -> str:
            """A merging glab/gh on PATH; answers with the platform it stands for."""
            merging_tool(self.tmp, self.TOOLS[name])
            return name

        def argv(self, platform: str, sub: str) -> list:
            return tool_argv(self.tmp, self.TOOLS[platform], sub)

        def value(self, argv: Sequence[str], flag: str) -> str:
            self.assertIn(flag, argv)
            return argv[list(argv).index(flag) + 1]

        def upstream_change(self) -> None:
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")

        def state_file(self, fork: str) -> str:
            return git_path(fork, STATE_FILE)

        @staticmethod
        def pending_of(fork: str) -> dict:
            return pending_entry(ctx_for(fork, need_trunk=False, strict_mirror=False))

        @staticmethod
        def tree_of(fork: str, sha: str) -> str:
            return sh("git", "rev-parse", sha + "^{tree}", cwd=fork)

    class TestMergeStep(MergeBase):
        """`--merge`: the merge request the run just opened is merged by the platform tool
        with the method rule 5 requires and a head-commit guard - or the run is exit 6 with
        the branch pushed, the merge request open and the `pending` record intact.

        The platform is `merging_tool`, which moves the bare origin's trunk the way the real
        one would, so a success is asserted on the trunk moving and not on the argv alone.
        Nothing here asserts on `pending` after a successful merge: `land` chains in after
        it and clears the record (`TestMergeLands` asserts that chain)."""

        # -- success: the trunk moves, by the platform, to what this run pushed ----------

        def test_gitlab_ship_merges_with_the_projects_method_and_the_head_guard(self):
            fork = self.self_fork()
            name = self.feature(fork, commits=2)
            base = origin_sha(fork, "develop")
            with self.on(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            shipped = origin_sha(fork, name)
            self.assertNotEqual(shipped, "")
            self.assertEqual(self.argv("gitlab", "create")[:2], ["mr", "create"])
            argv = self.argv("gitlab", "merge")
            self.assertEqual(argv[:3], ["mr", "merge", name])          # by branch, not URL
            self.assertEqual(self.value(argv, "--repo"), self.FORKS["gitlab"])
            self.assertEqual(self.value(argv, "--sha"), shipped)       # the pushed head
            self.assertIn("--auto-merge=false", argv)
            self.assertIn("--remove-source-branch", argv)
            self.assertIn("--yes", argv)
            for flag in ("--squash", "--rebase", "--merge"):
                self.assertNotIn(flag, argv)               # the project's method decides
            self.assertNotEqual(origin_sha(fork, "develop"), base)
            self.assertEqual(origin_sha(fork, "develop"), shipped)     # fast-forwarded
            self.assertIn(MERGING_TOOL_URL["glab"], out)
            self.assertIn("merged", out)

        def test_gitlab_sync_merges_the_merge_commit_fast_forward(self):
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            with self.on(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            synced = origin_sha(fork, name)
            argv = self.argv("gitlab", "merge")
            self.assertEqual(argv[:3], ["mr", "merge", name])
            self.assertEqual(self.value(argv, "--sha"), synced)
            self.assertEqual(origin_sha(fork, "develop"), synced)
            self.assertEqual(len(sh("git", "rev-list", "--parents", "-1", synced,
                                    cwd=fork).split()), 3)            # still the merge commit

        def test_github_ship_merges_with_rebase_and_the_head_guard(self):
            """"Rebase and merge" rewrites the commit: the trunk gets a new SHA with the
            same patch on top of the base - which is what the fake does, and what `land`
            has to recognise later."""
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            with self.on(self.platform("github")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            shipped = origin_sha(fork, name)
            argv = self.argv("github", "merge")
            self.assertEqual(argv[:3], ["pr", "merge", name])
            self.assertEqual(self.value(argv, "--repo"), self.FORKS["github"])
            self.assertEqual(self.value(argv, "--match-head-commit"), shipped)
            self.assertIn("--rebase", argv)
            for flag in ("--squash", "--merge", "--auto-merge=false", "--yes"):
                self.assertNotIn(flag, argv)
            sh("git", "fetch", "origin", cwd=fork)
            landed = origin_sha(fork, "develop")
            self.assertNotIn(landed, (base, shipped))                  # rewritten
            self.assertEqual(sh("git", "rev-list", "--parents", "-1", landed,
                                cwd=fork).split()[1:], [base])
            self.assertEqual(self.tree_of(fork, landed), self.tree_of(fork, shipped))

        def test_github_sync_merges_with_merge(self):
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            with self.on(self.platform("github")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            argv = self.argv("github", "merge")
            self.assertEqual(argv[:3], ["pr", "merge", name])
            self.assertEqual(self.value(argv, "--match-head-commit"), origin_sha(fork, name))
            self.assertIn("--merge", argv)
            self.assertNotIn("--rebase", argv)
            self.assertNotIn("--squash", argv)
            self.assertEqual(origin_sha(fork, "develop"), origin_sha(fork, name))

        # -- exit 6: the branch is pushed, the record is landable ------------------------

        def refused_merge(self, fork: str, name: str, kind: str, base: str,
                          platform: str, *argv: str) -> dict:
            """Run with --merge, expect exit 6, and answer with the intact record."""
            with self.on(platform):
                code, out, err = run("-C", fork, kind, "--merge", *argv)
            self.assertEqual(code, 6, err + out)
            self.assertIn("forkflow land", err)                        # the way out
            self.assertEqual(origin_sha(fork, "develop"), base)        # trunk unchanged
            entry = self.pending_of(fork)
            self.assertEqual((entry.get("kind"), entry.get("branch")), (kind, name))
            self.assertEqual(entry.get("commit"), rev(fork, "refs/heads/" + name))
            self.assertEqual(entry.get("base"), base)
            self.assertEqual(resumable(ctx_for(fork, strict_mirror=False), kind, name), {})
            return entry

        def head_moved(self, platform: str, flag: str) -> None:
            """A teammate's commit reaches the branch between the push and the merge: the
            guard names the commit this run pushed, and the platform refuses - the merge
            must never take whatever the branch points at by then. The proof is the fake's
            refusal, not the argv; and the argv carries exactly the recorded commit."""
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            commit_after_receive(self.tmp)
            with self.on(self.platform(platform)):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 6, err + out)
            self.assertIn("head mismatch", out)                        # the fake's refusal
            self.assertIn("NOT MERGED (exit 1)", out)
            self.assertIn("forkflow land", err)                        # the way out
            self.assertEqual(origin_sha(fork, "develop"), base)        # trunk unchanged
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["base"]),
                             ("ship", name, base))
            self.assertEqual(entry["commit"], rev(fork, "refs/heads/" + name))
            self.assertEqual(entry["mr"], MERGING_TOOL_URL[self.TOOLS[platform]])
            self.assertEqual(self.value(self.argv(platform, "merge"), flag), entry["commit"])
            self.assertNotEqual(origin_sha(fork, name), entry["commit"])    # it did move
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), name)

        def test_gitlab_refuses_a_head_that_moved_after_the_push(self):
            self.head_moved("gitlab", "--sha")

        def test_github_refuses_a_head_that_moved_after_the_push(self):
            self.head_moved("github", "--match-head-commit")

        def test_a_failing_merge_is_exit_6_with_the_request_open(self):
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            base = origin_sha(fork, "develop")
            os.environ["FORKFLOW_FAKE_FAIL"] = "1"
            with self.on(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 6, err + out)
            self.assertIn("created", out)                              # the MR is open ...
            self.assertIn(MERGING_TOOL_URL["glab"], out)
            self.assertIn("NOT MERGED (exit 1)", out)                  # ... and not merged
            self.assertIn("FORKFLOW_FAKE_FAIL", out)                   # the tool's stderr
            self.assertIn("not merged", err)
            self.assertEqual(origin_sha(fork, "develop"), base)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            entry = self.pending_of(fork)
            self.assertEqual(entry, {"kind": "sync", "branch": name,
                                     "commit": rev(fork, "refs/heads/" + name),
                                     "base": base, "mr": MERGING_TOOL_URL["glab"]})
            self.assertEqual(resumable(ctx_for(fork, strict_mirror=False), "sync", name), {})

        def test_a_merge_request_that_was_not_created_is_exit_6(self):
            """The tool failed at `create`, so there is no merge request to merge: no merge
            is attempted, and the record is there for a by-hand merge and `land`."""
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            fake_tool(os.path.join(self.tmp, "bin"), "glab",
                      'echo "glab: not authenticated" >&2\nexit 1\n')
            entry = self.refused_merge(fork, name, "ship", base, "gitlab")
            self.assertEqual(entry["mr"], "")
            self.assertEqual(self.argv("gitlab", "merge"), [])          # never ran
            self.assertEqual(origin_sha(fork, name), entry["commit"])   # pushed all the same

        def test_a_missing_merge_tool_is_exit_6(self):
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            self.platform("github")
            absent = ["forkflow-no-such-tool", "pr", "merge", name]
            with mock.patch.object(sys.modules[__name__], "merge_command", lambda *a: absent):
                with self.on("github"):
                    code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 6, err + out)
            self.assertIn("NOT MERGED (forkflow-no-such-tool unavailable", out)
            self.assertEqual(origin_sha(fork, "develop"), base)
            self.assertEqual(self.pending_of(fork)["mr"], MERGING_TOOL_URL["gh"])

        # -- what does not merge ----------------------------------------------------------

        def test_mr_without_merge_runs_no_merge(self):
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            with self.on(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.argv("gitlab", "create")[:2], ["mr", "create"])
            self.assertEqual(self.argv("gitlab", "merge"), [])
            self.assertEqual(origin_sha(fork, "develop"), base)
            self.assertEqual(self.pending_of(fork)["mr"], MERGING_TOOL_URL["glab"])
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), name)

        def test_a_dry_run_runs_nothing_and_writes_nothing(self):
            fork = self.self_fork()
            self.upstream_change()
            before = (origin_sha(fork, "develop"), origin_sha(fork, "main"))
            self.platform("gitlab")
            with self.on("gitlab"):
                code, out, err = run("-C", fork, "sync", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would: merge", out)
            self.assertIn("would: land", out)
            self.assertIn("unless --merge lands it", out)         # where you end up
            name = self.feature(fork)
            with self.on("gitlab"):
                code, out, err = run("-C", fork, "ship", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would: merge", out)
            self.assertIn("glab mr merge " + name, out)
            self.assertIn("would: land", out)
            self.assertEqual(self.argv("gitlab", "create"), [])       # neither tool ran
            self.assertEqual(self.argv("gitlab", "merge"), [])
            self.assertFalse(os.path.exists(self.state_file(fork)))    # no state at all
            self.assertEqual((origin_sha(fork, "develop"), origin_sha(fork, "main")), before)
            self.assertEqual(self.backup_branch(fork), "")
            self.assertEqual(origin_sha(fork, name), "")                # nothing pushed

    # ------------------------------------------------------------------- #
    # land
    # ------------------------------------------------------------------- #

    class TestLand(ShipBase):
        """`land`: the closing step, from the `pending` record a ship or a sync left.

        The merge is a human's here - the bare origin's trunk is moved by the test, the way
        the platform's merge button moves it: a fast-forward, a cherry-pick (rebase and
        merge), a squash, a merge commit. Assertions are on state: where the local trunk,
        HEAD and the landed branch are afterwards, and whether the record is gone."""

        MR = "https://example.invalid/-/merge_requests/1"

        def origin_git(self) -> str:
            return os.path.join(self.tmp, "origin.git")

        def move_trunk(self, sha: str) -> None:
            """A human merged the MR by fast-forward: the bare origin's trunk moves to `sha`."""
            sh("git", "--git-dir=" + self.origin_git(), "update-ref", "refs/heads/develop", sha)

        def human(self, *cmds: Sequence[str]) -> str:
            """A clone somebody else merges from: origin/develop checked out, each of `cmds`
            run there, the result pushed; answers with the trunk's new tip.

            Under their own committer identity: a cherry-pick onto the same parent by the
            same committer in the same second is byte-for-byte the shipped commit again,
            and a "rewrite" that keeps the SHA would prove nothing."""
            clone = os.path.join(self.tmp, "human")
            if not os.path.exists(clone):
                sh("git", "clone", "-q", self.origin_git(), clone)
                identity(clone)
            with mock.patch.dict(os.environ, {"GIT_COMMITTER_NAME": "a human",
                                              "GIT_COMMITTER_EMAIL": "human@example.invalid"}):
                sh("git", "fetch", "-q", "origin", cwd=clone)
                sh("git", "checkout", "-q", "-B", "develop", "origin/develop", cwd=clone)
                for cmd in cmds:
                    sh("git", *cmd, cwd=clone)
                sh("git", "push", "-q", "origin", "develop", cwd=clone)
            return sh("git", "rev-parse", "HEAD", cwd=clone)

        def parent_of(self, sha: str) -> str:
            return sh("git", "rev-parse", sha + "^", cwd=os.path.join(self.tmp, "human"))

        @staticmethod
        def pending_of(fork: str) -> dict:
            return pending_entry(ctx_for(fork, need_trunk=False, strict_mirror=False))

        def with_mr(self, fork: str, entry: dict) -> dict:
            """The record as a run with `--mr` leaves it: the merge request's URL known."""
            entry = dict(entry, mr=self.MR)
            write_state(ctx_for(fork, strict_mirror=False), "pending", entry)
            return entry

        def shipped(self, commits: int = 1) -> Tuple[str, str, dict]:
            """A fork whose feature branch is shipped and pushed, the MR not merged yet."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=commits)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertIn("next: forkflow land", out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"]), ("ship", name))
            return fork, name, entry

        def synced(self) -> Tuple[str, str, dict]:
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            self.assertIn("next: forkflow land", out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"]), ("sync", sync_branch_name()))
            return fork, entry["branch"], entry

        def land(self, fork: str, *flags: str):
            return run("-C", fork, "land", *flags)

        def assert_landed(self, fork: str, name: str, tip: str) -> None:
            """The local trunk is at `tip` and checked out, the branch is gone, the record
            is cleared, and the tree is clean."""
            self.assertEqual(rev(fork, "refs/heads/develop"), tip)
            self.assertEqual(rev(fork, "refs/remotes/origin/develop"), tip)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(rev(fork, "refs/heads/" + name), "")
            self.assertEqual(self.pending_of(fork), {})
            self.assertEqual(sh("git", "status", "--porcelain", cwd=fork), "")

        def assert_untouched(self, fork: str, name: str, entry: dict, on: str) -> None:
            """Nothing moved: local trunk, HEAD, the branch and the record as they were."""
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["base"])
            self.assertEqual(checked_out(fork), on)
            self.assertEqual(rev(fork, "refs/heads/" + name), entry["commit"])
            self.assertEqual(self.pending_of(fork), entry)

        # -- preflight: exit 2, nothing moves ----------------------------------------------

        def test_nothing_pending_is_exit_2(self):
            fork = make_fork(self.tmp)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("nothing pending", err)
            self.assertEqual(checked_out(fork), "develop")

        def test_a_dirty_tree_is_exit_2(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            write(fork, "README.md", "# edited\n")
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("uncommitted changes", err)
            self.assert_untouched(fork, name, entry, on=name)

        def test_a_rebase_in_progress_is_exit_2(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "checkout", "-q", "-b", "other", "develop", cwd=fork)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "other: shared")
            second_clone_commit(self.tmp, path="shared.tf",
                                content=self.BASE_TF.replace("count = 1", "count = 9"))
            sh("git", "fetch", "-q", "origin", cwd=fork)
            sh("git", "rebase", "origin/develop", cwd=fork, check=False)   # stops on shared.tf
            self.assertTrue(rebase_in_progress(ctx_for(fork, strict_mirror=False)))
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("rebase is in progress", err)
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["base"])
            self.assertEqual(self.pending_of(fork), entry)

        def test_the_trunk_checked_out_in_another_worktree_is_exit_2(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            wt = os.path.join(self.tmp, "wt")
            sh("git", "worktree", "add", "-q", wt, "develop", cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("checked out in", err)
            self.assertIn(os.path.basename(wt), err)                 # the path is named
            self.assert_untouched(fork, name, entry, on=name)

        # -- not landed --------------------------------------------------------------------

        def test_a_ship_not_merged_yet_is_exit_2_naming_the_request(self):
            fork, name, entry = self.shipped()
            entry = self.with_mr(fork, entry)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn(self.MR, err)
            self.assertIn("not on origin/develop yet", err)
            self.assertNotIn("rule 5", err)                          # a ship may be rewritten
            self.assert_untouched(fork, name, entry, on=name)
            self.assertEqual(origin_sha(fork, "develop"), entry["base"])

        def test_a_sync_not_merged_yet_carries_the_rule_5_note(self):
            fork, name, entry = self.synced()
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("`%s`" % name, err)                        # no URL: the branch
            self.assertIn("rule 5", err)
            self.assertIn("land --force", err)
            self.assert_untouched(fork, name, entry, on=name)

        def test_the_ship_of_another_clone_cannot_be_verified_here(self):
            """A record whose commit this clone does not have - the state file copied from
            elsewhere, say - is refused rather than guessed at."""
            fork, name, entry = self.shipped()
            elsewhere = commit_upstream(self.tmp, "docs/x.md", "x\n")   # never fetched here
            entry = dict(entry, commit=elsewhere, branch="feat/other")
            write_state(ctx_for(fork, strict_mirror=False), "pending", entry)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("cannot verify", err)
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["base"])

        # -- landed ------------------------------------------------------------------------

        def test_a_ship_merged_by_fast_forward_lands_by_ancestry(self):
            fork, name, entry = self.shipped(commits=2)
            self.move_trunk(entry["commit"])
            old = rev(fork, "refs/heads/develop")
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("(ancestor)", out)
            self.assertIn("git branch -d " + name, out)
            self.assertIn("landed: develop %s..%s" % (short(old), short(entry["commit"])), out)
            self.assertIn("you are on develop", out)
            self.assertNotIn("WARNING", out)
            self.assert_landed(fork, name, entry["commit"])
            self.assertTrue(os.path.exists(os.path.join(fork, "ours", "f1.txt")))  # files follow

        def test_a_sync_merged_as_its_merge_commit_lands(self):
            fork, name, entry = self.synced()
            self.move_trunk(entry["commit"])
            mirror = rev(fork, "refs/heads/main")
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assert_landed(fork, name, entry["commit"])
            self.assertEqual(rev(fork, "refs/heads/main"), mirror)     # the mirror is not touched
            self.assertTrue(os.path.exists(os.path.join(fork, "docs", "theirs.md")))

        def test_a_ship_rebased_by_the_platform_lands_by_its_patch(self):
            """"Rebase and merge": a new SHA with the same patch. `-d` would refuse the
            branch (its tip is unreachable from the trunk), so the verified landing uses `-D`."""
            fork, name, entry = self.shipped()
            tip = self.human(("cherry-pick", "origin/" + name))
            self.assertNotEqual(tip, entry["commit"])
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("%s (rewritten)" % short(tip), out)
            self.assertIn("git branch -D " + name, out)
            self.assert_landed(fork, name, tip)

        def test_a_rewritten_ship_is_found_behind_a_commit_that_landed_first(self):
            """Another commit reached the trunk between the push and the merge: tree
            equality would say "not landed"; the patch is still there."""
            fork, name, entry = self.shipped()
            other = second_clone_commit(self.tmp)
            tip = self.human(("cherry-pick", "origin/" + name))
            self.assertEqual(self.parent_of(tip), other)
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("(rewritten)", out)
            self.assert_landed(fork, name, tip)
            self.assertTrue(os.path.exists(os.path.join(fork, "ours", "other.txt")))

        def test_a_ship_squashed_by_a_human_lands_by_its_patch(self):
            fork, name, entry = self.shipped()
            tip = self.human(("merge", "--squash", "origin/" + name),
                             ("commit", "-q", "-m", "squashed by hand"))
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("(rewritten)", out)
            self.assert_landed(fork, name, tip)

        def test_a_ship_merged_as_a_merge_commit_lands_with_the_rule_5_warning(self):
            fork, name, entry = self.shipped()
            tip = self.human(("merge", "--no-ff", "-q", "-m", "Merge " + name, "origin/" + name))
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("(ancestor)", out)
            self.assertIn("WARNING", out)
            self.assertIn("merged as a merge commit", out)
            self.assert_landed(fork, name, tip)

        def test_a_local_trunk_with_its_own_commit_is_exit_2_and_untouched(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "checkout", "-q", "develop", cwd=fork)
            own = commit_fork(fork, "ours/local.txt", "local\n", "local: by hand")
            sh("git", "checkout", "-q", name, cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("commits origin lacks", err)
            self.assertEqual(rev(fork, "refs/heads/develop"), own)
            self.assertEqual(checked_out(fork), name)
            self.assertEqual(rev(fork, "refs/heads/" + name), entry["commit"])
            self.assertEqual(self.pending_of(fork), entry)

        def test_a_missing_local_trunk_is_created_from_origin(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "branch", "-D", "develop", cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("git branch --no-track develop origin/develop", out)
            self.assert_landed(fork, name, entry["commit"])

        def test_land_from_an_unrelated_branch_ends_on_the_trunk(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "checkout", "-q", "-b", "other", "develop", cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assert_landed(fork, name, entry["commit"])
            self.assertEqual(rev(fork, "refs/heads/other"), entry["base"])   # left alone

        def test_land_twice_is_nothing_pending(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("nothing pending", err)

        # -- --force and --dry-run ---------------------------------------------------------

        def test_force_fast_forwards_an_unverified_landing_and_keeps_the_branch(self):
            """The MR was closed instead of merged and somebody else's commit landed: the
            trunk follows origin, the branch stays (nothing proved it landed), the record
            is cleared - the escape hatch, said out loud."""
            fork, name, entry = self.shipped()
            entry = self.with_mr(fork, entry)
            other = second_clone_commit(self.tmp)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)                      # without --force: no
            self.assert_untouched(fork, name, entry, on=name)
            code, out, err = self.land(fork, "--force")
            self.assertEqual(code, 0, err + out)
            self.assertIn("not verified (--force)", out)
            self.assertIn("`%s` is kept" % name, out)
            self.assertEqual(rev(fork, "refs/heads/develop"), other)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(rev(fork, "refs/heads/" + name), entry["commit"])   # kept
            self.assertEqual(self.pending_of(fork), {})

        def test_force_deletes_the_branch_when_the_landing_is_verified(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            code, out, err = self.land(fork, "--force")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn("not verified", out)
            self.assert_landed(fork, name, entry["commit"])

        def test_a_dry_run_moves_nothing_and_keeps_the_record(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            code, out, err = self.land(fork, "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would: checkout  $ git checkout develop", out)
            self.assertIn("would: trunk  $ git merge --ff-only origin/develop", out)
            self.assertIn("would: branch  $ git branch -d " + name, out)
            self.assertIn("would: landed", out)
            self.assertEqual(rev(fork, "refs/remotes/origin/develop"), entry["commit"])  # fetched
            self.assert_untouched(fork, name, entry, on=name)

    class TestMergeLands(MergeBase):
        """`--merge` ends by running `land` in the same process: the trunk is
        fast-forwarded locally, you are left on it, the branch is gone, the record cleared -
        on both platforms and for both kinds. A merge that succeeded and a catch-up that
        then could not run is exit 2 opening with "the merge request was merged"."""

        def assert_landed(self, fork: str, name: str, tip: str) -> None:
            self.assertEqual(rev(fork, "refs/heads/develop"), tip)
            self.assertEqual(origin_sha(fork, "develop"), tip)
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), "develop")
            self.assertEqual(rev(fork, "refs/heads/" + name), "")
            self.assertEqual(self.pending_of(fork), {})
            self.assertEqual(sh("git", "status", "--porcelain", cwd=fork), "")

        def test_gitlab_ship_lands_by_fast_forward(self):
            fork = self.self_fork()
            name = self.feature(fork, commits=2)
            old = rev(fork, "refs/heads/develop")
            with self.on(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            shipped = origin_sha(fork, name)
            self.assertIn("merged", out)
            self.assertIn("(ancestor)", out)
            self.assertIn("landed: develop %s..%s" % (short(old), short(shipped)), out)
            self.assertNotIn("may still exist", out)                 # glab removed it
            self.assert_landed(fork, name, shipped)

        def test_gitlab_sync_lands_its_merge_commit(self):
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            with self.on(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertIn("unless --merge lands it", out)
            self.assert_landed(fork, name, origin_sha(fork, name))
            self.assertEqual(rev(fork, "refs/heads/main"), rev(fork, "upstream/main"))

        def test_github_ship_lands_its_rebased_copy(self):
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            with self.on(self.platform("github")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            tip = origin_sha(fork, "develop")
            self.assertNotIn(tip, (base, origin_sha(fork, name)))     # rewritten by the platform
            self.assertIn("(rewritten)", out)
            self.assertIn("git branch -D " + name, out)
            self.assertIn("git push origin --delete " + name, out)    # gh leaves the remote branch
            self.assert_landed(fork, name, tip)

        def test_github_sync_lands_its_merge_commit(self):
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            with self.on(self.platform("github")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertIn("(ancestor)", out)
            self.assertNotIn("may still exist", out)                 # a sync: no hint
            self.assert_landed(fork, name, origin_sha(fork, name))

        def test_a_merged_request_whose_catch_up_cannot_run_says_so_first(self):
            """The local trunk carries a commit of its own: the platform merged the request,
            `land` refuses to move the trunk, and the exit says both - merged, not landed."""
            fork = self.self_fork()
            name = self.feature(fork)
            sh("git", "checkout", "-q", "develop", cwd=fork)
            own = commit_fork(fork, "ours/local.txt", "local\n", "local: by hand")
            sh("git", "checkout", "-q", name, cwd=fork)
            with self.on(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertTrue(err.startswith("forkflow: the merge request was merged; "
                                           "the local catch-up did not run: "), err)
            self.assertIn("commits origin lacks", err)
            shipped = origin_sha(fork, name)
            self.assertEqual(origin_sha(fork, "develop"), shipped)   # merged ...
            self.assertEqual(rev(fork, "refs/heads/develop"), own)   # ... not landed
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), name)
            self.assertEqual(rev(fork, "refs/heads/" + name), shipped)
            entry = self.pending_of(fork)                            # `land` can run later
            self.assertEqual((entry["kind"], entry["branch"], entry["commit"]),
                             ("ship", name, shipped))
            self.assertEqual(entry["mr"], MERGING_TOOL_URL["glab"])

    # ------------------------------------------------------------------- #
    # argument parsing and main
    # ------------------------------------------------------------------- #

    class TestMergeGate(Base):
        """`--merge` is config AND flag, refused before the fetch, the backup and any push.

        A reviewed fork must never be merged by accident: the flag alone does nothing, and
        the refusal comes before anything the run would have to undo - `origin/develop`, the
        mirror, the backup branches, the sync branch, the feature branch on origin and the
        state file are all exactly as they were. The fixture's origin is a local path, so
        the second condition (the fork can be named) needs the platform supplied."""

        REFUSED = "--merge needs"
        UNNAMED = "names no project"
        FORK = "ssh://gitlab.example.com/acme/team/widget"

        def named(self):
            return mock.patch.multiple(sys.modules[__name__],
                                       detect_platform=lambda url: "gitlab",
                                       mr_target=lambda ctx: (self.FORK, ""))

        def feature(self, fork: str) -> str:
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            commit_fork(fork, "ours/f0.txt", "line 0\n", "ours: step 0")
            return "feat/x"

        def refused(self, fork: str, *argv: str, why: str = REFUSED) -> None:
            before = (origin_sha(fork, "develop"), origin_sha(fork, "main"),
                      rev(fork, "refs/heads/main"), local_branches(fork))
            code, out, err = run("-C", fork, *argv)
            self.assertEqual(code, 2, " ".join(argv) + ": " + err + out)
            self.assertIn(why, err)
            self.assertEqual((origin_sha(fork, "develop"), origin_sha(fork, "main"),
                              rev(fork, "refs/heads/main"), local_branches(fork)), before)
            self.assertEqual(sh("git", "ls-remote", "--heads", "origin",
                                "refs/heads/" + DEFAULT_BACKUP_PREFIX + "*", cwd=fork), "")
            self.assertEqual(origin_sha(fork, sync_branch_name()), "")
            self.assertEqual(origin_sha(fork, "feat/x"), "")
            self.assertFalse(os.path.exists(git_path(fork, STATE_FILE)))

        def both_refused(self, fork: str, why: str = REFUSED) -> None:
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            self.refused(fork, "sync", "--merge", why=why)
            self.feature(fork)
            self.refused(fork, "ship", "--merge", why=why)

        @needs_tomllib
        def test_refused_on_a_manual_fork_with_the_config_committed(self):
            self.both_refused(make_fork(self.tmp, config='merge = "manual"\n'))

        @needs_tomllib
        def test_refused_on_a_manual_fork_with_the_config_untracked(self):
            """`setup` leaves `.forkflow.toml` untracked, and `load_config` reads the
            working tree: the gate must read it there too."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "manual"\n')
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            self.both_refused(fork)

        def test_refused_with_no_config_at_all(self):
            """No file means "manual": the default is the safe side."""
            self.both_refused(make_fork(self.tmp))

        def test_refused_on_continue_too(self):
            """`cmd_sync` dispatches `--continue` before any preflight, and a conflicted sync
            is exactly when `--continue` is used: the gate has to sit in front of it."""
            fork = make_fork(self.tmp)
            self.refused(fork, "sync", "--continue", "--merge")
            self.feature(fork)
            self.refused(fork, "ship", "--continue", "--merge")

        @needs_tomllib
        def test_refused_when_the_origin_names_no_project(self):
            """Knowable at gate time, so not a push followed by a failed merge: the fixture's
            local-path origin is on no platform and cannot be given to `--repo`."""
            self.both_refused(make_fork(self.tmp, config='merge = "self"\n'), why=self.UNNAMED)

        @needs_tomllib
        def test_passes_on_a_self_fork_whose_origin_is_named(self):
            """Both conditions met: the run goes on (a dry run here, which pushes nothing
            and reaches the merge-request step)."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            with self.named():
                code, out, err = run("-C", fork, "sync", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn(self.REFUSED, err)
            self.feature(fork)
            with self.named():
                code, out, err = run("-C", fork, "ship", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn(self.REFUSED, err)
            self.assertIn("not run (dry run)", out)              # `--merge` implied `--mr`

    class TestParseArgs(unittest.TestCase):
        def test_common_flags_before_or_after_the_subcommand(self):
            for argv in (["--dry-run", "sync"], ["sync", "--dry-run"]):
                self.assertTrue(parse_args(argv).dry_run, argv)
            for argv in (["-C", "/x", "status"], ["status", "-C", "/x"]):
                self.assertEqual(parse_args(argv).dir, "/x", argv)
            for argv in (["--force", "setup"], ["setup", "--force"]):
                self.assertTrue(parse_args(argv).force, argv)

        def test_defaults(self):
            args = parse_args(["status"])
            self.assertEqual((args.dir, args.dry_run, args.force), (".", False, False))
            self.assertFalse(args.fetch)

        def test_subcommand_flags(self):
            self.assertTrue(parse_args(["sync", "--continue"]).cont)
            self.assertTrue(parse_args(["ship", "--continue"]).cont)
            self.assertEqual(parse_args(["ship", "--message-file", "m.txt"]).message_file, "m.txt")
            self.assertEqual(parse_args(["setup", "--upstream-url", "U"]).upstream_url, "U")
            for cmd in ("sync", "ship"):
                self.assertTrue(parse_args([cmd, "--mr"]).mr, cmd)
                self.assertEqual(parse_args([cmd, "--title", "t"]).title, "t", cmd)
                self.assertFalse(parse_args([cmd]).mr, cmd)
                self.assertIsNone(parse_args([cmd]).title, cmd)

        def test_merge_implies_mr_and_not_the_other_way_round(self):
            """Merging a merge request means opening it first; opening one never means
            merging it."""
            for cmd in ("sync", "ship"):
                args = parse_args([cmd, "--merge"])
                self.assertTrue(args.merge, cmd)
                self.assertTrue(args.mr, cmd)
                args = parse_args([cmd, "--mr"])
                self.assertTrue(args.mr, cmd)
                self.assertFalse(args.merge, cmd)
                self.assertFalse(parse_args([cmd]).merge, cmd)
            for argv in (["--dry-run", "ship", "--merge"], ["ship", "--merge", "--dry-run"]):
                args = parse_args(argv)
                self.assertTrue(args.mr and args.dry_run, argv)

        def test_land_takes_the_common_flags_and_nothing_else(self):
            self.assertIs(COMMANDS["land"], cmd_land)
            for argv in (["land", "--force"], ["--force", "land"]):
                args = parse_args(argv)
                self.assertEqual((args.cmd, args.force, args.dry_run), ("land", True, False), argv)
            args = parse_args(["land", "--dry-run", "-C", "/x"])
            self.assertEqual((args.force, args.dry_run, args.dir), (False, True, "/x"))
            with self.assertRaises(SystemExit):        # `--merge` belongs to sync and ship
                capture(parse_args, ["land", "--merge"])

        def test_no_subcommand_is_exit_2(self):
            code, _, err = capture(main, [])
            self.assertEqual(code, 2)
            self.assertIn("no subcommand", err)

    class TestMainWiring(Base):
        def test_flag_order_gives_identical_output(self):
            fork = make_fork(self.tmp)

            def norm(text):
                return [ln for ln in text.splitlines() if "as of last fetch" not in ln]

            code_a, out_a, err_a = run("-C", fork, "status", "--dry-run")
            code_b, out_b, err_b = run("status", "--dry-run", "-C", fork)
            self.assertEqual((code_a, code_b), (0, 0), err_a + err_b)
            self.assertEqual(norm(out_a), norm(out_b))
            self.assertIn("forkflow status", out_a)

        def test_status_header_mentions_both_branches(self):
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertIn("mirror  main", out)
            self.assertIn("trunk   develop", out)
            self.assertIn("platform=unknown", out)

        def test_an_interrupt_is_exit_130(self):
            def interrupted(_args):
                raise KeyboardInterrupt

            fork = make_fork(self.tmp)
            with mock.patch.dict(COMMANDS, {"status": interrupted}):
                code, _, _ = run("-C", fork, "status")
            self.assertEqual(code, 130)

    class TestSourceInvariants(unittest.TestCase):
        """The rules read off the source itself.

        The helpers are unit-tested for what they refuse; this proves nothing *else* in the
        script pushes, rebases or moves the mirror behind their backs - the mistake the whole
        workflow exists to make impossible. Only the code above `run_tests` is examined."""

        @classmethod
        def setUpClass(cls):
            import ast
            with open(os.path.abspath(__file__), encoding="utf-8") as fh:
                src = fh.read()
            tree = ast.parse(src)
            cls.limit = next(n.lineno for n in tree.body
                             if isinstance(n, ast.FunctionDef) and n.name == "run_tests")
            funcs = [n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.lineno < cls.limit]
            funcs.sort(key=lambda n: n.end_lineno - n.lineno, reverse=True)
            cls.owner = {}
            for n in funcs:                      # innermost def wins
                for ln in range(n.lineno, n.end_lineno + 1):
                    cls.owner[ln] = n.name
            cls.lines = src.splitlines()

        def owners(self, needle: str) -> set:
            """Names of the functions whose code contains `needle` (prose is not code).

            Module-level code - constants, the hook template - answers as `<module>`, so a git
            argument hidden outside every `def` cannot slip past these invariants."""
            return {self.owner.get(n, "<module>") for n, line in enumerate(self.lines, 1)
                    if n < self.limit and needle in line}

        def test_only_the_three_helpers_push(self):
            self.assertEqual(self.owners('"push"'), {"push", "push_mirror", "bootstrap_trunk"})

        def test_the_argv_matcher_has_no_blind_spot(self):
            """The invariants read double-quoted argv literals. Nothing may spell a git
            argument another way and slip past them."""
            self.assertEqual(self.owners("git('"), set())          # single-quoted argv
            self.assertEqual(self.owners("git_rc('"), set())
            self.assertEqual(self.owners("'push'"), set())
            self.assertEqual(self.owners('"-f"'), set())           # the short --force
            self.assertEqual(self.owners('"-u"'), {"push"})        # only `push -u`, nothing else

        def test_no_force_push_and_no_hook_bypass(self):
            self.assertEqual(self.owners('"--force"'), {"parse_args"})   # the CLI flag, not git's
            self.assertEqual(self.owners("--no-verify"), set())
            # `<module>` is the docstring, where rule 4 is stated
            self.assertEqual(self.owners("--force-with-lease"), {"push", "<module>"})

        def test_only_advance_mirror_moves_the_mirror(self):
            self.assertEqual(self.owners('"update-ref"'), {"advance_mirror"})

        def test_the_two_fast_forwards_are_the_mirror_and_the_trunk_landing(self):
            """`land_trunk` joined `advance_mirror` as an owner of `merge --ff-only` when
            `land` came: the local trunk moves only by fast-forward, only there, and never
            by a ref move (`git branch -f` stays blocked, `update-ref` stays the mirror's)."""
            self.assertEqual(self.owners('"merge", "--ff-only"'), {"advance_mirror", "land_trunk"})

        def test_the_only_rebase_is_of_a_feature_branch(self):
            self.assertEqual(self.owners('"rebase"'), {"rebase_onto"})

        def test_git_and_the_platform_tools_are_the_only_subprocesses(self):
            """`run_tool` took over the platform-tool call from `open_mr` when the merge
            step (`merge_mr`) came to share it: a rename of the one owner, not a widening
            of the set."""
            self.assertEqual(self.owners("subprocess"),      # `<module>` is the import
                             {"git", "git_ok", "git_rc", "shell", "run_tool", "api_get",
                              "<module>"})

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    # every TestCase defined above, in definition order: a new class is registered by existing,
    # not by being remembered in a list here
    cases = [obj for obj in list(locals().values())
             if isinstance(obj, type) and issubclass(obj, unittest.TestCase)]
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    sys.exit(main())
