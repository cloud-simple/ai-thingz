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
    forkflow.py status [--fetch] [-C DIR]
    forkflow.py check [-C DIR]
    forkflow.py sync [--continue] [--mr] [--title T] [-C DIR] [--dry-run] [--force]
    forkflow.py ship [--continue] [--mr] [--title T] [--message-file F] [-C DIR] [--dry-run] [--force]
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
from dataclasses import dataclass, field
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
PUBLISHED_KEEP = 100                 # remembered (branch, commit) pushes - see record_published

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
                  "sync_prefix", "backup_prefix")


def have_tomllib() -> bool:
    """Whether `.forkflow.toml` can be read at all - Python 3.11+ (or a backport on the path)."""
    try:
        import tomllib  # noqa: F401   Python 3.11+
    except ImportError:
        return False
    return True


def configures_nothing(path: str) -> bool:
    """True when the file holds nothing but blank lines and `#` comments."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return False
    return all(not line.strip() or line.strip().startswith("#") for line in text.splitlines())


def load_config(root: str) -> dict:
    """{} when absent. A config that exists but cannot be read - or carries a value of the
    wrong type - is a hard failure: it names the branches every safety check depends on."""
    path = os.path.join(root, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    if not have_tomllib():
        # a file of nothing but comments configures nothing: reading it as `{}` is exactly what
        # tomllib would have said, and refusing it would brick every subcommand over the
        # commented template `setup` used to leave behind
        if configures_nothing(path):
            return {}
        raise Fail(f"{CONFIG_FILE} needs Python 3.11+ (tomllib) to be read; "
                   f"this is Python {sys.version_info[0]}.{sys.version_info[1]}. "
                   f"Remove the file or run forkflow with a newer Python.")
    import tomllib  # Python 3.11+, guarded by have_tomllib() above
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise Fail(f"{CONFIG_FILE}: {exc}")
    except OSError as exc:
        raise Fail(f"{CONFIG_FILE} cannot be read: {exc}")
    # every value ends up in a git command line: a wrong type is a traceback, not a workflow
    for key in CONFIG_STRINGS:
        if key in cfg and not isinstance(cfg[key], str):
            raise Fail(f"{CONFIG_FILE}: `{key}` must be a string, "
                       f"not {type(cfg[key]).__name__}")
    gate = cfg.get("gate")
    if gate is not None and (not isinstance(gate, list)
                             or any(not isinstance(c, str) for c in gate)):
        raise Fail(f"{CONFIG_FILE}: `gate` must be a list of shell commands")
    return cfg


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


def repo_id(url: str) -> str:
    """`host/path` of a git URL, so two spellings of one repository compare equal.

    `https://git@host/o/r.git`, `ssh://host/o/r` and `git@host:o/r.git` are the same project;
    a local path is left as it is, case included, because a path is not case-folded the way a
    host name is."""
    u = (url or "").strip().rstrip("/")
    host = False
    if "://" in u:
        u, host = u.split("://", 1)[1], True
    elif ":" in u and "/" not in u.split(":", 1)[0]:      # scp-like [user@]host:path
        head, _, path = u.partition(":")
        u, host = head + "/" + path.lstrip("/"), True
    if host:
        first, _, rest = u.partition("/")
        u = first.split("@")[-1].lower() + ("/" + rest if rest else "")
    return u[:-4] if u.endswith(".git") else u


def origin_pushes_to_upstream(root: str, origin: str, upstream: str) -> Optional[str]:
    """The origin push URL that reaches the original project, or None.

    `git push origin` sends to `remote.origin.pushurl` when there is one, and nothing else in
    this script looks at that key: an origin whose push URL is the upstream repository turns
    every `push()` here - and the trunk bootstrap `setup` runs before the hook exists - into a
    push to the project we must never write to (rule 1)."""
    theirs = {repo_id(u) for u in push_urls(root, upstream)}
    theirs.add(repo_id(git("remote", "get-url", upstream, cwd=root, check=False)))
    theirs.discard("")
    theirs.discard(repo_id("DISABLED"))     # what `setup` sets, and not a repository
    for url in push_urls(root, origin):
        if repo_id(url) in theirs:
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
              sync_prefix=sync_prefix, backup_prefix=backup_prefix)
    ctx.platform = detect_platform(ctx.origin_url)

    local_mirror = has_ref(root, f"refs/heads/{ctx.mirror}")
    remote_mirror = has_ref(root, f"refs/remotes/{ctx.origin}/{ctx.mirror}")
    if strict_mirror and not (local_mirror or remote_mirror):
        raise Fail(f"mirror `{mirror}` exists neither locally nor on origin - run `forkflow setup`")
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
    path = git_path(ctx.root, "FETCH_HEAD")
    if not path or not os.path.exists(path):
        return "never"
    return _rel_age(max(0.0, time.time() - os.path.getmtime(path)))


def plus_minus(ctx: Ctx, a: str, b: str) -> str:
    """`+<ahead>/-<behind>` of a against b - the header's two count cells."""
    ahead, behind = ahead_behind(ctx, a, b)
    return f"+{ahead}/-{behind}"


def header(ctx: Ctx, sub: str) -> None:
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


def mirror_worktree(ctx: Ctx) -> str:
    """Path of the worktree that has the mirror checked out, "" when none."""
    rc, out, _ = git_rc("for-each-ref", "--format=%(worktreepath)",
                        f"refs/heads/{ctx.mirror}", cwd=ctx.root)
    if rc != 0:                     # git without %(worktreepath): only this worktree is visible
        cur = git("symbolic-ref", "-q", "--short", "HEAD", cwd=ctx.root, check=False)
        return ctx.root if cur == ctx.mirror else ""
    return out.strip()


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


def mr_command(ctx: Ctx, branch: str, title: str, body_file: str) -> list:
    """The platform's MR command, or [] when the platform is unknown."""
    if ctx.platform == "gitlab":
        return ["glab", "mr", "create",
                "--source-branch", branch, "--target-branch", ctx.trunk,
                "--title", title, "--description-file", body_file, "--remove-source-branch"]
    if ctx.platform == "github":
        return ["gh", "pr", "create",
                "--head", branch, "--base", ctx.trunk,
                "--title", title, "--body-file", body_file]
    return []


def open_mr(ctx: Ctx, branch: str, title: str, body: str, run_it: bool) -> str:
    """Print the MR command and, with --mr, run it. Never a failure: the branch is pushed,
    the merge request is the only thing left to do."""
    path = "<description file>" if ctx.dry_run else write_temp(body, "mr-body.md")
    cmd = mr_command(ctx, branch, title, path)
    if not cmd:
        step("mr", f"# platform {ctx.platform}",
             f"open the merge request manually: {branch} -> {ctx.trunk}", dry=ctx.dry_run)
        print(f"    title: {title}")
        if not ctx.dry_run:
            print(f"    description: {path}")
        return ""
    shown = " ".join(shlex.quote(c) for c in cmd)
    if ctx.dry_run:
        step("mr", shown, "not run (dry run)", dry=True)
        return shown
    if not run_it:
        step("mr", shown, "not run (add --mr to run it)")
        print(f"    description: {path}")
        return shown
    try:
        p = subprocess.run(cmd, cwd=ctx.root, capture_output=True)
    except OSError as exc:
        step("mr", shown, f"{cmd[0]} unavailable ({exc.strerror or exc})")
        print(f"    description: {path}")
        return shown
    out = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        step("mr", shown, f"FAILED (exit {p.returncode}) - open it yourself")
        for line in tail_lines(p.stderr.decode("utf-8", "replace"), TAIL_LINES):
            print(f"      {line}")
        print(f"    description: {path}")
        return shown
    step("mr", shown, "created")
    for line in out.splitlines():
        print(f"    {line}")
    try:
        os.unlink(path)          # the description is on the platform now; nobody needs the file
    except OSError:
        pass
    return shown


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_status(args: argparse.Namespace) -> int:
    """Read-only. Without --fetch it makes no network call and writes nothing."""
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=False, strict_mirror=False)
    fetch_step = None
    if getattr(args, "fetch", False):
        rc, cmd, result, err = fetch(ctx, (ctx.origin, ctx.upstream))
        tail = err.strip().splitlines()[-1] if err.strip() else "see git output"
        fetch_step = (cmd, result if rc == 0
                      else f"FAILED, reporting the refs on disk: {tail}")

    header(ctx, "status")
    if fetch_step:
        step("fetch", fetch_step[0], fetch_step[1])

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


def run_check(ctx: Ctx, touched: Optional[Sequence[str]] = None) -> int:
    """The preflight `sync` and `ship` run, and what `status` surfaces for humans.

    Read-only. 0 when every invariant holds, 3 when one does not; the upstream-tracked
    warning is advisory and never changes the code."""
    failures = []
    warn_upstream_tracked(ctx, touched)

    gate = gate_commands(ctx)
    if not gate:
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
        raise Fail(f"merge of {short(target)} failed:\n{(err or out).strip()}")
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
                backup_ref: str) -> int:
    """check -> push -> merge request: the tail both `sync` and `sync --continue` run."""
    if ctx.dry_run:
        step("check", "forkflow check", "not run (dry run)", dry=True)
    elif run_check(ctx) == 3:
        print(check_failure_hint(ctx, name))
        return 3

    try:
        push(ctx, name)
    except Fail as exc:
        if exc.code == 5:
            print("  fix that, then: forkflow sync --continue")
        raise

    title = (getattr(args, "title", None)
             or f"sync: {ctx.up()} {utc_stamp()} ({len(commits)} commits)")
    open_mr(ctx, name, title, sync_body(ctx, commits, rows, mirror_move, backup_ref),
            bool(getattr(args, "mr", False)))
    write_state(ctx, "sync", None)                     # this sync is done: nothing to resume
    print(f"  after the MR is merged: git fetch {sh_arg(ctx.origin)} "
          f"&& git switch {sh_arg(ctx.trunk)} "
          f"&& git merge --ff-only {sh_arg(f'{ctx.origin}/{ctx.trunk}')}")
    return 0


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
    # top of it, and that must not turn `--continue` into "nothing to continue"
    merge_sha = git("rev-list", "--merges", "-n", "1", "HEAD", "--not",
                    f"{ctx.origin}/{ctx.trunk}", cwd=ctx.root, check=False)
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
        step("continue", "git rev-list --merges -n 1 HEAD",
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
                       resumable(ctx, "sync", name).get("backup", ""))


def cmd_sync(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=True)
    header(ctx, "sync")
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
        rows = []
    else:
        rows = both_sides_survived(ctx, "HEAD^1", "HEAD^2")
    return finish_sync(ctx, args, name, commits, rows, mirror_move, backup_ref)


def rebase_in_progress(ctx: Ctx) -> bool:
    """True while `git rebase` is stopped on a conflict or an edit."""
    for name in ("rebase-merge", "rebase-apply"):
        path = git_path(ctx.root, name)
        if path and os.path.exists(path):
            return True
    return False


def ship_preflight(ctx: Ctx) -> str:
    """The branch `ship` may rewrite, or Fail(2). Everything else is refused by name:
    ship rebases and squashes what it is on, and only a feature branch may be rewritten."""
    if rebase_in_progress(ctx):
        raise Fail("a rebase is in progress: finish it with `git rebase --continue` and then "
                   "`forkflow ship --continue`, or start over with `git rebase --abort`")
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

    try:
        push(ctx, branch, lease=lease or None, backup_ref=backup_ref)
    except Fail as exc:
        if exc.code == 5 and rollback:
            print(rollback)
        raise

    title = (getattr(args, "title", None)
             or (message.strip().splitlines() or [f"ship {branch}"])[0])
    open_mr(ctx, branch, title, ship_body(ctx, message, touched),
            bool(getattr(args, "mr", False)))
    write_state(ctx, "ship", None)                     # this ship is done: nothing to resume
    print(f"  after the MR is merged: git fetch {sh_arg(ctx.origin)} "
          f"&& git switch {sh_arg(ctx.trunk)} "
          f"&& git merge --ff-only {sh_arg(f'{ctx.origin}/{ctx.trunk}')}")
    if ctx.platform == "github":
        print(f"    then delete the local `{branch}`: "
              f'"Rebase and merge" rewrites the commit, so the local branch is a stale copy')
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


def setup_mirror(ctx: Ctx, target: str) -> None:
    """Check the mirror, or create it in a single-branch clone. It is never reset:
    a `<mirror>` that carries work is a migration, and that is done by hand."""
    m = ctx.mirror
    local = has_ref(ctx.root, f"refs/heads/{m}")
    remote = has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{m}")
    if not (local or remote):
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


# The hook is the second half of the guarantee: the script routes every push through the three
# helpers, and this refuses the forbidden pushes even when git is driven by hand. Every name and
# the upstream URL are baked in at install time, so the hook needs no config of its own.
HOOK_TEMPLATE = """#!/bin/sh
%(mark)s - written by `forkflow setup`; `forkflow setup --force` replaces it.
#
# Refuses the pushes the workflow forbids, however git is driven:
#   - anything to the original project (remote `%(upstream)s` or its URL)
#   - anything to the trunk `%(trunk)s`, deletion included: merge requests only
#   - any push of the mirror `%(mirror)s` that is not a pure copy of upstream
#
# The mirror check validates against the last fetch of `%(upstream)s` (%(up_ref)s);
# fetch before pushing the mirror.

up_remote=%(upstream_q)s
up_url=%(upstream_url_q)s
up_ref=%(up_ref_q)s
trunk_ref=%(trunk_ref_q)s
mirror_ref=%(mirror_ref_q)s
zero='0000000000000000000000000000000000000000'

to_upstream=no
[ "$1" = "$up_remote" ] && to_upstream=yes
[ -n "$up_url" ] && [ "$2" = "$up_url" ] && to_upstream=yes
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
            if [ $? -eq 1 ]; then
                echo "forkflow: the mirror only moves forward (rule 6) - $remote_sha is not in $local_sha" >&2
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


def hook_text(ctx: Ctx) -> str:
    """The pre-push hook for this fork - names and upstream URL substituted in.

    Every value the shell reads is quoted: the names come from `.forkflow.toml`, a tracked
    file a sync can bring in from upstream, and this text is run by `sh` on every push."""
    up_ref = f"refs/remotes/{ctx.upstream}/{ctx.upstream_branch}"
    return HOOK_TEMPLATE % {
        "mark": HOOK_MARK,
        "upstream": ctx.upstream,
        "upstream_q": sh_quote(ctx.upstream),
        "upstream_url_q": sh_quote(ctx.upstream_url),
        "up_ref": up_ref,
        "up_ref_q": sh_quote(up_ref),
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
    print(f"    it refuses every push to `{ctx.upstream}` (by name or URL) and to `{ctx.trunk}`, "
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


def gitlab_protection(ctx: Ctx, role: str, branch: str) -> None:
    # the name is one path segment: `release/1.0` unencoded would address another endpoint
    path = f"projects/:fullpath/protected_branches/{urllib.parse.quote(branch, safe='')}"
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
    protect = (f"glab api --method POST projects/:fullpath/protected_branches "
               f"-f name={sh_quote(branch)} -F push_access_level=0 -F merge_access_level=40 "
               f"-F allow_force_push=false")
    if force is None:
        fix_cmd(protect)
        return
    if force:
        fix_cmd(f"glab api --method PATCH {path} -F allow_force_push=false")
    if direct and role == "trunk":
        finding("who may push can only be changed by recreating the rule (GitLab's PATCH takes "
                "`allowed_to_push` entries by id, not a level): this deletes it and creates it "
                "again, with merge requests merged by Maintainers")
        fix_cmd(f"glab api --method DELETE {path} && {protect}")


def gitlab_report(ctx: Ctx) -> None:
    path = "projects/:fullpath"
    reply = api_get(ctx, "glab", path)
    if not reply.ok():
        step("platform", f"glab api {path}", reply.note)
        return
    default = str(reply.data.get("default_branch") or "-")
    method = str(reply.data.get("merge_method") or "-")
    step("platform", f"glab api {path}",
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
        fix_cmd(f"glab api --method PUT {path} " + " ".join(fixes))
    gitlab_protection(ctx, "trunk", ctx.trunk)
    gitlab_protection(ctx, "mirror", ctx.mirror)


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


def github_protection(ctx: Ctx, role: str, branch: str) -> None:
    # concatenated, not an f-string: `{owner}`/`{repo}` are gh's own placeholders, which an
    # f-string would read as fields of its own
    path = ("repos/{owner}/{repo}/branches/"
            + urllib.parse.quote(branch, safe="") + "/protection")
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
        fix_cmd(f"echo {sh_quote(body)} | gh api -X PUT {path} --input -")
        return
    finding("the PUT below replaces the whole protection object: it carries over the "
            "settings the read above returned - check them before you run it")
    body = github_protection_body(reply.data, require_pr=bool(direct) and role == "trunk")
    fix_cmd(f"echo {sh_quote(body)} | gh api -X PUT {path} --input -")


def github_report(ctx: Ctx) -> None:
    path = "repos/{owner}/{repo}"
    reply = api_get(ctx, "gh", path)
    if not reply.ok():
        step("platform", f"gh api {path}", reply.note)
        return
    default = str(reply.data.get("default_branch") or "-")
    merge_commit = bool(reply.data.get("allow_merge_commit"))
    rebase = bool(reply.data.get("allow_rebase_merge"))
    step("platform", f"gh api {path}",
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
        fix_cmd(f"gh api -X PATCH {path} " + " ".join(fixes))
    github_protection(ctx, "trunk", ctx.trunk)
    github_protection(ctx, "mirror", ctx.mirror)


def platform_report(ctx: Ctx) -> None:
    """What the hosting platform has to say - reported, never changed. Runs in a dry run too:
    every call is a GET."""
    if ctx.platform == "gitlab":
        gitlab_report(ctx)
    elif ctx.platform == "github":
        github_report(ctx)
    else:
        step("platform", f"# origin {ctx.origin_url or '-'}",
             f"unknown host - check yourself that the default branch is `{ctx.trunk}`, that "
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
                             "foreign pre-push hook. No effect on status, check or ship")

    p = argparse.ArgumentParser(prog="forkflow", parents=[common],
                                description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="`forkflow.py --test` runs the embedded test suite.")
    sub = p.add_subparsers(dest="cmd", metavar="{status,check,sync,ship,setup}")

    s = sub.add_parser("status", parents=[common], help="where mirror, trunk and upstream stand")
    s.add_argument("--fetch", action="store_true", help="refresh remote-tracking refs first")

    sub.add_parser("check", parents=[common], help="preflight invariants (gate, trunk tip)")

    sy = sub.add_parser("sync", parents=[common], help="advance the mirror and merge it into the trunk")
    sy.add_argument("--continue", dest="cont", action="store_true",
                    help="resume after resolving merge conflicts")
    sy.add_argument("--mr", action="store_true", help="run the merge-request command")
    sy.add_argument("--title", help="merge-request title")

    sh = sub.add_parser("ship", parents=[common], help="squash a feature branch onto the trunk's tip")
    sh.add_argument("--continue", dest="cont", action="store_true",
                    help="resume after `git rebase --continue`")
    sh.add_argument("--mr", action="store_true", help="run the merge-request command")
    sh.add_argument("--title", help="merge-request title")
    sh.add_argument("--message-file", help="file with the squashed commit message")

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
            self.assertIn("exists neither locally nor on origin", str(cm.exception))
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
                for marker in ("rollback: ", "fix: ", "after the MR is merged: "):
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

            names, seen = (self.TRUNK, self.PREFIX), set()
            for argv in (("status", "--fetch"), ("check",), ("sync", "--dry-run"),
                         ("ship", "--dry-run"), ("setup", "--dry-run")):
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
            self.assertRegex(last_fetch(ctx), r"^\d+[smhd] ago$")
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

        def test_no_network_and_no_writes_without_fetch(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "src/app.py", "def main():\n    return 5\n")
            push_upstream_into_origin(self.tmp)
            before = sh("git", "for-each-ref", cwd=fork)
            code, out, err = run("-C", fork, "status")
            self.assertEqual(code, 0, err)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)
            self.assertNotIn("git fetch", out)

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
        def test_failing_gate_is_exit_3_with_the_continue_hint(self):
            fork = make_fork(self.tmp, config='gate = ["exit 2"]\n')
            self.ahead_upstream(fork)
            before_trunk = origin_sha(fork, "develop")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 3)
            self.assertIn("forkflow sync --continue", out)
            self.assertEqual(origin_sha(fork, self.sync_name()), "")   # nothing was pushed
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)

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

            # a second `ship` while the rebase is stopped refuses to do anything
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 2)
            self.assertIn("rebase is in progress", err)

            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"))
            sh("git", "add", "shared.tf", cwd=fork)
            sh("git", "rebase", "--continue", cwd=fork)

            code, out, err = run("-C", fork, "ship", "--continue")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "HEAD^"), before_trunk)
            self.assertEqual(origin_sha(fork, name), rev(fork, "HEAD"))
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            self.assertIn("count = 2", sh("git", "show", "HEAD:shared.tf", cwd=fork))

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

        def test_a_single_branch_clone_gets_a_local_mirror(self):
            make_fork(self.tmp)
            thin = os.path.join(self.tmp, "thin")
            sh("git", "clone", "--single-branch", "--branch", "develop",
               os.path.join(self.tmp, "origin.git"), thin)
            identity(thin)
            sh("git", "remote", "add", "upstream", os.path.join(self.tmp, "upstream.git"), cwd=thin)
            self.assertEqual(rev(thin, "refs/heads/main"), "")
            self.assertEqual(rev(thin, "refs/remotes/origin/main"), "")

            code, out, err = run("-C", thin, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(thin, "refs/heads/main"), rev(thin, "refs/remotes/upstream/main"))
            self.assertEqual(self.cfg(thin, "branch.main.remote"), "")        # --no-track

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
                ("projects/:fullpath", project[0], project[1]),
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

        def report(self, platform: str) -> str:
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = platform
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
                "glab api --method DELETE projects/:fullpath/protected_branches/develop && "
                "glab api --method POST projects/:fullpath/protected_branches "
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
                "api", "projects/:fullpath",
                "api", "projects/:fullpath/protected_branches/develop",
                "api", "projects/:fullpath/protected_branches/main"])

        def test_a_wrong_default_branch_and_merge_method_share_one_fix(self):
            self.gitlab_tool(project=('{"default_branch":"main","merge_method":"merge"}', 0))
            out = self.report("gitlab")
            self.assertIn("default branch: `main` - it must be the trunk `develop`", out)
            self.assertIn("merge method: `merge` -", out)
            self.assertEqual(self.fixes(out), [
                "glab api --method PUT projects/:fullpath "
                "-f default_branch='develop' -f merge_method=ff"])

        def test_an_unprotected_trunk_gets_a_post_and_the_mirror_only_advice(self):
            self.gitlab_tool(trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("gitlab")
            self.assertIn("trunk `develop`: NOT protected", out)
            self.assertIn("mirror `main`: not protected (advisory", out)
            self.assertEqual(self.fixes(out), [
                "glab api --method POST projects/:fullpath/protected_branches "
                "-f name='develop' -F push_access_level=0 -F merge_access_level=40 "
                "-F allow_force_push=false"])

        def test_force_push_allowed_gets_a_patch_on_either_branch(self):
            self.gitlab_tool(trunk=GL_FORCE, mirror=GL_FORCE)
            out = self.report("gitlab")
            self.assertIn("trunk `develop`: protected, but force-push is ALLOWED", out)
            self.assertIn("mirror `main`: protected, but force-push is ALLOWED", out)
            self.assertEqual(self.fixes(out), [
                "glab api --method PATCH projects/:fullpath/protected_branches/develop "
                "-F allow_force_push=false",
                "glab api --method PATCH projects/:fullpath/protected_branches/main "
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
            self.assertEqual(self.argv(), ["api", "projects/:fullpath"])

        def test_a_missing_tool_is_not_checked(self):
            ctx = ctx_for(make_fork(self.tmp))
            ctx.platform = "gitlab"
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
                "api", "repos/{owner}/{repo}",
                "api", "repos/{owner}/{repo}/branches/develop/protection",
                "api", "repos/{owner}/{repo}/branches/main/protection"])

        def test_disabled_merge_options_are_fixed_together_with_the_default_branch(self):
            self.github_tool(repo=('{"default_branch":"main","allow_merge_commit":false,'
                                   '"allow_rebase_merge":false}', 0))
            out = self.report("github")
            self.assertIn("merge commits: NOT allowed", out)
            self.assertIn("rebase merges: NOT allowed", out)
            self.assertEqual(self.fixes(out), [
                "gh api -X PATCH repos/{owner}/{repo} -f default_branch='develop' "
                "-F allow_merge_commit=true -F allow_rebase_merge=true"])

        def test_an_unprotected_trunk_gets_the_put_and_the_mirror_only_advice(self):
            self.github_tool(trunk=UNPROTECTED, mirror=UNPROTECTED)
            out = self.report("github")
            self.assertIn("trunk `develop`: NOT protected", out)
            self.assertIn("mirror `main`: not protected (advisory", out)
            body = github_protection_body({"enforce_admins": True}, require_pr=True)
            self.assertEqual(self.fixes(out), [
                "echo '%s' | gh api -X PUT "
                "repos/{owner}/{repo}/branches/develop/protection --input -" % body])
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

    class TestPlatformReportInSetup(PlatformBase):
        def as_gitlab(self):
            return mock.patch.object(sys.modules[__name__], "detect_platform",
                                     lambda url: "gitlab")

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
                "api", "projects/:fullpath",
                "api", "projects/:fullpath/protected_branches/develop",
                "api", "projects/:fullpath/protected_branches/main"])

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
                 "--source-branch", "sync/upstream-20260101",
                 "--target-branch", "develop",
                 "--title", "sync: title",
                 "--description-file", "/tmp/body.md",
                 "--remove-source-branch"])

        def test_github_command(self):
            ctx = self.ctx("https://github.com/owner/repo.git")
            self.assertEqual(
                mr_command(ctx, "feat/x", "ship: title", "/tmp/body.md"),
                ["gh", "pr", "create",
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

    class TestOpenMr(Base):
        def test_unknown_platform_prints_the_manual_note(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            shown, out, _ = capture(open_mr, ctx, "feat/x", "a title", "body\n", True)
            self.assertEqual(shown, "")
            self.assertIn("open the merge request manually: feat/x -> develop", out)
            self.assertIn("a title", out)

        def test_missing_tool_is_reported_and_never_raises(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            ctx.platform = "gitlab"
            missing = FileNotFoundError(2, "No such file or directory")
            with mock.patch.object(subprocess, "run", side_effect=missing):
                shown, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("glab mr create", shown)
            self.assertIn("glab unavailable", out)
            self.assertIn("No such file or directory", out)

        def temp_files(self) -> list:
            return sorted(f for f in os.listdir(tempfile.gettempdir())
                          if f.startswith("forkflow-"))

        def test_dry_run_writes_no_body_file(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork, dry_run=True)
            ctx.platform = "github"
            before = self.temp_files()
            shown, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("gh pr create", shown)
            self.assertIn("<description file>", shown)
            self.assertIn("not run (dry run)", out)
            self.assertEqual(self.temp_files(), before)      # nothing was written anywhere

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

        def as_gitlab(self):
            return mock.patch.object(sys.modules[__name__], "detect_platform",
                                     lambda url: "gitlab")

        def as_github(self):
            return mock.patch.object(sys.modules[__name__], "detect_platform",
                                     lambda url: "github")

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

    # ------------------------------------------------------------------- #
    # argument parsing and main
    # ------------------------------------------------------------------- #

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
            self.assertEqual(self.owners('"merge", "--ff-only"'), {"advance_mirror"})

        def test_the_only_rebase_is_of_a_feature_branch(self):
            self.assertEqual(self.owners('"rebase"'), {"rebase_onto"})

        def test_git_and_the_platform_tools_are_the_only_subprocesses(self):
            self.assertEqual(self.owners("subprocess"),      # `<module>` is the import
                             {"git", "git_ok", "git_rc", "shell", "open_mr", "api_get",
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
