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
    forkflow.py land [BRANCH] [--force] [-C DIR] [--dry-run]
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
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Optional, Sequence, Tuple

# The state file's lock is the operating system's (`lock_exclusive`). Neither module is on
# every platform, and forkflow refuses to write the state file rather than write it
# unserialised, so both are optional here and the absence of both is a refusal with a reason.
try:
    import fcntl                                  # POSIX
except ImportError:                               # pragma: no cover - not POSIX
    fcntl = None
try:
    import msvcrt                                 # Windows
except ImportError:                               # pragma: no cover - not Windows
    msvcrt = None

CONFIG_FILE = ".forkflow.toml"
STATE_FILE = "forkflow-state.json"   # in .git: ties a `--continue` run to the backup it belongs to
HOOK_MARK = "# forkflow pre-push hook"
DEFAULT_TRUNK = "develop"
DEFAULT_MIRROR = "main"
DEFAULT_SYNC_PREFIX = "sync/"
DEFAULT_BACKUP_PREFIX = "backup/"
README_POINTER = ("See *Adopting forkflow in an existing fork* in the project README "
                  "(github.com/cloud-simple/ai-thingz, section `forkflow`)")
TAIL_LINES = 12                      # last lines of a failed command shown (a gate, the MR tool)
MERGE_TREE_GIT = (2, 38)             # `git merge-tree --write-tree` - older git skips the simulation
BOTH_SIDES_SNIFF = 8192              # bytes of a blob looked at for a NUL before it is "binary"
BOTH_SIDES_WIDTH = 48                # column the both-sides table pads paths to
MAX_SYNC_RERUNS = 100                # `--force` reruns: `<name>-2` .. `<name>-99` before it gives up
DRY_PREFIX = "would: "               # the marker every line a --dry-run did not run carries
EXIT_PRECONDITION = 2                # the codes the module docstring's table publishes
EXIT_CHECK = 3
EXIT_CONFLICT = 4
EXIT_UNSAFE = 5
EXIT_INTERRUPTED = 130               # Ctrl-C, as the module docstring publishes it
EXIT_NOT_MERGED = 6                  # --merge: the branch is pushed, the merge request is
                                     # not merged (or not created) - all before it stands
PUBLISHED_KEEP = 100                 # remembered (branch, commit) pushes - see record_published
UPSTREAM_CONFIG_KEEP = 500           # remembered upstream `.forkflow.toml` digests. NOTHING
                                     # IS EVER DROPPED TO MAKE ROOM: the memory fills and
                                     # `--merge` refuses from then on. See
                                     # remember_upstream_configs
CONFIG_MEMORY_FULL = "upstream_configs_full"   # ... and the state key that says it did
RENDERED_CONFIGS = "rendered_configs"   # the state key holding the digest of every working-
                                     # tree config this clone has judged unprovable for a
                                     # RENDERING reason - see config_rendered_before
ATTRS_FILE = ".gitattributes"         # the only file in a tree that can render the config
CONFIG_RENDER_ATTRS = ("filter", "ident",            # the attributes that make the working
                       "working-tree-encoding")      # tree's config differ from the blob git
                                     # stores in a way nothing here normalises back - see
                                     # config_render_unprovable for why `text`, `eol` and
                                     # `diff` are NOT among them
STATE_LOCK_WAIT = 5.0                # seconds a run waits for another worktree's state write
STATE_LOCK_POLL = 0.02               # between tries while it waits
HELD_ELSEWHERE = frozenset(          # what the lock calls answer with while another run
    code for code in (getattr(errno, name, None)          # holds it - as against a
                      for name in ("EACCES", "EAGAIN", "EWOULDBLOCK",  # filesystem that
                                   "EINTR", "EDEADLK", "EDEADLOCK"))   # cannot lock at all
    if code is not None)

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
    def __init__(self, msg: str, code: int = EXIT_PRECONDITION):
        super().__init__(msg)
        self.code = code


def git(*args: str, cwd: Optional[str] = None, check: bool = True) -> str:
    """Run git; raise Fail(..., EXIT_PRECONDITION) on failure unless check=False
    (then return "")."""
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if p.returncode != 0:
        if check:
            raise Fail(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
        return ""
    return p.stdout.decode("utf-8", "replace").strip()


def git_ok(*args: str, cwd: Optional[str] = None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def git_rc(*args: str, cwd: Optional[str] = None,
           env: Optional[dict] = None) -> Tuple[int, str, str]:
    """(returncode, stdout, stderr) - for every call whose failure has its own exit code.

    `env` replaces this process's environment for the one call (`merge_tree` sends the
    objects git writes somewhere other than this repository)."""
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=env)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def merge_tree(root: str, *args: str) -> Tuple[int, str, str]:
    """`git merge-tree --write-tree ...` with the objects it writes sent somewhere other
    than this repository's object database.

    `--write-tree` is the only form modern git offers, and it writes the merged tree and a
    blob per conflicting file - a real write, inside a `--dry-run` that promises none and
    inside a `status` that only reports. Nothing needs those objects to survive the call:
    both callers read stdout and throw the tree away. So they go to a temporary directory
    (`GIT_OBJECT_DIRECTORY`) with this repository's own object database named as an
    alternate to read from (`GIT_ALTERNATE_OBJECT_DIRECTORIES` - git follows alternates
    transitively, so a clone that has its own still resolves everything), and the directory
    is removed afterwards. The answer is identical to the unredirected call's.

    When git cannot say where the objects live, the call is made as it always was: a
    simulation that does not run is worse than a few unreachable objects."""
    real = git_path(root, "objects")
    if not real:
        return git_rc("merge-tree", *args, cwd=root)
    alternates = os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
    env = dict(os.environ, GIT_OBJECT_DIRECTORY=tempfile.mkdtemp(prefix="forkflow-odb-"),
               GIT_ALTERNATE_OBJECT_DIRECTORIES=(real + os.pathsep + alternates
                                                 if alternates else real))
    try:
        return git_rc("merge-tree", *args, cwd=root, env=env)
    finally:
        shutil.rmtree(env["GIT_OBJECT_DIRECTORY"], ignore_errors=True)


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
MERGE_MANUAL = "manual"              # the default: a person merges, through the platform
MERGE_SELF = "self"                  # whoever opened the request merges it - what --merge needs
MERGE_MODES = (MERGE_MANUAL, MERGE_SELF)


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
                   f"Run forkflow with Python 3.11 or newer, or turn every line of the file "
                   f"into a `#` comment (a file of comments configures nothing, and its "
                   f"settings stay there to uncomment later).")
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


def config_name_in(names: Sequence[str]) -> Optional[str]:
    """The name among `names` that is `.forkflow.toml` under some spelling of its case:
    the exact name when it is there, else the first variant, else None."""
    if CONFIG_FILE in names:
        return CONFIG_FILE
    variants = sorted(n for n in names if n.casefold() == CONFIG_FILE.casefold())
    return variants[0] if variants else None


def tracked_config_names(root: str) -> list:
    """Every path in the index that is `.forkflow.toml` under some case of its name (each
    stage of an unmerged one once). Every tracked path is read and case-folded, rather
    than asked for with `:(icase)`, which matches ASCII case only."""
    rc, out, _ = git_rc("ls-files", "-z", cwd=root)
    fold = CONFIG_FILE.casefold()
    return sorted({p for p in out.split("\0") if p and p.casefold() == fold}) if rc == 0 else []


def remote_trunk(root: str) -> str:
    """The trunk's name where the config cannot be read to say it: origin's default branch
    (`origin/HEAD` - `setup`'s report insists it is the trunk), else the default name."""
    head = git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", cwd=root, check=False)
    prefix = "refs/remotes/origin/"
    return head[len(prefix):] if head.startswith(prefix) else DEFAULT_TRUNK


def upstream_branch_names(root: str) -> set:
    """The branch names the original project's remotes have here: `upstream/main` gives
    `main`. A local branch of that name is this fork's mirror - `setup` bootstraps it by
    that name - and the config that would say which branch the mirror is is the file that
    cannot be read when this is asked. A mirror under a name upstream does not use is not
    recognised here; the pre-push hook and `status` are what catch a commit on it."""
    names = set()
    out = git("for-each-ref", "--format=%(refname)", "refs/remotes/", cwd=root, check=False)
    for ref in out.splitlines():
        parts = ref.split("/", 4)                     # refs/remotes/<remote>/<branch...>
        if len(parts) >= 4 and parts[2] != "origin" and parts[3] != "HEAD":
            names.add("/".join(parts[3:]))
    return names


def no_commit_here(root: str, trunk: str) -> str:
    """Why nothing may be committed on the checked-out branch, "" when something may.

    Rules 2 and 6, and nothing besides: the trunk, by the name origin gives it and by the
    default, and the mirror, by its name (`upstream_branch_names`, and the default). A
    branch mistaken for one of those gets an explanation and no command, the safe
    direction; the one miss is a trunk or a mirror under a name nothing here gives.

    It used to answer for any branch contained in a remote-tracking ref that is not
    origin's - "carries nothing but upstream's commits". On a fresh fork every branch is
    still at upstream's tip, so `forkflow setup` - the first command a fork runs - and the
    branch this refusal sends the user to both hit that clause, and nothing anywhere named
    a fix: a dead end out of which only an unrelated commit led. Committing on a branch of
    one's own is what the workflow is for. Whose a FILE is, which that clause was also
    standing in for, is `config_is_upstreams`'s question, and its bytes answer it."""
    branch = git("symbolic-ref", "-q", "--short", "HEAD", cwd=root, check=False)
    if not branch:
        return "HEAD is detached"
    if branch == trunk:
        return f"`{branch}` is origin's default branch"
    if branch == DEFAULT_TRUNK:
        return f"`{branch}` is the trunk"
    if branch == DEFAULT_MIRROR or branch in upstream_branch_names(root):
        return f"`{branch}` is the mirror of the original project"
    return ""


def keep_aside(root: str, name: str) -> Tuple[str, str]:
    """(a printable command that copies the working file `name` aside, the path it copies
    it to) - the one place in this script that builds a copy for a user to run.

    Every remedy that overwrites, moves or deletes a working file begins with this, because
    a printed command is followed to the letter and the file may hold an edit of the user's
    that nothing else has. `test ! -e` makes a second run of the same line stop before it
    could copy a restored file over the saved one, and the timestamp keeps two runs in one
    second apart from... nothing, which is what `test ! -e` is there for.

    The destination is ALWAYS inside the git directory, and that is the point of having one
    owner: a remedy must never produce `.forkflow.toml` bytes. Twice now a remedy has ended
    by copying something back INTO the config's path - the contents a symlink read as, and
    a file git's own conversion had rewritten - and each time the bytes that landed there
    were bytes no `.forkflow.toml` blob ever held, so they read as this fork's own and
    opened `--merge` on the original project's settings. What a remedy may do is take the
    obstacle away and name where the old file went. What comes back is the user's to write.
    `test_every_copy_a_remedy_prints_is_built_here` pins both halves."""
    keep = git_path(root, f"forkflow-config-{utc_stamp('%Y%m%d-%H%M%S')}.toml")
    return (f"test ! -e {sh_arg(keep)} && cp -p -- {sh_arg(name)} {sh_arg(keep)}", keep)


def variant_remedy(root: str, found: str) -> str:
    """The way from `found`, a case variant of the config, to `.forkflow.toml` that loses
    nothing - the second half of `load_config`'s refusal. Every printed command is followed
    to the letter, so each one obeys three rules:

    - it copies the working file aside FIRST (`keep_aside`), to a new path in the git
      directory it names, whenever it overwrites, moves or deletes the file. On a
      case-insensitive filesystem
      the variant and `.forkflow.toml` are ONE file on disk, and the user may have edited it
      - the outer messages used to invite that. `test ! -e` makes a second run of the same
      line stop before it could copy the restored file over the saved one;
    - it never commits on the trunk or the mirror: there the answer is an explanation and
      no command (`no_commit_here`) - the fix is a branch off `origin/<trunk>`, shipped;
    - nothing that git refuses and then suggests `-f` for: the index entries go with
      `update-index --force-remove`, which touches no file and does not refuse in the middle
      of a merge the way `git rm --cached` does.

    By what git holds, not by the name:

    - git holds no config under any case - untracked, this clone's own file: renamed to the
      exact name (no commit, on any branch);
    - `.forkflow.toml` is in HEAD - a sync brought upstream's variant in beside the fork's
      own, or the file was renamed on disk: both names leave the index and the fork's copy
      comes back from HEAD (with the index entry gone, checkout writes the file anew - under
      the exact name - even when the bytes are the same);
    - only a variant is tracked: `git mv` gives it the exact name, its content kept."""
    tracked = tracked_config_names(root)
    variants = [p for p in tracked if p != CONFIG_FILE]
    rc, out, _ = git_rc("ls-tree", "-z", "--name-only", "HEAD", cwd=root)
    in_head = config_name_in(out.split("\0")) if rc == 0 else None
    save, keep = keep_aside(root, found)
    kept = f"; the file as it is now is copied to `{keep}` first"
    merging = merge_in_progress(root)               # its file may be upstream's
    if not tracked and in_head is None and not merging:
        return (f"git holds no `{CONFIG_FILE}` under any case here, so it is this clone's own: "
                f"`{save} && mv -- {sh_arg(found)} {CONFIG_FILE}` gives it the exact name{kept}")
    trunk = remote_trunk(root)
    why = no_commit_here(root, trunk)
    if why:
        return (f"{why}, and nothing is committed on it: run forkflow on a branch off "
                f"`origin/{trunk}` instead - if this refusal comes there too, commit the fix "
                f"it names on that branch and ship it")
    again = "then commit, and run the forkflow command again"
    if in_head == CONFIG_FILE:
        names = " ".join(sh_arg(p) for p in variants + [CONFIG_FILE])
        return (f"This fork's own `{CONFIG_FILE}` is in HEAD: `{save} && git update-index "
                f"--force-remove -- {names} && git checkout HEAD -- {CONFIG_FILE}` puts it "
                f"back from there through the index{kept} - carry anything of yours over from "
                f"that copy - {again}. Do not `rm`, `git rm` or rename `{found}`: on a "
                f"case-insensitive filesystem it is the same file as `{CONFIG_FILE}`")
    if variants:
        return (f"`{save} && git mv -- {sh_arg(variants[0])} {CONFIG_FILE}` gives it the exact "
                f"name, its content kept{kept} - {again}. If a sync brought it in, renaming "
                f"it does not make it yours: that sync still treats it as upstream's - its "
                f"`gate` is shown rather than run - and `--merge` is refused while the file "
                f"is upstream's byte for byte, until you edit it yourself")
    return ("No command is safe to name for what git holds of it here - a change staged by "
            "hand, or a merge in progress whose copy is out of the index; `git status` shows "
            "which")


def load_config(root: str) -> dict:
    """{} when absent. A config that is there but cannot be read is a hard failure.

    Read only from a file listed under exactly its own name. On a case-insensitive
    filesystem (the macOS and Windows default) opening `.forkflow.toml` opens a
    `.ForkFlow.toml` just as well, while git matches paths case-exactly: `git show
    <rev>:.forkflow.toml` and `ls-files` find nothing, so every check of what a sync merge
    brought in reads "no config on either side" - and `gate`, arbitrary shell, came from
    upstream. A case variant is refused rather than read or ignored: ignoring it would still
    leave the file every tool but this one opens as the config."""
    try:
        names = os.listdir(root)
    except OSError as exc:
        raise Fail(f"{root} cannot be listed to look for {CONFIG_FILE}: {exc}")
    found = config_name_in(names)
    if found is None:
        return {}
    if found != CONFIG_FILE:
        raise Fail(f"`{found}` is not `{CONFIG_FILE}`: the names differ only in case, and "
                   f"forkflow reads its config only from a file named exactly "
                   f"`{CONFIG_FILE}` - on a case-insensitive filesystem that one name opens "
                   f"both, while git tells them apart, so no check could see what it holds. "
                   f"{variant_remedy(root, found)}", 2)
    path = os.path.join(root, CONFIG_FILE)
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
    mirror: str = DEFAULT_MIRROR
    platform: str = "unknown"
    dry_run: bool = False
    sync_prefix: str = DEFAULT_SYNC_PREFIX
    backup_prefix: str = DEFAULT_BACKUP_PREFIX
    # no `merge` here: the working tree's config is not where it is read - `fork_merge_mode`

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
              sync_prefix=sync_prefix, backup_prefix=backup_prefix)
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

# What a run of *this worktree* may resume (`sync`, `ship`) and what it published is kept per
# worktree, beside its HEAD. What is waiting to land is not: a ship made in a linked worktree
# lands where the trunk is checked out, which is usually the main one - so `pending` lives in
# the git directory every worktree of the clone shares, and `land` and `status` see it from any.
# It is a map keyed by branch: worktrees ship different branches at the same time, and one
# record for the clone let each ship overwrite the other's (`record_pending`)
SHARED_STATE = ("pending",)


def state_path(ctx: Ctx, shared: bool = False) -> str:
    """The state file of this worktree, or with `shared` the one all its worktrees share
    (`--git-common-dir`; the same file as the main worktree's own)."""
    if not shared:
        return git_path(ctx.root, STATE_FILE)
    common = git("rev-parse", "--git-common-dir", cwd=ctx.root, check=False)
    if not common:
        return ""
    return os.path.join(common if os.path.isabs(common) else os.path.join(ctx.root, common),
                        STATE_FILE)


def load_state(ctx: Ctx, shared: bool = False) -> Tuple[dict, str]:
    """(what the state file holds, "") - or ({}, why it cannot be read at all).

    The one parser, because "this file says nothing" and "this file cannot be read" are two
    different facts and the difference decides things. An unparseable file used to answer
    `{}` here and nowhere else, so a truncated write, a full disk or a hand edit was
    indistinguishable from a clone that had never recorded anything: `upstream_configs` -
    which is the only thing standing between a version upstream has withdrawn and the
    `--merge` gate - was simply forgotten, in silence, and the next write put the file back
    with one key in it so the record was gone for good (scratchpad `f10/repro15.py`).

    So the two facts are told apart here and each caller decides what it costs.
    `read_state` keeps the tolerant answer, because a command that does not depend on the
    memory has no business failing over it; `change_state` will not write over a file it
    cannot read, and `config_memory_unprovable` refuses `--merge`."""
    path = state_path(ctx, shared)
    if not path:
        return ({}, f"git could not say where `{STATE_FILE}` lives")
    if not os.path.exists(path):
        return ({}, "")                    # never written here: no record to have lost
    try:
        with open(path, "r") as fh:
            data = json.load(fh)
    except OSError as exc:
        return ({}, f"`{path}` cannot be read ({exc.strerror or exc})")
    except ValueError as exc:
        return ({}, f"`{path}` is not the JSON object forkflow writes there ({exc})")
    if not isinstance(data, dict):
        return ({}, f"`{path}` holds a JSON {type(data).__name__} where forkflow writes an "
                    f"object")
    return (data, "")


def read_state(ctx: Ctx, shared: bool = False) -> dict:
    """What the state file holds, {} when it holds nothing this tool can read. Deliberately
    tolerant: see `load_state`, and `state_unreadable` for the other half of the answer."""
    return load_state(ctx, shared)[0]


def state_unreadable(ctx: Ctx, shared: bool = False) -> str:
    """Why the state file cannot be read, "" when it can be (or is not there at all)."""
    return load_state(ctx, shared)[1]


def save_state(ctx: Ctx, data: dict, shared: bool = False) -> str:
    """Write `data`, and answer "" - or why it could not be written.

    Written to a temporary file beside it and renamed over it: an interrupted write leaves
    the old file whole, where writing in place left a truncated one that reads back as {}
    and loses every record in it.

    A failure used to be swallowed here, which made a state file that cannot be written -
    a read-only git directory, a full disk, a `forkflow-state.json` that is not a file -
    invisible: the run went on to report a branch as landable with no record of it
    anywhere. Nothing here decides what that costs; each caller says what was lost."""
    path = state_path(ctx, shared)
    if not path:
        return f"git could not say where `{STATE_FILE}` lives"
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return ""
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return f"{path}: {exc.strerror or exc}"


class StateLocked(OSError):
    """No lock on the state file: another run held it for longer than forkflow waits, or
    this platform has no lock to take."""


def state_lock_path(path: str) -> str:
    return path + ".lock"


def lock_exclusive(fd: int) -> bool:
    """Take the operating system's exclusive lock on `fd` without waiting for it: True when
    this run holds it, False while another run does, StateLocked when none can be taken here.

    The KERNEL owns the lock, and that is the whole point of it. The lock belongs to the
    open file description rather than to the pathname or to a record written in a file, so
    it is released when this process exits or is KILLED. There is no staleness to judge, no
    lock to steal, and no way for one run to release what another holds.

    What this replaced created the lock file exclusively, called one older than ten minutes
    the leftover of a run that had died, and unlinked it. Two reviews in a row found races
    in that judgement and the third named the reason there will always be one: a stat and
    an unlink are two statements about two different files, so the unlink lands on whatever
    the path names by the time it runs - which can be the lock a third run has just taken.
    No extra check fixes that, because the check is the thing that cannot be made atomic.

    `fcntl.flock` on POSIX and `msvcrt.locking` on Windows, both standard library, both
    released by the operating system when the holder goes. A platform with neither gets a
    refusal and a plain reason, never an unserialised write.

    On a NETWORK filesystem - NFS, SMB, some container overlays - neither call is dependable:
    it may be a silent no-op, it may fail outright, or it may serialise only the runs on one
    mount. forkflow builds no fallback for that, because every fallback is another staleness
    judgement: a lock that cannot be taken is a refusal that says so, and a clone on such a
    mount wants one forkflow at a time."""
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in HELD_ELSEWHERE:
                return False
            raise StateLocked(unlockable(exc))
    if msvcrt is not None:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in HELD_ELSEWHERE:
                return False
            raise StateLocked(unlockable(exc))
    raise StateLocked(f"this Python has neither `fcntl` nor `msvcrt`, so `{STATE_FILE}` "
                      f"cannot be locked and two forkflow runs writing it at once would "
                      f"lose each other's records; nothing was written. Run forkflow on a "
                      f"Python that has one of them - every supported platform ships one")


def unlockable(exc: OSError) -> str:
    """Why a lock the operating system was asked for could not be taken at all - a
    filesystem that does not implement it (a network mount, an exotic overlay), never
    another run holding it."""
    return (f"this filesystem will not lock `{STATE_FILE}` ({exc.strerror or exc}), so two "
            f"forkflow runs writing it at once cannot be kept apart; nothing was written. "
            f"Run one forkflow at a time, or keep the clone where the operating system "
            f"locks files")


def take_state_lock(path: str) -> int:
    """Hold the lock for `path` until `drop_state_lock` closes what this returns, waiting
    up to `STATE_LOCK_WAIT` for whoever holds it; raise StateLocked when the wait runs out.

    The lock file is created when it is not there and is NEVER removed: what keeps two runs
    apart is the kernel's lock on the open descriptor and not the file's existence, so all
    a finished run leaves behind is an empty file in the git directory that nobody holds.
    Removing it is what the design before this one did, and removing it is exactly how one
    run came to release another's lock.

    A run that cannot get the lock writes NOTHING - `change_state` answers with this
    failure and never falls through to the write."""
    lock = state_lock_path(path)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise StateLocked(f"{lock}: {exc.strerror or exc}")
    deadline = time.monotonic() + STATE_LOCK_WAIT
    while True:
        try:
            got = lock_exclusive(fd)
        except StateLocked:
            os.close(fd)
            raise
        if got:
            return fd
        if time.monotonic() >= deadline:
            os.close(fd)
            raise StateLocked(f"{lock} is held by another forkflow run (waited "
                              f"{STATE_LOCK_WAIT:g}s); let that run finish and try again. "
                              f"The lock is the operating system's, on that open file, and "
                              f"it is released the moment the run holding it ends - even if "
                              f"that run is killed - so there is never one left over for you "
                              f"to clear away")
        time.sleep(STATE_LOCK_POLL)


def drop_state_lock(fd: int) -> None:
    """Let go of what `take_state_lock` returned: closing the descriptor is what releases
    the kernel's lock. The file itself stays, and is meant to."""
    try:
        os.close(fd)
    except OSError:
        pass


def change_state(ctx: Ctx, shared: bool, change) -> str:
    """Read the state file, let `change` edit what it holds, write it back - all while
    holding that file's lock. Answers "" when it was written, else why it was not.

    The one writer, because read-modify-write is what every record here is: `os.replace`
    makes each write whole for a READER, and does nothing about the window between a
    caller's read and its own write. Two worktrees shipping at the same time both read the
    `pending` map, each adds its branch, and the second write puts the map back as the
    first found it - eight concurrent ships left ONE record, every time, and the ships
    whose records went are landable by nothing the tool offers. The same window let a land
    clearing its own record write back a map from before another worktree's ship.

    A file that cannot be READ is not written over. Read-modify-write on an unparseable
    file is read-as-{}-and-replace: one key goes in and every record that was in there -
    including what this clone had written down of the original project's configs, which is
    the whole defence against a withdrawn version - is gone, with nothing said. So this
    answers with the reason instead, the callers say what was lost the way they already do
    for a disk that is full, and `config_memory_unprovable` refuses `--merge` until the
    user deals with the file. Nothing here deletes it: what is in it is the user's."""
    path = state_path(ctx, shared)
    if not path:
        return f"git could not say where `{STATE_FILE}` lives"
    try:
        fd = take_state_lock(path)
    except OSError as exc:
        return str(exc)
    try:
        data, why = load_state(ctx, shared)
        if why:
            return (f"{why}, and writing over it would lose whatever is in there for good; "
                    f"nothing was written. Look at that file - and if you accept losing "
                    f"every record in it, including which `{CONFIG_FILE}`s the original "
                    f"project has had, delete it")
        change(data)
        return save_state(ctx, data, shared)
    finally:
        drop_state_lock(fd)


def write_state(ctx: Ctx, reason: str, entry: Optional[dict]) -> str:
    """Record (or, with entry=None, forget) what a `--continue` of this kind may resume -
    or, for a `SHARED_STATE` reason, what every worktree of the clone has to see. Answers
    "" when it was written, else why it was not."""
    if ctx.dry_run:
        return ""

    def change(data: dict) -> None:
        if entry is None:
            data.pop(reason, None)
        else:
            data[reason] = entry

    return change_state(ctx, reason in SHARED_STATE, change)


def record_published(ctx: Ctx, branch: str, commit: str) -> None:
    """Remember that this clone put `commit` on `origin/<branch>`.

    This is the only trustworthy answer to "may `ship` replace `origin/<branch>`?". The branch
    reflog is not one: a teammate's commit reaches `refs/heads/<branch>`'s reflog through an
    ordinary `git pull` or `git checkout`, and could then be reset away and force-pushed away
    on the strength of having once been there.

    Backups are left out - they are written once and never rewritten, so nothing ever has to
    prove one is ours - and the list keeps only the newest `PUBLISHED_KEEP` entries, so the
    state file cannot grow without bound.

    A failure to write is not reported: the cost of forgetting a push is that a later ship
    of the same branch refuses to force-push over it (exit 5, with the backup route), which
    is the safe direction, and every caller here is in the middle of a push whose own
    result is what the run is about."""
    if ctx.dry_run or not commit or branch.startswith(ctx.backup_prefix):
        return

    def change(data: dict) -> None:
        entries = data.get("published")
        kept = [e for e in (entries if isinstance(entries, list) else [])
                if isinstance(e, list) and len(e) == 2 and e != [branch, commit]]
        kept.append([branch, commit])
        data["published"] = kept[-PUBLISHED_KEEP:]

    change_state(ctx, False, change)


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


def pending_shape(entry: object) -> bool:
    """True for exactly the shape `record_pending` writes (`mr` may be absent)."""
    return (isinstance(entry, dict)
            and all(isinstance(entry.get(k), str) for k in PENDING_FIELDS)
            and isinstance(entry.get("mr", ""), str))


def pending_map(raw: object) -> dict:
    """{branch: entry} from what the state file holds under `pending`.

    Only entries of the written shape, each under its own branch's name: a state file edited
    by hand is no state rather than a crash - the same defensive read as `resumable`. The
    single bare entry an earlier build kept reads as a map of one, so a user who upgrades
    between a ship and its landing keeps the record."""
    if pending_shape(raw):
        return {raw["branch"]: raw}
    if not isinstance(raw, dict):
        return {}
    return {b: e for b, e in raw.items() if pending_shape(e) and e["branch"] == b}


def record_pending(ctx: Ctx, kind: str, branch: str, base: str, url: str = "") -> dict:
    """What is waiting to land, for `land` and `status`: the tip this run pushed on `branch`
    and the `origin/<trunk>` it was built on, with the merge request's URL once known.
    Returns the entry, which is what `--merge` lands - not whatever the file holds by then.

    Written right after the push succeeds and before the merge request step, so a merge
    request that fails to open still leaves a landable record, then rewritten with the URL.
    One entry per branch, in the state file every worktree shares: a second ship of the same
    branch replaces its entry, and ships of other branches - from other worktrees, at the
    same time - keep theirs. Read, changed and written back under the file's lock
    (`change_state`), so what another worktree records in between is kept rather than
    written back over. A dry run writes nothing.

    Nothing is said here about a write that failed: `report_pending` reads the file back
    once, after the merge request step, and says what stands and what to run."""
    entry = {"kind": kind, "branch": branch, "commit": rev(ctx.root, f"refs/heads/{branch}"),
             "base": base, "mr": url}

    def change(data: dict) -> None:
        entries = pending_map(data.get("pending"))
        entries[branch] = entry
        data["pending"] = entries

    if not ctx.dry_run:
        change_state(ctx, True, change)
    return entry


def forget_pending(ctx: Ctx, entry: dict) -> bool:
    """Clear `entry` once it has landed - only while the record under its branch is still
    that one (the same branch and commit), read under the lock that the write then happens
    under (`change_state`): another worktree may have shipped the branch again since, or
    recorded a ship of its own, and neither is this run's to erase. False when another
    record stands there now (or in a dry run); True when it was cleared, or there was
    nothing under the branch to clear."""
    if ctx.dry_run:
        return False
    cleared = [True]

    def change(data: dict) -> None:
        entries = pending_map(data.get("pending"))
        now = entries.get(entry["branch"])
        if not now:
            return                                   # nothing of this branch's to clear
        if now["commit"] != entry["commit"]:
            cleared[0] = False                       # a newer ship of the same branch
            return
        del entries[entry["branch"]]
        if entries:
            data["pending"] = entries
        else:
            data.pop("pending", None)

    change_state(ctx, True, change)
    return cleared[0]


def pending_entries(ctx: Ctx) -> dict:
    """Every recorded `pending` entry, {branch: entry} ({} when there is none). Read from
    the state every worktree shares (`SHARED_STATE`), so it is the same whichever asks."""
    return pending_map(read_state(ctx, shared=True).get("pending"))


def resume_unrecorded(ctx: Ctx, kind: str, why: str, backup_ref: str) -> None:
    """Say so when the record that ties a `--continue` to this run's backup could not be
    written. Nothing is undone by it: the backup is on origin under the name printed here,
    and this run goes on. What is lost is only the resume - `ship --continue` refuses
    without it, and names the command to run instead."""
    if not why:
        return
    print(f"  WARNING: this run's `{kind}` resume record could not be written ({why}) - if "
          f"this run stops on conflicts, resuming it with `--continue` will refuse and name "
          f"the command to run in its place. The backup `{backup_ref}` is on "
          f"`{ctx.origin}` either way.")


def report_pending(ctx: Ctx, entry: dict, url: str) -> bool:
    """True when the shared file holds this run's record; otherwise say so, and say what
    to run instead of `forkflow land`.

    The record is read back rather than trusted. A state file that could not be written was
    silent, so a run whose push and merge request had both succeeded went on to print
    "after the MR is merged, next: forkflow land" about a record that does not exist, and
    `land` then answered "nothing pending" about a branch that is pushed with its request
    open. A record another run replaced in the meantime is the same fact for this run.

    Never fatal, and it undoes nothing: the push and the merge request have happened, and
    what is needed is the truth about them and a way to finish by hand. The commands are
    the ones `land` would have run - a fetch, a fast-forward of the LOCAL trunk onto what
    origin has, and the branch deleted only once `-d` can see it is merged. Nothing here
    commits, force-pushes or touches the trunk on origin.

    `git checkout`, not the `switch` spelling: this project's floor is git 2.20 (README
    *forkflow*), `switch` arrived in 2.23, and every command printed here is followed to
    the letter - one that a supported git answers "unknown command" to is a dead end in the
    middle of a recovery. `TestSourceInvariants` holds the whole script to that floor."""
    branch = entry["branch"]
    if ctx.dry_run or pending_entries(ctx).get(branch) == entry:
        return True
    where = (f"its merge request is open ({url})" if url
             else "no merge request was opened by this run")
    print(f"  WARNING: `{branch}` is pushed at {short(entry['commit'])} and {where}, "
          f"but forkflow could not record that: "
          f"`{state_path(ctx, shared=True) or STATE_FILE}` holds no such entry, so "
          f"`{land_cmd()}` will answer \"nothing pending\" for it.")
    print(f"    Nothing is lost - the branch and the request are as this run left them. "
          f"Once the request is merged, finish it by hand:")
    print(f"      git fetch {sh_arg(ctx.origin)}")
    print(f"      git checkout {sh_arg(ctx.trunk)}")
    print(f"      git merge --ff-only {sh_arg(ctx.origin + '/' + ctx.trunk)}")
    print(f"      git branch -d {sh_arg(branch)}   # refuses while it is not on the trunk")
    return False


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
    prefix = f"  {DRY_PREFIX}" if dry else "  "
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
    trunk_name = f"{ctx.origin}/{ctx.trunk}"
    if not rev(ctx.root, trunk_name) or not rev(ctx.root, ctx.up()):
        return (None, None)
    mb = git("merge-base", ctx.up(), trunk_name, cwd=ctx.root, check=False)
    if not mb:
        return (None, None)
    files = diff_names(ctx, mb, trunk_name)
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


def fetch_preview(ctx: Ctx, remotes: Sequence[str],
                  refs: Sequence[str]) -> Tuple[int, str, str, str, list]:
    """What a fetch would have found, for a `--dry-run` that must write nothing.

    The fifth field is the refs the server has MOVED PAST - the ones this run would have
    fetched and did not. A caller that then CONCLUDES something from a ref on that list is
    concluding it from bytes it already knows are out of date, which is what the "already
    in sync" answer did (`cmd_sync`). The refs NOT on it are the same here as on the
    server, so what is read off one of those is as true as a fetch would have made it -
    which is why this is a list of refs and not one flag for the whole preview.

    `git ls-remote` asks the same server over the same transport in one round trip and
    writes nothing at all - no `FETCH_HEAD`, no remote-tracking ref, no object - so each
    named ref can be reported as it is HERE and as it is on the remote. What the rest of the
    run then reasons from is the refs on disk, unchanged, which is why the line says so
    rather than reading like a fetch that happened. With no ref named there is nothing to
    compare, so nothing is asked of the network either.

    A failure to reach the remote is the caller's to handle exactly as a failed fetch is:
    the dry run of a command that could not have run is not a dry run that passed."""
    shown, moved, stale = [], [], []
    for ref in refs:
        remote, _, branch = ref.partition("/")     # the callers name `<remote>/<branch>`
        if not branch or remote not in remotes:
            continue
        cmd = f"git ls-remote {sh_arg(remote)} {sh_arg('refs/heads/' + branch)}"
        shown.append(cmd)
        rc, out, err = git_rc("ls-remote", remote, "refs/heads/" + branch, cwd=ctx.root)
        if rc != 0:
            return (rc, cmd, "", err, [])
        here, there = rev(ctx.root, ref), (out.split("\t")[0] if out.strip() else "")
        moved.append(f"{ref} {short(here) or '-'} here"
                     + ("" if here == there else
                        f", {short(there) or 'gone'} on {remote}"))
        if here != there:
            stale.append(ref)
    if not shown:
        args = ["fetch"] + (["--multiple"] if len(remotes) > 1 else []) + list(remotes)
        return (0, "git " + " ".join(sh_arg(a) for a in args),
                "not run (dry run): the refs on disk are as the last fetch left them",
                "", [])
    tail = (" - NOT fetched (dry run): everything below is judged from the refs on disk"
            if stale else " (nothing to fetch)")
    return (0, " && ".join(shown), ", ".join(moved) + tail, "", stale)


def fetch(ctx: Ctx, remotes: Sequence[str],
          refs: Sequence[str] = ()) -> Tuple[int, str, str, str, list]:
    """Refresh `remotes` and say what moved: (returncode, command, result, stderr, stale).

    `stale` is the refs nothing may be concluded from. After a real fetch it is always
    empty - the fetch has just made every named ref the server's - and in a dry run it is
    what `fetch_preview` found the server had moved past.

    `result` is the `step()` line a successful fetch deserves - one `<ref> <old>..<new>` (or
    `<ref> unchanged`) per named ref, or "remote-tracking refs refreshed" when the caller
    names none. Nothing is printed and nothing is raised here: whether a failure stops the run
    or is only reported is the caller's, and `status` still has the refs on disk to report.

    A dry run does not fetch (`fetch_preview`): `git fetch` rewrites `FETCH_HEAD`, moves
    remote-tracking refs and brings objects in - three writes in the one command every
    `--dry-run` path used to run before it promised to write nothing."""
    if ctx.dry_run:
        return fetch_preview(ctx, remotes, refs)
    args = ["fetch"] + (["--multiple"] if len(remotes) > 1 else []) + list(remotes)
    cmd = "git " + " ".join(sh_arg(a) for a in args)
    before = dict((r, rev(ctx.root, r)) for r in refs)
    rc, _, err = git_rc(*args, cwd=ctx.root)
    if rc != 0:
        return (rc, cmd, "", err, [])
    moved = []
    for r in refs:
        now = rev(ctx.root, r)
        moved.append(f"{r} unchanged" if now == before[r]
                     else f"{r} {short(before[r])}..{short(now)}")
    return (0, cmd, ", ".join(moved) if moved else "remote-tracking refs refreshed",
            err, [])


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
                       f"{ctx.origin}", EXIT_UNSAFE)
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
                       f"force-push `{branch}` without a restore point", EXIT_UNSAFE)
    rc, _, err = git_rc(*args, cwd=ctx.root)
    if rc != 0:
        step("push", cmd, "REJECTED")
        raise Fail(f"push of `{branch}` was rejected by {ctx.origin}:\n{err.strip()}", EXIT_UNSAFE)
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
        raise Fail(f"push of the mirror `{m}` was rejected by {ctx.origin}:\n"
                   f"{err.strip()}", EXIT_UNSAFE)
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


def advance_mirror(ctx: Ctx, target: str) -> Tuple[str, str]:
    """Fast-forward the local mirror to target. Returns (old sha, new sha)."""
    m = ctx.mirror
    old = rev(ctx.root, f"refs/heads/{m}")
    if old and old != target:
        rc, _, _ = git_rc("merge-base", "--is-ancestor", old, target, cwd=ctx.root)
        if rc == 1 and ctx.dry_run:
            # the target is `<upstream>/<branch>` as the LAST FETCH left it, and this run did
            # not fetch (a dry run writes nothing). A teammate's sync legitimately puts the
            # mirror ahead of a stale upstream ref, and calling that rule 6 would be a guess
            raise Fail(f"this dry run did not fetch, so it cannot preview the sync: `{m}` is "
                       f"not an ancestor of {short(target)}, which is `{ctx.up()}` as the "
                       f"last fetch left it - the `fetch` line above says what "
                       f"`{ctx.upstream}` has now. Run `git fetch {sh_arg(ctx.upstream)}` and "
                       f"try again, or run the same command without `--dry-run`, which "
                       f"fetches first")
        if rc == 1:
            raise Fail(f"`{m}` is not an ancestor of {short(target)}: the mirror only ever "
                       f"moves forward. {README_POINTER}")
        if rc != 0:
            raise Fail(f"cannot compare `{m}` with {short(target)}: "
                       f"run `git fetch {sh_arg(ctx.upstream)}`")
    wt = branch_worktree(ctx, m)
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
        raise Fail(f"push of the new trunk `{t}` was rejected by {ctx.origin}:\n"
                   f"{err.strip()}", EXIT_UNSAFE)
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
                   f"without a confirmed restore point", EXIT_UNSAFE)
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
    rc, out, err = merge_tree(ctx.root, "--write-tree", "--name-only",
                              f"{ctx.origin}/{ctx.trunk}", target)
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
        return ('merge it with "Rebase and merge" - GitHub rewrites the commit\'s SHA; '
                f"`{land_cmd()}` recognises the patch and deletes the local branch")
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


def open_mr(ctx: Ctx, branch: str, title: str, body: str, run_it: bool) -> str:
    """Print the MR command and, with --mr, run it. Never a failure: the branch is pushed,
    the merge request is the only thing left to do.

    Answers the merge request's URL: the first stdout line of the tool that starts with
    `http` - what both glab and gh print - and "" when the tool was not run, could not run
    or failed. It is kept for the `pending` record and for display only: nothing parses it,
    the merge step addresses the MR by its branch. The command as shown is not answered
    with it - it is printed here, on the `mr` line, which is where the tests read it."""
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
        return ""
    shown = shown_argv(cmd)
    if ctx.dry_run:
        step("mr", shown, "not run (dry run)", dry=True)
        return ""
    if not run_it:
        step("mr", shown, "not run (add --mr to run it)")
        print(f"    description: {path}")
        return ""
    p = run_tool(ctx, cmd)
    if p is None:
        step("mr", shown, f"{cmd[0]} unavailable (not installed, or not runnable)")
        print(f"    description: {path}")
        return ""
    out = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0:
        step("mr", shown, f"FAILED (exit {p.returncode}) - open it yourself")
        for line in tail_lines(p.stderr.decode("utf-8", "replace"), TAIL_LINES):
            print(f"      {line}")
        print(f"    description: {path}")
        return ""
    step("mr", shown, "created")
    for line in out.splitlines():
        print(f"    {line}")
    try:
        os.unlink(path)          # the description is on the platform now; nobody needs the file
    except OSError:
        pass
    return next((ln.strip() for ln in out.splitlines() if ln.strip().startswith("http")), "")


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
    if not target:                    # `mr_target` names a fork on gitlab or github only
        return []
    if ctx.platform == "gitlab":
        return ["glab", "mr", "merge", branch, "--repo", target, "--sha", head,
                "--auto-merge=false", "--remove-source-branch", "--yes"]
    return ["gh", "pr", "merge", branch, "--repo", target, "--match-head-commit", head,
            "--merge" if kind == "sync" else "--rebase"]


def merge_mr(ctx: Ctx, kind: str, branch: str, url: str) -> None:
    """Merge the merge request `open_mr` just opened on `branch`, or exit 6.

    Runs after the push and after the `pending` record has the merge request's URL, so any
    failure here leaves a landable state: the branch is on origin, the merge request is
    open, and `forkflow land` finishes the job once it is merged by hand - which is what
    the exit-6 message says. A merge request that was never created (the tool failed, was
    missing, or printed no URL) is the same exit: there is nothing *this run opened* to
    merge - which includes a request already open for the branch, whose `create` fails;
    `--merge` merges only what it opened. A dry run shows the merge and the landing it
    would chain into and runs neither. `merge_gate` has made sure the fork can be named,
    so `merge_command` always has a command here.

    `merge = "self"` is asked again first (`fork_merge_mode`), for a fresh run and a
    resumed one alike: the gate read `origin/<trunk>` before this run's fetch moved it and
    before the sync merge touched the tree, and this is the last point where not merging
    costs nothing - the request stays open for a person to merge, which is exit 6."""
    head = "<pushed head>" if ctx.dry_run else rev(ctx.root, f"refs/heads/{branch}")
    cmd = merge_command(ctx, kind, branch, head)
    shown = shown_argv(cmd)
    if ctx.dry_run:
        step("merge", shown, "not run (dry run)", dry=True)
        step("land", land_cmd(), "not run (dry run)", dry=True)
        return
    if fork_merge_mode(ctx) != MERGE_SELF:
        step("merge", shown, "NOT RUN: this fork's config does not say merge = \"self\" now")
        raise Fail(f"--merge needs `merge = \"self\"` in this fork's own config - "
                   f"{fork_merge_source(ctx)} - and after this run's fetch and merge it does "
                   f"not say that: the branch is pushed and the merge request is open; have "
                   f"it merged by hand, then: {land_cmd()}", EXIT_NOT_MERGED)
    if not url:
        raise Fail(f"the merge request was not created by this run (or one was already open "
                   f"for `{branch}`) - the branch is pushed; open or find it and merge it by "
                   f"hand, then: {land_cmd()}", EXIT_NOT_MERGED)
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
               f"({url}), then: {land_cmd()}", EXIT_NOT_MERGED)


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
        rc, cmd, result, err, _ = fetch(ctx, (ctx.origin, ctx.upstream))
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
                                f"`{rerun_cmd('sync', None)}` to take it")
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

    entries = pending_entries(ctx)
    for branch in sorted(entries):
        print(pending_line(ctx, branch, entries[branch]))

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


def config_text(ctx: Ctx, revision: str) -> Optional[str]:
    """`.forkflow.toml` as of `revision`; None when it is not there at all. (The working
    tree's is `load_config`'s alone, which refuses a case variant.)

    A committed file whose name differs from `.forkflow.toml` only in case counts as it
    (the exact name wins where a case-sensitive clone holds both): it is the file a
    case-insensitive checkout opens under that name, so "what did the merge bring in" has to
    see it - git's own `<rev>:<path>` matches case-exactly and would read it as absent."""
    name = config_name_at(ctx, revision)
    if name is None:
        return None
    rc, out, _ = git_rc("show", f"{revision}:{name}", cwd=ctx.root)
    return out if rc == 0 else None


def config_name_at(ctx: Ctx, revision: str) -> Optional[str]:
    """The name `.forkflow.toml` has in `revision`'s tree under any case of it (see
    `config_text`), None when the tree holds none - and the exact name when the tree cannot
    be listed, so the caller's `git show` gives the answer."""
    rc, out, _ = git_rc("ls-tree", "-z", "--name-only", revision, cwd=ctx.root)
    return config_name_in(out.split("\0")) if rc == 0 else CONFIG_FILE


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

    trunk_name = f"{ctx.origin}/{ctx.trunk}"
    tip_cmd = f"git merge-base --is-ancestor {sh_arg(trunk_name)} HEAD"
    if current_branch(ctx) == ctx.trunk:
        step("tip", tip_cmd, f"skipped (on the trunk `{ctx.trunk}`)")
    else:
        rc, _, err = git_rc("merge-base", "--is-ancestor", trunk_name, "HEAD", cwd=ctx.root)
        if rc == 0:
            step("tip", tip_cmd, f"on {trunk_name}'s tip")
        elif rc == 1:
            ahead, behind = ahead_behind(ctx, "HEAD", trunk_name)
            where = f"behind by {behind}, ahead by {ahead}"
            step("tip", tip_cmd, f"not on {trunk_name}'s tip ({where})")
            failures.append(f"not on {trunk_name}'s tip ({where})")
        else:
            raise Fail(f"cannot compare HEAD with {trunk_name}: {err.strip()}")

    if failures:
        print("  check    FAILED: " + "; ".join(failures))
        return EXIT_CHECK
    print("  check    ok")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=True)
    header(ctx, "check")
    return run_check(ctx)


def sync_branch(ctx: Ctx) -> str:
    """`sync/<upstream remote>-<UTC YYYYMMDD>` - a non-standard remote name shows in the name."""
    return f"{ctx.sync_prefix}{ctx.upstream}-{utc_stamp()}"


def fetch_both(ctx: Ctx) -> list:
    """Refresh both remotes and report what moved; every later step reads these refs.

    Answers with the refs a dry run did NOT fetch and the server has moved past, for the
    one caller that goes on to draw a CONCLUSION rather than a preview out of them."""
    rc, cmd, result, err, stale = fetch(ctx, (ctx.origin, ctx.upstream),
                                        [ctx.up(), f"{ctx.origin}/{ctx.trunk}",
                                         f"{ctx.origin}/{ctx.mirror}"])
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    step("fetch", cmd, result)
    return stale


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


def sync_branch_is_free(ctx: Ctx, name: str, force: bool,
                        args: Optional[argparse.Namespace] = None) -> None:
    """Refuse a sync branch that already exists, locally or on origin. Checked before the
    backup as well as inside `make_sync_branch`, so a rerun on the same day leaves no orphan
    backup on origin.

    origin is asked with `ls-remote`, as `free_sync_name` does and for the same reason: this
    runs before the fetch, and another clone's sync counts too. A branch that is only on
    origin gets the `--force` answer, not the `--continue` one - there is no local branch to
    resume, so `--continue` cannot get past the push that would be rejected at the end. The
    commands named carry this run's `--merge`/`--mr` (`rerun_cmd`)."""
    if force:
        return
    if has_ref(ctx.root, f"refs/heads/{name}"):
        raise Fail(f"branch `{name}` already exists: resume it with "
                   f"`{continue_cmd('sync', args)}`, or recreate it from "
                   f"{ctx.origin}/{ctx.trunk} with `{rerun_cmd('sync', args, ' --force')}`")
    rc, out, _ = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{name}", cwd=ctx.root)
    if rc == 0 and f"refs/heads/{name}" in out:
        raise Fail(f"`{name}` is already on {ctx.origin} and this clone has no local copy: a "
                   f"published sync branch is never rebased or force-pushed (rule 5). Close "
                   f"its merge request and rerun with `{rerun_cmd('sync', args, ' --force')}`, "
                   f"which publishes the next free `{name}-N`")


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


def make_sync_branch(ctx: Ctx, name: str, force: bool,
                     args: Optional[argparse.Namespace] = None) -> None:
    """Create the sync branch off `origin/<trunk>` and switch to it. Never off the local trunk:
    the MR has to apply to what is published."""
    base = f"{ctx.origin}/{ctx.trunk}"
    cmd = f"git checkout --no-track -b {sh_arg(name)} {sh_arg(base)}"
    sync_branch_is_free(ctx, name, force, args)
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
    is exactly the collision this answers, and rename detection hid it.

    A name the merge writes collides with an untracked file of another case too, when the
    filesystem makes the two one file (macOS and Windows by default): upstream's
    `.ForkFlow.toml` lands on this fork's untracked `.forkflow.toml`, and git refuses that
    merge just the same - after the backup, had only exact names been compared. The
    filesystem is asked (`samefile`), not `core.ignorecase`: git's own refusal comes from an
    `lstat` of the path it is about to write. The name reported is the one on disk - the
    file the user knows as theirs."""
    base = f"{ctx.origin}/{ctx.trunk}"
    added = set(diff_names(ctx, "--no-renames", "--diff-filter=A", base, target))
    merge_base = git("merge-base", base, target, cwd=ctx.root, check=False)
    if merge_base:
        added &= set(diff_names(ctx, "--no-renames", "--diff-filter=A", merge_base, target))
    if not added:
        return []
    out = git("ls-files", "--others", "--exclude-standard", "-z", cwd=ctx.root, check=False)
    others = [f for f in out.split("\0") if f]
    # an IGNORED untracked file is one git writes over without a word - it counts ignored
    # files as expendable - and a fork keeping setup's `.forkflow.toml` in `info/exclude`
    # lost its `merge`, `gate` and branch names that way. The config lives at the top of the
    # tree, so the listing there is asked, whatever the ignore rules say
    try:
        names = os.listdir(ctx.root)
    except OSError:
        names = []
    tracked = set(tracked_config_names(ctx.root))
    others += [n for n in names if n.casefold() == CONFIG_FILE.casefold()
               and n not in tracked and n not in others]
    blocked = added & set(others)
    by_fold: dict = {}
    for f in others:
        by_fold.setdefault(f.casefold(), []).append(f)
    for path in added - blocked:
        for mine in by_fold.get(path.casefold(), []):
            if same_file(ctx.root, path, mine):
                blocked.add(mine)
    return sorted(blocked)


def same_file(root: str, a: str, b: str) -> bool:
    """Do the two names open one file here? False when either is not there at all."""
    try:
        return os.path.samefile(os.path.join(root, a), os.path.join(root, b))
    except OSError:
        return False


def in_the_way_advice(ctx: Ctx, paths: Sequence[str], rerun: str,
                      args: Optional[argparse.Namespace]) -> str:
    """What to do about untracked files a sync merge would write over - never "delete them".

    The config is its own answer: `setup` leaves `.forkflow.toml` untracked, and it is this
    fork's - a gate, the branch names, `merge`. It goes into the trunk the way everything
    does (a branch, `ship`), and the next sync then meets upstream's copy as a tracked file,
    where the merge shows the two side by side. That ship is `--mr`, not `--merge`: once the
    file is committed on a branch it is neither untracked nor on `origin/<trunk>`, so
    `fork_merge_mode` has no `merge = "self"` to read until a person has merged it. Any
    other file is moved out of the tree: on a case-insensitive filesystem the name git lists
    may be another spelling of one of the user's own files, so a removal can take a file
    that was never upstream's."""
    trunk = f"{ctx.origin}/{ctx.trunk}"
    if any(p.casefold() == CONFIG_FILE.casefold() for p in paths):
        ship = rerun_cmd("ship", argparse.Namespace(mr=bool(getattr(args, "mr", False))))
        mine = (f"`{CONFIG_FILE}` is this fork's own config, untracked (as `forkflow setup` "
                f"leaves it)")
        if config_is_upstreams(ctx, working_config_text(ctx)):
            # not the fork's: upstream's own file, brought in by a sync and untracked since.
            # Committed and shipped it puts upstream's bytes on the trunk, where `merge` is
            # still upstream's word - so the sentence that names the ship says so too
            mine = (f"the `{CONFIG_FILE}` here is the original project's own file, byte for "
                    f"byte - brought in by a sync and untracked since, not one this fork "
                    f"wrote, so `--merge` stays refused until you edit it yourself (any "
                    f"edit of your own makes it this fork's)")
        return (f"{mine}, and upstream tracks a `{CONFIG_FILE}` - under that name or "
                f"another case of it, which a case-insensitive filesystem makes the same "
                f"file. Do not delete or rename it: commit it on a branch off `{trunk}` and "
                f"`{ship}` it, have that merged, so it is on `{trunk}`; then {rerun}")
    return (f"Move them out of the working tree (they are not deleted that way - on a "
            f"case-insensitive filesystem a name git lists can be another spelling of a file "
            f"of yours), or get them into `{trunk}` first (commit them on a branch and "
            f"`{rerun_cmd('ship', args)}` it); then {rerun}")


def rerun_cmd(kind: str, args: Optional[argparse.Namespace], extra: str = "") -> str:
    """A `forkflow <kind>` this run tells the user to run, carrying the flag that decides
    how this run was to end: printed without `--merge` (or `--mr`), a command that is
    followed to the letter opens or merges nothing this run was asked to. `extra` is the
    rest of the command line (` --force`, ` --continue`). Every printed `forkflow sync` and
    `forkflow ship` is built here or in `continue_cmd` - `TestSourceInvariants` holds it."""
    if getattr(args, "merge", False):
        flag = " --merge"
    elif getattr(args, "mr", False):
        flag = " --mr"
    else:
        flag = ""
    return f"forkflow {kind}{extra}{flag}"


def continue_cmd(kind: str, args: Optional[argparse.Namespace]) -> str:
    """The `--continue` that resumes this run - see `rerun_cmd`."""
    return rerun_cmd(kind, args, " --continue")


def land_cmd(branch: str = "", force: bool = False) -> str:
    """A `forkflow land` this run tells the user to run. `rerun_cmd`'s problem, for the
    other subcommand that takes arguments: spelled out by hand, the `--force` or the branch
    that makes the command the right one is dropped at one site after another, and the two
    sites that print both have to agree where `--force` sits. The branch is quoted as every
    printed argument is (`sh_arg`); "" leaves it out, which is what a run landing several
    records prints. Every printed `forkflow land` is built here - `TestSourceInvariants`
    holds that, as it holds it for `forkflow sync` and `forkflow ship`."""
    return ("forkflow land" + (" --force" if force else "")
            + (f" {sh_arg(branch)}" if branch else ""))


def merge_upstream(ctx: Ctx, name: str, target: str, commits: Sequence[str],
                   args: Optional[argparse.Namespace] = None) -> None:
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
            # merge to resume: `--force` is the only rerun that is not a closed loop. git lists
            # each path it would write on a line of its own, indented by a tab
            paths = [ln.strip() for ln in text.splitlines() if ln.startswith("\t")]
            advice = in_the_way_advice(
                ctx, paths, f"rerun the sync with `{rerun_cmd('sync', args, ' --force')}` "
                            f"(`{name}` was already created, and only `--force` recreates it)",
                args)
            raise Fail(f"the merge would overwrite untracked file(s) in the working tree, "
                       f"which git refuses outright. {advice}:\n{text}")
        raise Fail(f"merge of {short(target)} failed:\n{text}")
    report_paths("merge", cmd, unmerged, "conflicting file(s)")
    raise Fail(f"resolve the conflicts on `{name}`, `git add` them, "
               f"then run `{continue_cmd('sync', args)}`", EXIT_CONFLICT)


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


def merge_in_progress(root: str) -> bool:
    """True while a merge is resolved but not committed (`MERGE_HEAD` still there)."""
    path = git_path(root, "MERGE_HEAD")
    return bool(path) and os.path.exists(path)


def unmerged_paths(ctx: Ctx) -> list:
    """Paths still carrying conflict markers."""
    return diff_names(ctx, "--diff-filter=U")


def check_failure_hint(ctx: Ctx, name: str, args: Optional[argparse.Namespace]) -> str:
    """What to do about a failed `check` on a sync branch - which is not the same answer
    for the two invariants it checks."""
    rc, _, _ = git_rc("merge-base", "--is-ancestor", f"{ctx.origin}/{ctx.trunk}", "HEAD",
                      cwd=ctx.root)
    if rc != 0:
        return (f"  `{ctx.origin}/{ctx.trunk}` moved on: this sync has to be redone against "
                f"the new tip - `{rerun_cmd('sync', args, ' --force')}` recreates `{name}` "
                f"(a sync MR is never rebased)")
    return f"  fix that on this branch and commit it, then: {continue_cmd('sync', args)}"


def publish(ctx: Ctx, args: argparse.Namespace, kind: str, branch: str, base: str,
            title: str, body: str) -> int:
    """The tail `finish_sync` and `finish_ship` share, from the pushed branch onwards:
    record what is pending, open the merge request, record its URL, and - with `--merge` -
    merge it and land it in this run.

    The record is written before `open_mr` and rewritten after it, so a merge request step
    that fails still leaves something `forkflow land` can finish; `write_state(..., None)`
    then says this run has nothing to resume. Only `kind`, the branch and the two texts
    differ between a sync and a ship, and every round of review that touched one of these
    tails had to touch the other.

    `report_pending` runs exactly where the user is left to finish the landing themselves -
    instead of the `next: forkflow land` line, and on the way out of a `--merge` that did
    not land - so a record that could not be written is never a "next" that cannot work."""
    entry = record_pending(ctx, kind, branch, base)   # landable even if the MR step fails
    url = open_mr(ctx, branch, title, body, bool(getattr(args, "mr", False)))
    if url:
        entry = record_pending(ctx, kind, branch, base, url)
    write_state(ctx, kind, None)                      # this run is done: nothing to resume
    if getattr(args, "merge", False):
        try:
            merge_mr(ctx, kind, branch, url)          # exit 6 leaves the record above
            land_after_merge(ctx, entry)
        except Fail:
            report_pending(ctx, entry, url)
            raise
        return 0
    if report_pending(ctx, entry, url):
        print(f"  after the MR is merged, next: {land_cmd()}")
    return 0


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
    resume = continue_cmd("sync", args)
    if not ctx.dry_run:
        try:
            ctx = replace(ctx, cfg=load_config(ctx.root))
        except Fail as exc:
            # a case variant has its own way out, above, which copies the file aside before
            # it touches it: an invitation to edit the file first is not repeated for it
            try:
                variant = config_name_in(os.listdir(ctx.root)) not in (None, CONFIG_FILE)
            except OSError:
                variant = False
            then = (f"follow the step above on `{name}`" if variant else
                    f"fix `{CONFIG_FILE}` on `{name}` and commit it")
            raise Fail(f"the merge brought a `{CONFIG_FILE}` that cannot be read: {exc}\n"
                       f"  the merge commit and `{name}` are made and the backup is on "
                       f"origin; this run checked nothing and opened no merge request\n"
                       f"  {then}, then: {resume}", 2)

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
    elif run_check(ctx, config_merged=gate_merged) == EXIT_CHECK:
        print(check_failure_hint(ctx, name, args))
        return EXIT_CHECK

    base = rev(ctx.root, f"{ctx.origin}/{ctx.trunk}")   # what the merge was built on
    try:
        push(ctx, name)
    except Fail as exc:
        if exc.code == EXIT_UNSAFE:
            print(f"  fix that, then: {resume}")
        raise
    title = (getattr(args, "title", None)
             or f"sync: {ctx.up()} {utc_stamp()} ({len(commits)} commits)")
    return publish(ctx, args, "sync", name, base, title,
                   sync_body(ctx, commits, rows, mirror_move, backup_ref))


def land_after_merge(ctx: Ctx, entry: dict) -> None:
    """`--merge`'s closing step: the request is merged, so `land` runs in this process -
    on `entry`, the record this run wrote. Read back from the shared file it could be one
    another worktree wrote in the meantime, and this run would land (or fail on) that.

    A catch-up that cannot run is its own exit 2, but the message has to open with what
    did happen - the merge request *is* merged - so nobody reads a non-zero exit as
    "nothing happened": the branch is on the trunk, and `forkflow land` finishes the rest
    once the reason is dealt with. A dry run wrote no record and `merge_mr` has already
    shown `would: land`, so there is nothing to run here. A tool that answered "merged"
    while nothing reached the trunk is `land_pending`'s exit 6, passed on as it is: that
    merge request is *not* merged, and saying it was would be the one wrong sentence."""
    if ctx.dry_run:
        return
    try:
        land_pending(ctx, after_merge=True, entry=entry)
    except Fail as exc:
        if exc.code == EXIT_NOT_MERGED:
            raise
        raise Fail(f"the merge request was merged; the local catch-up did not run: {exc}",
                   exc.code)


def sync_merge_commit(ctx: Ctx) -> str:
    """The sync merge on the branch HEAD is on, "" when there is none (or it is uncommitted).

    The merge does not have to be at HEAD: fixing what `check` refused means a commit on
    top of it, and that must not turn `--continue` into "nothing to continue". The sync
    merge is the *first* commit on the branch, so it is the last line here - `-n 1` took
    the newest instead, and a `git merge` the user made on the branch afterwards then stood
    in for it, which handed upstream's unread `gate` to `run_check` as this fork's own.
    `--first-parent` keeps merges carried in on the second-parent side of such a merge out."""
    merges = git("rev-list", "--merges", "--first-parent", "HEAD", "--not",
                 f"{ctx.origin}/{ctx.trunk}", cwd=ctx.root, check=False).split()
    return merges[-1] if merges else ""


def merge_mode_in(text: Optional[str], where: str) -> Optional[str]:
    """`merge` as a `.forkflow.toml`'s bytes set it: the default when there is no file
    (`text` is None), None when the file cannot be read. `where` names it in the error.

    The text rather than a revision, because the caller that has to know whose file it is
    reads the same text for that (`written_by_upstream`), and two `git show`s of one path
    are two answers about two different files the moment the ref moves between them."""
    if text is None:
        return MERGE_MANUAL
    try:
        cfg = parse_config(text, where)
    except Fail:
        return None
    return cfg.get("merge") or MERGE_MANUAL


def config_fingerprint(text: str) -> str:
    """A `.forkflow.toml`'s bytes as provenance compares them.

    Line endings and trailing whitespace are normalised away: a checkout that rewrote the
    line endings, or an editor that dropped the last newline, is not this fork *writing*
    the file, and reading it as one would open the `--merge` gate on upstream's settings.
    Everything else is compared literally - one character of the fork's own is what makes
    the file the fork's.

    This is also why `text` and `eol` are not among `CONFIG_RENDER_ATTRS`: both do line
    endings and nothing else, so both are undone here before anything is compared."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in body.splitlines()).strip("\n")


def config_digest(text: str) -> str:
    """A `.forkflow.toml`'s bytes as provenance compares them, and as the state file writes
    them down: a sha256 over `config_fingerprint`'s normalised form, so what is remembered
    is a hash and never anybody's file."""
    return hashlib.sha256(config_fingerprint(text).encode("utf-8")).hexdigest()


def remembered_upstream_configs(ctx: Ctx) -> list:
    """The `config_digest`s of the original project's `.forkflow.toml`s this clone has
    written down, oldest first ({} entries when it has written down none)."""
    kept = read_state(ctx, shared=True).get("upstream_configs")
    return [d for d in (kept if isinstance(kept, list) else []) if isinstance(d, str)]


def remember_upstream_configs(ctx: Ctx, digests: set) -> Tuple[set, str]:
    """Write down each of `digests` this clone has not written down before, and answer
    everything it now remembers - or why it could not be written, which is a refusal.

    YOU CANNOT PROVE ABSENCE FROM A HISTORY YOU NO LONGER HOLD. Upstream force-pushes the
    branch, withdraws it, or this fork prunes, and the version that reached this working
    tree is gone from every ref a walk can reach while the rest of upstream's history is
    still there - so the walk's answer is short by exactly the version in question, the
    empty-baseline refusal does not fire, and upstream's own bytes read as this fork's.
    Nothing read out of the repository closes that, because the repository is the thing
    that no longer holds it.

    So the fork REMEMBERS, in the one place upstream cannot write - `.git/forkflow-state.json`,
    through the one locked writer (`change_state`), as hashes and never as contents. What
    was once the original project's stays the original project's, and a withdrawal changes
    nothing at all.

    Only what `foreign_remote_refs` carried is written down. That is the frame upstream
    cannot choose; the refs the config names widen the answer for the run that reads them
    and are left out of the memory on purpose, because a config naming this fork's own
    trunk would otherwise write the fork's own bytes in for good.

    A fork that ADOPTS a config of upstream's and then edits it is not trapped: edited bytes
    are different bytes with a different digest, in no remembered set, and that is the way
    back every refusal here prints.

    NOTHING IS EVER DROPPED TO MAKE ROOM. The memory used to be bounded oldest-first-out at
    `UPSTREAM_CONFIG_KEEP`, and that handed the eviction to the attacker: HOW MANY versions
    get published is the original project's choice, not this fork's. Publish enough of them
    and the digest of the version sitting untracked in this working tree falls out, then
    withdraw that version from every ref - the walk's answer is not empty so no fail-closed
    condition fires, and upstream's own file reads as this fork's own (scratchpad
    `f10/repro15.py`, with the bound lowered: 500 commits is the same attack with more
    typing).

    So the bound still exists - a state file upstream can grow without end is its own
    problem - but reaching it FAILS CLOSED rather than forgetting. `CONFIG_MEMORY_FULL` goes
    into the state file, `config_memory_unprovable` reads it on every later run, and
    `--merge` is refused here from then on with the way that needs no memory at all: have
    the merge request merged by hand. 500 distinct versions of one small file is a number no
    real project comes near, and a fork that somehow does loses an opt-in flag, not its work.

    What is remembered is what the tool has SEEN. A version that reached this clone and left
    it again with no provenance walk in between was never seen here - which is why the walk
    runs twice per `--merge` run, before the push and again after the run's own fetch.

    A dry run writes nothing (the no-write contract) and answers with the memory plus what
    was just walked."""
    kept = remembered_upstream_configs(ctx)
    known = set(kept)
    fresh = [d for d in sorted(digests) if d not in known]
    if len(kept) + len(fresh) > UPSTREAM_CONFIG_KEEP:
        if ctx.dry_run:                             # a dry run answers, and writes nothing
            return (set(), memory_full_refusal(ctx))
        failed = change_state(ctx, True, lambda data: data.update({CONFIG_MEMORY_FULL: True}))
        return (set(), failed or memory_full_refusal(ctx))
    if not fresh or ctx.dry_run:
        return (known | set(digests), "")

    def change(data: dict) -> None:
        now = [d for d in (data.get("upstream_configs") or []) if isinstance(d, str)]
        here = set(now)
        now.extend(d for d in fresh if d not in here)
        if len(now) > UPSTREAM_CONFIG_KEEP:   # another run wrote between the read and here
            data[CONFIG_MEMORY_FULL] = True   # - nothing is dropped, the memory is full
            now = now[:UPSTREAM_CONFIG_KEEP]
        data["upstream_configs"] = now

    why = change_state(ctx, True, change)
    if not why and read_state(ctx, shared=True).get(CONFIG_MEMORY_FULL):
        return (set(), memory_full_refusal(ctx))   # another run filled it between the two
    if why:
        return (set(), f"this clone cannot write down which `{CONFIG_FILE}`s the original "
                       f"project has ({why}), and a version it has since stopped being able "
                       f"to see in any ref would then read as one the project never had. "
                       f"Nothing else is refused: fix that and run this again, or run the "
                       f"same command without `--merge` and have the merge request merged "
                       f"by hand")
    return (known | set(digests), "")


def memory_full_refusal(ctx: Ctx) -> str:
    """Why `--merge` is refused once this clone has written down as many of the original
    project's `.forkflow.toml`s as it will hold - the same words whichever run finds it."""
    return (f"this clone has written down {UPSTREAM_CONFIG_KEEP} different `{CONFIG_FILE}`s "
            f"of the original project's, which is as many as it holds, and it will not drop "
            f"one to make room: how many versions get published is the original project's "
            f"choice, and a version dropped is a version that would read as one the project "
            f"never had. So `--merge` is refused here from now on. Run the same command "
            f"without `--merge` and have the merge request merged by hand - that needs no "
            f"memory at all. The record is `{state_path(ctx, shared=True) or STATE_FILE}`; "
            f"deleting it starts this clone's memory over and loses every version written "
            f"down so far, which is what the refusal is protecting")


def config_memory_unprovable(ctx: Ctx) -> str:
    """"" when this clone's memory of the original project's `.forkflow.toml`s can be
    trusted, otherwise why it cannot - and then `--merge` is REFUSED.

    The memory (`remember_upstream_configs`) is the only thing that answers a version
    upstream has since withdrawn from every ref, so a memory that has been made to forget is
    an open gate. Two ways it can have been:

    - the state file is there and cannot be READ - truncated by an interrupted write,
      half-written by a full disk, edited by hand into something that is not an object, or
      unreadable outright. That used to answer `{}` and nothing else: the memory was simply
      gone, in silence, and upstream's own file - brought in by a sync, untracked by hand,
      and withdrawn upstream since - read as this fork's own with `merge = "self"` in it
      (scratchpad `f10/repro15.py`). The refusal names the file and says what deleting it
      costs, because deleting it is a real choice and it is the user's to make;
    - `upstream_configs` is there but is not a list of strings. Same fact, by hand.
    - the memory is FULL (`CONFIG_MEMORY_FULL`, `memory_full_refusal`).

    Only `--merge` is refused. Everything else in this tool goes on working with an
    unreadable state file: `read_state` answers `{}` on purpose, a `land` that finds no
    `pending` says so, and `change_state` refuses to write over the file rather than making
    the damage permanent. A corrupt state file is a reason not to trust a provenance answer,
    not a reason for the tool to stop."""
    why = state_unreadable(ctx, shared=True)
    if why:
        return (f"{why}, and that file is where this clone writes down which `{CONFIG_FILE}`s "
                f"the original project has had. Without it a version the project has since "
                f"withdrawn from every branch reads as one it never had, so `--merge` cannot "
                f"be proven here. Look at that file; if you accept losing every record in it "
                f"you can delete it, and this clone starts remembering again from what its "
                f"refs carry today. Or run the same command without `--merge` and have the "
                f"merge request merged by hand")
    data = read_state(ctx, shared=True)
    if data.get(CONFIG_MEMORY_FULL):
        return memory_full_refusal(ctx)
    kept = data.get("upstream_configs")
    if kept is not None and not (isinstance(kept, list)
                                 and all(isinstance(d, str) for d in kept)):
        return (f"`{state_path(ctx, shared=True)}` holds an `upstream_configs` that is not a "
                f"list of digests, so what this clone has written down of the original "
                f"project's `{CONFIG_FILE}`s cannot be read and a version the project has "
                f"since withdrawn would read as one it never had. Put that key back as a "
                f"list of strings, or delete the file and accept losing every record in it - "
                f"or run the same command without `--merge` and have the merge request "
                f"merged by hand")
    return ""


def foreign_remote_refs(ctx: Ctx) -> list:
    """Every remote-tracking ref that is not `origin`'s.

    The one frame in this clone the original project cannot write, and so the basis of
    every provenance answer. Remotes are local git config: upstream cannot add one here,
    cannot move a ref under one, and cannot make this clone forget one. `origin` is the
    fork itself and stays out - its branches carry the fork's OWN configs, which is what
    upstream's are compared against.

    A broken symref (`<remote>/HEAD` after its target is deleted) is not listed by
    `for-each-ref` at all, so nothing here has to filter one out."""
    ours = f"refs/remotes/{ctx.origin}/"
    listing = git("for-each-ref", "--format=%(refname)", "refs/remotes/",
                  cwd=ctx.root, check=False)
    return [ref for ref in listing.splitlines() if ref and not ref.startswith(ours)]


def upstream_scope_refs(ctx: Ctx) -> list:
    """The refs `upstream_config_digests` walks - chosen without asking the `.forkflow.toml`
    whose provenance is the whole question.

    The scope used to be the refs the CONFIG names: `<upstream>/<branch>` and the mirror,
    both read off the working tree's `.forkflow.toml`. That let the file being judged
    choose the evidence against it - upstream's own config naming an `upstream_branch` of
    upstream's that never carried a config, and a `mirror` naming the fork's trunk, left
    the comparison set empty, so upstream's bytes matched nothing and read as the fork's
    own. That was the seventh route into the `--merge` gate, and the lesson of all seven:
    what the gate depends on has to come from somewhere upstream cannot write.

    So the baseline is the frame upstream cannot write - `foreign_remote_refs`. On top of
    it, three additions that can only WIDEN the set: the mirror on origin and here (rule 6
    keeps it a pristine copy of upstream; its name is the config's, which is why it is an
    addition and never a subtraction), the same for the upstream branch's name, and
    `MERGE_HEAD` while a sync merge is being resolved.

    A SUPERSET is the safe direction. An extra ref can only make MORE bytes count as
    upstream's, never fewer: the cost is a refusal, and every refusal here prints the same
    way back - one character of the fork's own in the file makes the file the fork's."""
    refs = foreign_remote_refs(ctx)
    seen = set(refs)

    def add(ref: str) -> None:
        if ref not in seen and has_ref(ctx.root, ref):
            seen.add(ref)
            refs.append(ref)

    for name in (ctx.mirror, ctx.upstream_branch):
        add(f"refs/remotes/{ctx.origin}/{name}")
        add(f"refs/heads/{name}")
    if merge_in_progress(ctx.root):
        refs.append("MERGE_HEAD")
    return refs


def history_unprovable(ctx: Ctx) -> str:
    """"" when this clone can be read for what `.forkflow.toml`s the original project has
    had, otherwise why it cannot - and then `--merge` is REFUSED.

    A version the walk cannot see is not a version upstream never had, and the difference
    decides whether upstream's own file reads as this fork's. Each of these hides history:

    - a SHALLOW clone simply does not have the commits before its cut;
    - a PARTIAL clone has the commits and not the blobs, fetched on demand - and the fetch
      that would get one needs a server this run may not be able to reach;
    - `refs/replace/*` and a grafts file make git answer with a history that is not the one
      the original project published;
    - and a clone with no remote-tracking ref outside `origin` (`foreign_remote_refs`) holds
      no history of the original project's at all, so the only refs left to walk would be
      the ones the config names - which is the hole `upstream_scope_refs` exists to close.

    Every one of them turns "upstream never had these bytes" into a lie, and that lie opens
    the gate on upstream's `merge` and so on upstream's `gate`, which this tool runs as
    shell. So each is a refusal with the condition named and a way out of it: the clone can
    be completed, or the merge request can be merged by a person, which is what a fork
    without `merge = "self"` does anyway."""
    if not foreign_remote_refs(ctx):
        return (f"this clone holds no remote-tracking ref outside `{ctx.origin}` - nothing "
                f"of the original project's is fetched here, so there is nothing to compare "
                f"a `{CONFIG_FILE}` against: `git fetch {sh_arg(ctx.upstream)}`, then run "
                f"this again")
    if git("rev-parse", "--is-shallow-repository", cwd=ctx.root, check=False) == "true":
        return (f"this clone is shallow, so the `{CONFIG_FILE}` versions before its cut are "
                f"not in it to compare against - `git fetch --unshallow "
                f"{sh_arg(ctx.upstream)}`, then run this again")
    replaced = [r for r in git("for-each-ref", "--format=%(refname)", "refs/replace/",
                               cwd=ctx.root, check=False).splitlines() if r]
    if replaced:
        return (f"{len(replaced)} `refs/replace/*` entry/entries rewrite what this clone's "
                f"history shows (`git replace -l` lists them), so what a walk of it reads "
                f"is not what the original project published")
    grafts = git_path(ctx.root, os.path.join("info", "grafts"))
    if grafts and os.path.exists(grafts):
        return (f"`{grafts}` grafts this clone's history, so what a walk of it reads is not "
                f"what the original project published")
    partial = git("config", "--get", "extensions.partialclone", cwd=ctx.root, check=False)
    promisors = [ln.split()[0] for ln in
                 git("config", "--get-regexp", r"^remote\..*\.promisor$",
                     cwd=ctx.root, check=False).splitlines()
                 if ln.split()[-1:] == ["true"]]
    which = (f"extensions.partialclone={partial}" if partial
             else ", ".join(f"{key}=true" for key in promisors))
    if partial or promisors:
        return (f"this clone is partial ({which}): objects are fetched on demand, so a "
                f"`{CONFIG_FILE}` the original project had can be absent here and read as "
                f"one it never had - clone it again without `--filter` (git 2.36 and "
                f"newer can also refill one in place), or have the merge request merged "
                f"by hand")
    return ""


def config_versions_in(ctx: Ctx, refs: Sequence[str]) -> Tuple[set, str]:
    """(every `.forkflow.toml` any of `refs` has EVER carried, as `config_digest`s; "" - or
    why this clone cannot be read for them, which is a refusal wherever it is asked).

    HISTORIES, not tips. For each ref every version of the file its history has carried,
    not only the one at the tip.

    Sampling the tips was the first shape of this and it left the gate open on a delay.
    Upstream's file, brought in by a sync and left on disk untracked, was refused while
    upstream still had those bytes at a tip; once upstream edited its own config and the
    mirror moved past, the stale bytes in the working tree matched nothing sampled and a
    file this fork never wrote read as "the fork's own". A version upstream has retired is
    still upstream's, however long ago it retired it, so the question is asked of the whole
    history.

    Two git calls per REF read, plus one per DISTINCT version ever committed: `rev-list
    --objects --full-history <refs> -- :(icase)<name>` lists the commits that changed the
    path and, beside each, the blob it holds there, so the versions are read once each
    however many commits carry them; `--full-history` follows every parent of a merge, so a
    version that exists only on a side branch or only in a merge's own resolution is listed
    too. The pathspec keeps the walk's output to the file and the file alone, so the width
    of the scope costs far less than it looks: 100,000 commits over 17 refs and four
    remotes, 200 distinct versions of the file, is 1.0s, of which the walk itself is 30ms
    and the rest is one `cat-file` per version. `--merge` runs reach this and nothing else
    does, twice each (the gate, then `merge_mr` after the fetch).

    The tips are read separately, and by name rather than through `config_text`: `:(icase)`
    matches ASCII case only, so a tip spelling the name with a character that merely
    case-FOLDS to one of these (the Kelvin sign) is caught here. In a history such a
    spelling is not - `load_config` refuses to read any such file in the working tree, so
    it cannot be the file whose provenance is in question.

    FAIL CLOSED, everywhere the reading can fail. A blob that cannot be read, a tree that
    cannot be listed, a walk git refuses - none of them is evidence that upstream never had
    those bytes, and treated as absence each one is an open gate. So each answers with the
    reason instead of a short set, on top of the whole-repository conditions
    `history_unprovable` names."""
    if not refs:
        return (set(), "")
    fold = CONFIG_FILE.casefold()
    digests = set()
    for revision in refs:
        name = config_name_at(ctx, revision)  # None: no file in its tree; the exact name
        if name is None:                      # when the tree cannot be listed, so `show` says
            continue
        rc, text, err = git_rc("show", f"{revision}:{name}", cwd=ctx.root)
        if rc != 0:
            why = tail_lines(err, 1)
            return (set(), f"`{revision}:{name}` is in this clone's refs and cannot be read "
                           f"({why[0] if why else 'git gave no reason'}) - a version the "
                           f"original project has may be missing here; complete the clone, "
                           f"or have the merge request merged by hand")
        digests.add(config_digest(text))
    rc, listing, err = git_rc("rev-list", "--objects", "--full-history", *refs, "--",
                              f":(icase){CONFIG_FILE}", cwd=ctx.root)
    if rc != 0:
        why = tail_lines(err, 1)
        return (set(), f"this clone's history cannot be walked for `{CONFIG_FILE}` "
                       f"({why[0] if why else 'git gave no reason'}) - complete the clone, "
                       f"or have the merge request merged by hand")
    blobs = set()
    for line in listing.splitlines():
        sha, _, name = line.partition(" ")
        if name.casefold() == fold:           # commits and trees come with no name at all
            blobs.add(sha)
    for sha in sorted(blobs):
        rc, text, _ = git_rc("cat-file", "blob", sha, cwd=ctx.root)
        if rc != 0:
            return (set(), f"the `{CONFIG_FILE}` this clone's history lists at {short(sha)} "
                           f"cannot be read - the object is not here (a filtered or damaged "
                           f"clone), and a version the original project had would be read "
                           f"as one it never had; complete the clone, or have the merge "
                           f"request merged by hand")
        digests.add(config_digest(text))
    return (digests, "")


def config_path_names(root: str, listed: Sequence[str]) -> list:
    """Every path in this working tree that answers to the config's name: the name itself,
    any case variant beside it on disk, and every variant git tracks.

    A case-insensitive filesystem makes them one file, so an attribute set on
    `.ForkFlow.toml` reaches the file forkflow reads - which is why the question is asked of
    all of them and not of `.forkflow.toml` alone."""
    fold = CONFIG_FILE.casefold()
    return sorted({CONFIG_FILE} | {n for n in listed if n.casefold() == fold}
                  | set(tracked_config_names(root)))


def config_render_unprovable(ctx: Ctx) -> str:
    """"" when nothing is rendering the `.forkflow.toml` in this working tree RIGHT NOW,
    otherwise why the file read from it is not the same kind of thing as the blobs it would
    be compared against - and then `--merge` is REFUSED.

    Asked only where the decision reads the working tree (`fork_config_state`), never for a
    config committed on the trunk, which is read as a blob and which no attribute can reach.

    What is set now is HALF the question, and the other half is two other functions':
    `config_rendered_before` (these bytes were judged rendered here once, and turning the
    attribute off does not un-render the file on disk) and `config_history_render_unprovable`
    (the original project's history ever rendered this path, so an untracked file at it can
    be one git wrote).

    The comparison has two sides and they are not the same kind of thing by themselves: one
    is the file as this working tree renders it (`working_config_text`, a plain read of the
    path), the other is the blob as git STORES it (`cat-file blob`). In an ordinary clone
    those are the same bytes. The repository can make them differ, and the repository is
    something the original project writes:

    - a SYMLINK under the config's name. What git stores there is the link TARGET, a short
      path; what reading the path gives is another file's contents. Upstream keeping its
      settings in `theirs.toml` and a link at `.forkflow.toml` means every blob upstream
      ever stored under that name is the string "theirs.toml", so upstream's own settings -
      read through the link - match nothing and pass as this fork's own.
    - a `.gitattributes` upstream controls. `filter` runs a program over the file on
      checkout, `ident` expands `$Id$` into the blob's own hash, and
      `working-tree-encoding` holds the working tree in another encoding entirely. Each one
      makes the rendered file differ from every blob that could have produced it, in a way
      nothing on the reading side puts back.

    Both were reproduced against the gate (scratchpad `f9/repro1.py`): upstream's config,
    with `merge = "self"` and a `gate` in it, read as `untracked_own` and opened `--merge`.

    `text`, `eol` and `diff` are deliberately NOT refused, and that matters more than it
    looks: `* text=auto` is in an enormous number of repositories, and refusing it would
    take `--merge` away from ordinary forks that are doing nothing unusual at all. They are
    safe for two different reasons.

    - `text` and `eol` change LINE ENDINGS and nothing else, and both sides of the
      comparison already have their line endings taken off them: `working_config_text`
      reads the file in text mode, where Python turns CRLF and CR into LF before anything
      here sees it, and `config_fingerprint` does the same to the blob. Upstream's own
      config, checked out through `eol=crlf` with CRLF really on disk, still compares equal
      to the blob it came from and is still caught as upstream's - measured both ways
      round (LF stored / CRLF on disk, and CRLF stored / LF on disk) in scratchpad
      `f10/repro4.py` and held by
      `test_line_endings_are_normalised_so_text_and_eol_are_safe_to_allow`.
    - `diff` names a diff driver, and a diff driver's `textconv` is run to produce DIFF
      OUTPUT. It is never part of a checkout, so the bytes on disk are the blob's either
      way - also measured in `f10/repro4.py`, with a `textconv` that rewrites `self`.

    If either reason ever stops holding - a `text` that did more than line endings, a
    `diff` that reached the working tree - the attribute belongs back in
    `CONFIG_RENDER_ATTRS`, and the tests above are what would say so.

    forkflow does not try to REPRODUCE git's rendering rules to compare like with like -
    that is a moving target, and a version of it that is subtly wrong is an open gate. It
    refuses, which is the direction this gate has taken at every other turn. Both conditions
    are ones an ordinary fork never meets, both are ones a user can end, and the refusal
    names the link or the attribute so they can.

    NEITHER REMEDY PRODUCES CONFIG BYTES, and that is the correction of what both of them
    did when they were first written. The symlink one ended `cp <the copy> .forkflow.toml`,
    so the link target's contents became a real config; the attribute one turned the
    attribute off for future reads and left the already-converted file exactly where it
    was. Run as printed, each one turned the original project's `merge = "self"` - with the
    original project's `gate` behind it - into this fork's own declaration, because the
    bytes that landed at the config's path were bytes no `.forkflow.toml` blob has ever
    held and so matched nothing in `upstream_config_digests` (reproduced in scratchpad
    `f10/repro23.py`: `unprovable` before the remedy, `untracked_own` and `self` after it).
    So both remedies now have the same shape - take the obstacle away, copy what was there
    aside into the git directory (`keep_aside`) and name the copy, and leave the user to
    WRITE THEIR OWN config. `test_the_symlink_remedy_leaves_no_config_behind` and
    `test_the_attribute_remedy_leaves_no_config_behind` run each one exactly as printed and
    say what the fork looks like afterwards.

    Asked of every path that case-folds to the config's name - the one on disk, any case
    variant beside it, and every variant git tracks - because a case-insensitive filesystem
    makes them one file, and an attribute set on `.ForkFlow.toml` would otherwise reach the
    file forkflow reads without being looked at. Failing to read the attributes at all is a
    refusal too: it is not evidence that none is set."""
    root = ctx.root
    try:
        listed = os.listdir(root)
    except OSError as exc:
        return (f"`{root}` cannot be listed to see what `{CONFIG_FILE}` is ({exc.strerror or exc})")
    names = config_path_names(root, listed)
    for name in names:
        full = os.path.join(root, name)
        if not os.path.islink(full):
            continue
        try:
            target = os.readlink(full)
        except OSError:
            target = "?"
        save, keep = keep_aside(root, name)
        return (f"`{name}` is a symbolic link (to `{target}`), so what git stores under that "
                f"name is the link's target and what reading the path gives is another "
                f"file's contents - two different things, and comparing them reads the "
                f"original project's own config as one it never had. Take the link away: "
                f"`{save} && rm -- {sh_arg(name)}` copies what the link reads as now into "
                f"`{keep}` first and then removes the link - nothing of yours is lost, and "
                f"the file the link pointed at is left alone. Then WRITE YOUR OWN "
                f"`{CONFIG_FILE}` there. Nothing puts one back for you, and that is "
                f"deliberate: the bytes on the other end of that link are whatever the file "
                f"it points at holds, no `{CONFIG_FILE}` git stores has ever been those "
                f"bytes, and a config made out of them would read as this fork's own "
                f"declaration while being the original project's settings. `{keep}` is there "
                f"to read, not to copy back")
    rc, out, err = git_rc("check-attr", "-z", *CONFIG_RENDER_ATTRS, "--", *names, cwd=root)
    if rc != 0:
        why = tail_lines(err, 1)
        return (f"git cannot say what attributes are set on `{CONFIG_FILE}` "
                f"({why[0] if why else 'git gave no reason'}), and an attribute that rewrites "
                f"the file between the working tree and the object store would make the "
                f"original project's own config read as one it never had")
    fields = out.split("\0")
    for i in range(0, len(fields) - 2, 3):
        where, attr, value = fields[i], fields[i + 1], fields[i + 2]
        if value in ("unspecified", "unset"):
            continue                              # not set, or set OFF: nothing is rendered
        attrs = git_path(root, os.path.join("info", "attributes"))
        off = f"{where} " + " ".join("-" + a for a in CONFIG_RENDER_ATTRS)
        turn_off = f"printf '%s\\n' {sh_arg(off)} >> {sh_arg(attrs)}"
        why = (f"`{attr}` is set on `{where}` (to `{value}`), by a `.gitattributes` this "
               f"fork does not have to own, and git renders a file with that attribute "
               f"differently from the bytes it stores it as - so the config read here and "
               f"every stored `{CONFIG_FILE}` are two different kinds of thing, and the "
               f"original project's own config can compare as bytes it never stored. "
               f"`git check-attr -a -- {sh_arg(where)}` shows every attribute on it. Two "
               f"things end it, and neither of them writes a `{CONFIG_FILE}`. First, turn "
               f"the attribute off for that one path in a file only this clone has: "
               f"`{turn_off}` - git reads `info/attributes` in the git directory before any "
               f"`.gitattributes` in the tree, and nothing of yours is overwritten.")
        if CONFIG_FILE not in listed:
            return why + (f" Second, there is nothing to undo on disk - no `{CONFIG_FILE}` "
                          f"is there - so write your own, and `--merge` reads that")
        save, keep = keep_aside(root, CONFIG_FILE)
        tracked = (f" Git tracks a `{CONFIG_FILE}` here, so the removal shows as a deletion "
                   f"until your own file is in its place: commit that on a branch off "
                   f"`origin/{remote_trunk(root)}` and ship it, never on the trunk itself."
                   if tracked_config_names(root) else "")
        return why + (f" Second, take the file that attribute already rewrote OFF disk: "
                      f"`{save} && rm -- {CONFIG_FILE}` copies what is there now into "
                      f"`{keep}` first. Turning the attribute off fixes what git does NEXT "
                      f"time; the file sitting there was written by git's conversion and "
                      f"not by this fork, and while it stays it reads as this fork's own "
                      f"with the original project's settings in it. Nothing puts one back "
                      f"for you: WRITE YOUR OWN `{CONFIG_FILE}`, and `--merge` reads that. "
                      f"`{keep}` is there to read, not to copy back.") + tracked
    return ""

def remembered_rendered_configs(ctx: Ctx) -> list:
    """The `config_digest`s of working-tree `.forkflow.toml`s this clone has judged
    unprovable for a RENDERING reason ([] when it has judged none)."""
    kept = read_state(ctx, shared=True).get(RENDERED_CONFIGS)
    return [d for d in (kept if isinstance(kept, list) else []) if isinstance(d, str)]


def remember_rendered_config(ctx: Ctx, text: Optional[str]) -> str:
    """Write down that the file at the config's path, as it reads now, was judged unprovable
    because something renders it. "" when there was nothing to write down or it was written;
    otherwise why it could not be, which the refusal that called this then says out loud.

    THE ATTRIBUTE IS A FACT ABOUT NOW AND THE CONVERSION HAPPENED AT CHECKOUT. `check-attr`
    answers what is set this second; the file git already converted stays exactly where it
    is once the attribute stops being reported. So the refusal could be ended with the
    converted file still on disk, two ways, neither of them needing anything clever:

    - the user runs only the FIRST of the two commands the refusal prints - the one the
      message itself labels "First,", which turns the attribute off for that path in
      `info/attributes`. `check-attr` then says `unset`, the loop passes over it, NO refusal
      is produced, and the `ident`-expanded bytes of the original project's config read as
      `untracked_own` with `merge = "self"` in them (scratchpad `q1/render2.py` case A;
      `q1/E4.sh` runs it end to end and upstream's `gate` runs as shell);
    - the original project deletes the `.gitattributes` that set it and the fork syncs. The
      converted file is untracked, so the sync leaves it there and takes the attribute away
      (`q1/render2.py` case B, `q1/E3.sh`). No user action at all beyond the untracking.

    A point-in-time question cannot answer "were these bytes produced by a conversion", so
    the VERDICT is remembered rather than re-derived: these BYTES were judged unprovable in
    this clone, and nothing set or unset afterwards changes what they are. A digest, never
    the file, in the one place the original project cannot write (the state file, through
    `change_state`).

    The way back is the way back everywhere else in this gate - the bytes decide. A file
    with one character of the fork's own in it is a different digest, in no remembered set,
    and provable again. That is what keeps the printed remedy working, because the remedy
    takes the converted file OFF disk and leaves the user to write their own; a HALF-followed
    remedy, which leaves that file exactly where it was, stays refused.

    Nothing here is the original project's to grow: a digest is written only when this clone
    refuses, and only about the one file in this working tree. Compare
    `remember_upstream_configs`, whose size IS the project's choice, which is why that one
    has a bound and this one needs none."""
    if text is None or ctx.dry_run:      # nothing on disk / the no-write contract
        return ""
    digest = config_digest(text)
    if digest in remembered_rendered_configs(ctx):
        return ""

    def change(data: dict) -> None:
        now = [d for d in (data.get(RENDERED_CONFIGS) or []) if isinstance(d, str)]
        if digest not in now:
            now.append(digest)
        data[RENDERED_CONFIGS] = now

    why = change_state(ctx, True, change)
    return (f"; this clone could not write that verdict down ({why}), so ending the "
            f"condition would end this refusal with that same file still on disk" if why
            else "")


def config_rendered_before(ctx: Ctx) -> str:
    """"" unless the file at the config's path holds bytes this clone has ALREADY judged
    unprovable for a rendering reason - and then `--merge` is REFUSED, whatever
    `check-attr` says today. `remember_rendered_config` is why."""
    text = working_config_text(ctx)
    if text is None or config_digest(text) not in remembered_rendered_configs(ctx):
        return ""
    save, keep = keep_aside(ctx.root, CONFIG_FILE)
    return (f"the `{CONFIG_FILE}` in this working tree holds bytes this clone has already "
            f"judged unprovable because something rendered them - a `filter`, `ident` or "
            f"`working-tree-encoding` attribute on that path, or a symbolic link at it - and "
            f"the verdict is written down in "
            f"`{state_path(ctx, shared=True) or STATE_FILE}`. An attribute is a fact about "
            f"NOW and the conversion happened at CHECKOUT, so turning it off - or the "
            f"original project deleting the `.gitattributes` that set it - leaves that file "
            f"exactly where it was: this is a verdict about these bytes, not about what is "
            f"set today. Take the file off disk: `{save} && rm -- {CONFIG_FILE}` copies what "
            f"is there now into `{keep}` first. Then WRITE YOUR OWN `{CONFIG_FILE}` - "
            f"nothing puts one back for you - and `--merge` reads that; one character of "
            f"your own makes it a different file. `{keep}` is there to read, not to copy "
            f"back. Or run the same command without `--merge` and have the merge request "
            f"merged by hand")


def attrs_render_the_config(ctx: Ctx, refs: Sequence[str]) -> Tuple[str, str]:
    """(what ever set a rendering attribute on the config's path anywhere in the histories
    of `refs`, "" when nothing ever did; or ("", why this clone cannot be asked that).

    The question `config_render_unprovable` asks of the working tree, asked of HISTORY the
    way `config_versions_in` asks about the config - because "is the attribute set now"
    cannot answer "were these bytes produced by a conversion". A `.gitattributes` the
    original project has since deleted converted every file checked out under it, and those
    files are still on disk.

    Only a ROOT `.gitattributes` can reach a file at the root, and the rooted pathspec lists
    exactly those, so nothing under a directory has to be read.

    Asked with GIT'S OWN MATCHER rather than one written here: each version is laid down as
    the only `.gitattributes` of an empty scratch repository and `check-attr` is asked there.
    Patterns, macros, precedence and case are then git's answer instead of a
    re-implementation of them - and a re-implementation that was subtly wrong would either
    refuse ordinary forks (`*.png filter=lfs` must not) or miss a real one (`*.toml ident`
    must not). The scratch tree is empty and its `core.attributesFile` points at nothing, so
    what is answered is what THAT version says and nothing else: not this clone's
    `info/attributes` (which the printed remedy appends to) and not the working tree's own
    `.gitattributes`.

    FAIL CLOSED wherever the reading can fail - a walk git refuses, a blob that is not here,
    a scratch repository that cannot be made. None of them is evidence that the original
    project never set one."""
    if not refs:
        return ("", "")
    fold = ATTRS_FILE.casefold()
    rc, listing, err = git_rc("rev-list", "--objects", "--full-history", *refs, "--",
                              f":(icase){ATTRS_FILE}", cwd=ctx.root)
    if rc != 0:
        tail = tail_lines(err, 1)
        return ("", f"this clone's history cannot be walked for `{ATTRS_FILE}` "
                    f"({tail[0] if tail else 'git gave no reason'}) - complete the clone, or "
                    f"have the merge request merged by hand")
    blobs = set()
    for line in listing.splitlines():
        sha, _, name = line.partition(" ")
        if name.casefold() == fold:           # commits and trees come with no name at all
            blobs.add(sha)
    if not blobs:
        return ("", "")
    try:
        listed = os.listdir(ctx.root)
    except OSError:
        listed = []
    names = config_path_names(ctx.root, listed)
    scratch = tempfile.mkdtemp(prefix="forkflow-attrs-")
    try:
        rc, _, err = git_rc("init", "-q", scratch)
        if rc != 0:
            tail = tail_lines(err, 1)
            return ("", f"the `{ATTRS_FILE}` versions this clone's history carries cannot be "
                        f"read for what they set on `{CONFIG_FILE}` "
                        f"({tail[0] if tail else 'git gave no reason'})")
        for sha in sorted(blobs):
            rc, text, _ = git_rc("cat-file", "blob", sha, cwd=ctx.root)
            if rc != 0:
                return ("", f"the `{ATTRS_FILE}` this clone's history lists at {short(sha)} "
                            f"cannot be read - the object is not here (a filtered or damaged "
                            f"clone), and an attribute the original project set would read "
                            f"as one it never set; complete the clone, or have the merge "
                            f"request merged by hand")
            with open(os.path.join(scratch, ATTRS_FILE), "w", encoding="utf-8",
                      newline="") as fh:
                fh.write(text)
            rc, out, err = git_rc("-c", "core.attributesFile="
                                  + os.path.join(scratch, "no-such-attributes"),
                                  "check-attr", "-z", *CONFIG_RENDER_ATTRS, "--", *names,
                                  cwd=scratch)
            if rc != 0:
                tail = tail_lines(err, 1)
                return ("", f"git cannot say what the `{ATTRS_FILE}` at {short(sha)} sets on "
                            f"`{CONFIG_FILE}` "
                            f"({tail[0] if tail else 'git gave no reason'})")
            fields = out.split("\0")
            for i in range(0, len(fields) - 2, 3):
                where, attr, value = fields[i], fields[i + 1], fields[i + 2]
                if value in ("unspecified", "unset"):
                    continue                  # not set, or set OFF: nothing is rendered
                return (f"`{attr}` is set on `{where}` (to `{value}`) by the `{ATTRS_FILE}` "
                        f"this clone's history carries at {short(sha)}", "")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return ("", "")


def config_history_render_unprovable(ctx: Ctx) -> str:
    """"" when no `.gitattributes` in the history this clone holds of the original project's
    ever rendered the config's path, otherwise why an UNTRACKED config cannot answer for
    `merge` here - and then `--merge` is REFUSED.

    An untracked `.forkflow.toml` is the one state where a file git itself wrote can pass
    for one this fork wrote: git converts a file it CHECKS OUT, the fork then untracks it by
    hand (`git rm --cached`, committed on a sync branch), and what is left on disk is byte
    for byte the shape `setup` leaves this fork's own template in. Once the attribute is
    gone - turned off, or deleted upstream - nothing on disk tells the two apart, and
    converted bytes are in no upstream digest set, so they read as this fork's own
    declaration with the original project's `merge` and the original project's `gate` in
    them.

    A config COMMITTED on the trunk is exposed to none of this: it is read with
    `git show <origin/trunk>:<name>`, blob against blobs, and the working tree is never
    consulted (`fork_config_state`). That is why the way forward this prints is a real one
    and not a shrug - and it is the state this tool recommends anyway."""
    found, why = attrs_render_the_config(ctx, upstream_scope_refs(ctx))
    if why:
        return why
    if not found:
        return ""
    return (f"{found}, and git renders a file with that attribute differently from the bytes "
            f"it stores it as. The attribute does not have to be set NOW: the conversion "
            f"happens at CHECKOUT and the converted file stays when the `{ATTRS_FILE}` goes "
            f"- so an untracked `{CONFIG_FILE}` in this working tree can be one git wrote "
            f"out of the original project's own blob rather than one you wrote, and nothing "
            f"on disk tells the two apart. While that is so, an untracked config cannot "
            f"answer for `merge` here. One that CAN: a `{CONFIG_FILE}` committed on "
            f"`{ctx.origin}/{ctx.trunk}` - that one is read as a blob, straight out of the "
            f"object store, and no attribute can reach it. Put yours there the way "
            f"everything else gets there: commit it on a branch off "
            f"`{ctx.origin}/{ctx.trunk}` and ship it, have that merge request merged, and "
            f"`--merge` works here. Or run the same command without `--merge` and have the "
            f"merge request merged by hand")


def upstream_config_digests(ctx: Ctx) -> Tuple[set, str]:
    """(every `.forkflow.toml` the original project has EVER had, as `config_digest`s; "" -
    or why this clone cannot be asked that at all, which is a refusal, see
    `fork_config_state`).

    Three parts, and each is there because of a route that was open without it:

    - what the refs upstream cannot choose carry NOW (`foreign_remote_refs`), read over
      their whole histories (`config_versions_in`);
    - what this clone has WRITTEN DOWN of upstream's, from every earlier run
      (`remember_upstream_configs`) - because a version that is gone from every ref is not
      a version upstream never had, and nothing read out of the repository can say so. A
      memory that has been made to forget - a state file that cannot be read, or one the
      original project has published enough versions to fill - is a refusal and not a short
      answer (`config_memory_unprovable`);

    Whether the file being JUDGED is the same kind of thing as these blobs is not asked
    here, and that is deliberate. This answers what the original project's configs are -
    raw blobs, read out of the object store - and the question of rendering is about the
    other side of the comparison. Asked here it reached the `trunk_*` states too, where the
    config is read with `git show <origin/trunk>:<name>` and the working tree is never
    consulted: a fork in the state this tool RECOMMENDS was refused by any `filter` or
    `ident` on the config's path, and told to delete its own reviewed config for a condition
    that could not change the answer. It is asked where the answer is read off the working
    tree instead (`fork_config_state`).
    - and what the refs the CONFIG names carry, which only ever WIDENS the answer
      (`upstream_scope_refs`) and is deliberately never written down: a config that names
      this fork's own trunk would otherwise put the fork's own bytes into the memory for
      good, and no later edit of the config could take them out again.

    The two walks are two `rev-list`s over sets that mostly overlap; the walk itself is the
    cheap half (30ms of the 1.0s measured in `config_versions_in`), and the expensive half -
    one `cat-file` per distinct version - is paid once per version in each, so the cost of
    the split is a second walk and not a second read of the repository's configs."""
    blind = history_unprovable(ctx) or config_memory_unprovable(ctx)
    if blind:
        return (set(), blind)
    theirs = foreign_remote_refs(ctx)
    seen, why = config_versions_in(ctx, theirs)
    if why:
        return (set(), why)
    known, why = remember_upstream_configs(ctx, seen)
    if why:
        return (set(), why)
    named = [ref for ref in upstream_scope_refs(ctx) if ref not in set(theirs)]
    wider, why = config_versions_in(ctx, named)
    if why:
        return (set(), why)
    return (known | wider, "")


def config_is_upstreams(ctx: Ctx, text: Optional[str],
                        upstreams: Optional[set] = None) -> bool:
    """Are these the bytes of a `.forkflow.toml` the original project has?

    The one question every `--merge` provenance decision asks, and it is asked of the
    CONTENT, because nothing else answers it:

    - the path's HISTORY lies in both directions. `git log -1 <rev> -- <path>` names this
      fork's commit when the file was re-added under another name - which is what the
      case-variant `git mv` remedy this tool prints does to upstream's file - and names
      upstream's commit when a sync merge kept upstream's side, because history
      simplification follows the parent the content came from;
    - INDEX MEMBERSHIP lies too: upstream's file, brought in by a sync and untracked by
      hand (`git rm --cached`, committed on a sync branch), is absent from the index, from
      HEAD and from MERGE_HEAD - exactly the state `setup` leaves this fork's template in.

    The bytes lie in neither direction. A `.forkflow.toml` is this fork's own precisely
    when it differs from every `.forkflow.toml` upstream has (`upstream_config_digests`),
    so any edit the fork makes to the file makes it the fork's - the way back every
    refusal prints. None (there is no file) is not upstream's: nothing came from there.

    `upstreams` is that set when the caller has already read it - `fork_config_state` asks
    this question of up to two files and would otherwise walk upstream's history twice for
    one decision. It is a value passed down WITHIN one decision, never a cache kept across
    calls: `fork_merge_mode` runs twice per `--merge` run on purpose, the second time after
    the run's own fetch, and each of those reads upstream's history as it stands then."""
    if text is None:
        return False
    if upstreams is None:
        upstreams, _ = upstream_config_digests(ctx)  # the refusal is `fork_config_state`'s
    return config_digest(text) in upstreams


def working_config_text(ctx: Ctx) -> Optional[str]:
    """The working tree's `.forkflow.toml` as it stands, None when no file is listed under
    exactly that name (a case variant is not it - `load_config` refuses those) or it cannot
    be read. The bytes, not the settings: provenance is about the file.

    Read in TEXT mode on purpose: Python turns CRLF and CR into LF here, which is half of
    why a `text` or `eol` attribute cannot make upstream's config read as this fork's
    (`config_render_unprovable`). `config_fingerprint` is the other half."""
    try:
        if CONFIG_FILE not in os.listdir(ctx.root):
            return None
        with open(os.path.join(ctx.root, CONFIG_FILE), "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def written_by_upstream(ctx: Ctx, text: Optional[str],
                        upstreams: Optional[set] = None) -> bool:
    """True when `text` - the `.forkflow.toml` a revision holds - is one the original
    project has, by its bytes (`config_is_upstreams`), or when there was none to read.

    That is the trunk a fresh fork bootstraps: `origin/<trunk>` starts as a copy of
    upstream, and an upstream that tracks the file hands this fork its `merge` with nobody
    here having written or reviewed it - which stays so through every later ship that does
    not touch the file, and through a sync merge resolved in upstream's favour. It asked
    `git log -1 <revision> -- <path>` who last wrote the path until that answer was found
    to be wrong in both directions; see `config_is_upstreams`. `upstreams` is that helper's
    set when the caller has already read it, and is passed no further than this decision."""
    if text is None:
        return True                 # there but unreadable: upstream's is the safe answer
    return config_is_upstreams(ctx, text, upstreams)


def own_untracked_config(ctx: Ctx, upstreams: Optional[set] = None) -> bool:
    """True when the working tree's `.forkflow.toml` is this fork's own untracked file - the
    one `setup` leaves: listed under exactly that name, held by git nowhere here (the index,
    HEAD, the MERGE_HEAD of a merge in progress), and holding bytes no `.forkflow.toml` of
    the original project's holds (`config_is_upstreams`).

    The last condition is the one that matters. A fork that never chose `merge` can end up
    with upstream's file sitting untracked on disk - a sync brings it in and the user
    untracks it by hand - and that is byte for byte the state `setup` leaves its own
    template in. Where git holds the file cannot tell the two apart; the bytes can.
    `upstreams` is that set when the caller has already read it (`config_is_upstreams`)."""
    text = working_config_text(ctx)
    if text is None:
        return False
    if tracked_config_names(ctx.root):
        return False
    revisions = ["HEAD"] + (["MERGE_HEAD"] if merge_in_progress(ctx.root) else [])
    if any(config_name_at(ctx, rev) is not None for rev in revisions):
        return False
    return not config_is_upstreams(ctx, text, upstreams)


def fork_config_state(ctx: Ctx) -> Tuple[str, Optional[str], str]:
    """Which `.forkflow.toml` speaks for this fork's `merge`, the bytes of it, and - when
    the question cannot be answered at all - why not.

    `fork_merge_mode` reads the mode out of the two states that carry a declaration of
    this fork's own - `trunk_own` and `untracked_own` - and `fork_merge_refusal` names the
    state the user is in. The two asked these questions separately, in the same order,
    with nothing tying them together: a state added to one fell silently into the other's
    "generic". The states, in the order they are decided:

    - `trunk_own` / `trunk_upstreams` - a config is committed on `origin/<trunk>`, and
      whose bytes those are (`written_by_upstream`) decides whether it is this fork's
      reviewed declaration or upstream's, adopted whole by a trunk bootstrapped from a
      project that tracks the file;
    - `branch_own` / `branch_upstreams` - none is committed on the trunk and one is in the
      index: a config the CHECKED-OUT BRANCH carries is read for `merge` nowhere, on a
      branch it is neither untracked nor on the trunk, and a sync branch carries
      upstream's file;
    - `untracked_own` - the fork's own untracked `.forkflow.toml`, where `setup` leaves
      it: own by its bytes, not by where git holds it (`own_untracked_config`);
    - `untracked_upstreams` - upstream's file, untracked by hand after a sync brought it
      in, which is byte for byte the state `setup` leaves the fork's own template in;
    - `none` - no readable file anywhere it would be read from.

    Before any of them, `unprovable`: `upstream_config_digests` could not be asked what the
    original project's configs are (a shallow, partial, grafted or replaced clone, an
    object that is not here, a walk git refused), or what this clone wrote down of the
    project's own configs cannot be trusted (a state file that cannot be read, a memory the
    project has published enough versions to fill - `config_memory_unprovable`). Every state
    below it is a statement about
    a set that would then be short, and a short set reads upstream's own file as this
    fork's - so the answer is the reason, and `--merge` is refused with it. It is decided
    first because it is a fact about the clone, true whichever file is being judged.

    WHERE THE RENDERING QUESTIONS ARE ASKED IS PART OF THE ANSWER. They are about the file
    on DISK - whether what reading the path gives is the same kind of thing as the blobs it
    is compared against - so they are asked below the `trunk_*` states and nowhere above
    them: a config committed on the trunk is read with `git show <origin/trunk>:<name>`,
    blob against blobs, and no symlink, filter or `ident` can reach it. Asked above, they
    refused the state this tool recommends and printed an `rm` of the fork's own reviewed
    config for a condition that could not change the decision. Three of them, in order:
    what is set NOW (`config_render_unprovable`), what these BYTES were already judged to be
    (`config_rendered_before` - an attribute can be turned off and the converted file stays),
    and, for an untracked config only, what the original project's own history ever set on
    that path (`config_history_render_unprovable` - the file git wrote is still there after
    the `.gitattributes` that rewrote it is deleted).

    The text is whatever the state was read from, None when there is none; the third field
    is that reason, "" in every other state.

    What upstream's configs are is read ONCE here and handed to each question this one
    decision asks - the states below ask it of up to two files, and the walk behind it
    (`upstream_config_digests`) would otherwise be repeated for one answer. Nothing keeps it
    beyond this call: `fork_merge_mode` runs twice per `--merge` run on purpose."""
    upstreams, blind = upstream_config_digests(ctx)
    if blind:
        return ("unprovable", working_config_text(ctx), blind)
    trunk_name = f"{ctx.origin}/{ctx.trunk}"
    if has_ref(ctx.root, f"refs/remotes/{trunk_name}") and config_name_at(ctx, trunk_name):
        # one read, two questions: whose the file is and what it says have to be asked of
        # the same bytes. The two calls a `--merge` run makes stay two reads on purpose -
        # the second, in `merge_mr`, is after this run's fetch moved the ref
        text = config_text(ctx, trunk_name)
        return ("trunk_upstreams" if written_by_upstream(ctx, text, upstreams)
                else "trunk_own", text, "")
    # below this line the answer is read off the WORKING TREE, and only below it can the
    # way this clone renders that file change what the answer is
    here = working_config_text(ctx)
    blind = config_render_unprovable(ctx)
    if blind:
        # ... and the verdict is written down, because the condition can be ENDED without
        # the rendered file going anywhere: `remember_rendered_config`
        return ("unprovable", here, blind + remember_rendered_config(ctx, here))
    blind = config_rendered_before(ctx)
    if blind:
        return ("unprovable", here, blind)
    if tracked_config_names(ctx.root):
        return ("branch_upstreams" if config_is_upstreams(ctx, here, upstreams)
                else "branch_own", here, "")
    if own_untracked_config(ctx, upstreams):
        # the one state where a file GIT wrote can pass for one this fork wrote, so it is
        # also the only one that has to ask what the original project's history renders
        blind = config_history_render_unprovable(ctx)
        return (("unprovable", here, blind) if blind else ("untracked_own", here, ""))
    if config_is_upstreams(ctx, here, upstreams):
        return ("untracked_upstreams", here, "")
    return ("none", here, "")


def fork_merge_mode(ctx: Ctx) -> str:
    """This fork's `merge`, "self" or "manual" - the one reader every `--merge` decision goes
    through: `merge_gate` before anything is pushed, and `merge_mr` again right before the
    merge command runs.

    Read only from where the original project cannot write it:

    - the config committed on `origin/<trunk>` - this fork's reviewed state - unless its
      bytes are a `.forkflow.toml` upstream has (`fork_config_state`: `trunk_own`);
    - while none is committed there, the fork's own untracked `.forkflow.toml`, where
      `setup` leaves it (`untracked_own`).

    Never from the checked-out branch's committed tree, nor from a tree a sync merge has
    touched: a sync branch carries upstream's `.forkflow.toml`, and reading `merge` from the
    working tree let upstream's `merge = "self"` merge a reviewed fork's sync four ways -
    on `--continue`, under a case variant of the name, and from a sync branch left checked
    out. Every other state - no file, a file that cannot be read, one only the branch or
    only upstream carries, and a clone that cannot be asked whose a file is at all
    (`unprovable`) - is "manual"."""
    state, text, _ = fork_config_state(ctx)
    if state == "trunk_own":
        where = f"{ctx.origin}/{ctx.trunk}:{CONFIG_FILE}"
        return merge_mode_in(text, where) or MERGE_MANUAL
    if state == "untracked_own":
        return load_config(ctx.root).get("merge") or MERGE_MANUAL
    return MERGE_MANUAL


def fork_merge_source(ctx: Ctx) -> str:
    """Where `fork_merge_mode` reads it, for the messages that refuse `--merge`."""
    return (f"the `{CONFIG_FILE}` committed on `{ctx.origin}/{ctx.trunk}` - or, while none "
            f"is committed there, an untracked `{CONFIG_FILE}` in the working tree")


def fork_merge_refusal(ctx: Ctx) -> str:
    """Why `--merge` is refused here and the way back - the rest of `merge_gate`'s message
    after "--merge needs `merge = "self"` in this fork's own config - ".

    The state is `fork_config_state`'s, the same answer `fork_merge_mode` just decided on
    - this used to re-derive it, so a state added there reached a message written for
    another one. The user can see which state they are in, so the message names it:

    - this clone cannot be asked the question at all (`unprovable`), which is about the
      clone and not about any file, so the message is the condition and the way round it;
    - the config on the trunk is upstream's own file, byte for byte (a sync took it whole);
    - the checked-out branch carries one, which is read nowhere until it is on the trunk;
    - the untracked config in the working tree is upstream's own file;
    - or there simply is no `merge = "self"` this fork wrote.

    The upstream-bytes states used to print the last one's wording, and a user looking at
    a file that plainly reads `merge = "self"` read that as a bug in the tool. Each state
    leaves a way forward, and where the file is upstream's it is the same one: an edit of
    the fork's own makes the file the fork's. A `{ship}` is named only for a config that
    already is this fork's - printed commands are followed to the letter, and shipping
    upstream's bytes to the trunk changes nothing about whose word `merge` is."""
    trunk_name = f"{ctx.origin}/{ctx.trunk}"
    ship = rerun_cmd("ship", argparse.Namespace(mr=True))
    theirs = (f"is the original project's own `{CONFIG_FILE}`, byte for byte, so what it "
              f"says about `merge` is upstream's word and not this fork's - whatever you "
              f"read in it")
    yours = (f"Make it this fork's: edit it - with `merge = \"self\"` set by you - ")
    generic = (f"{fork_merge_source(ctx)}; a `{CONFIG_FILE}` the checked-out branch carries "
               f"is not read for it. This fork's merge requests are merged by hand.")
    state, _, blind = fork_config_state(ctx)
    if state == "unprovable":
        # nothing about the file: this clone cannot be asked what upstream's configs are,
        # so no answer about whose the file is would mean anything. The way forward is the
        # one every fork without `merge = "self"` uses, and it needs no clone at all
        return (f"and whether any `{CONFIG_FILE}` here is this fork's own cannot be decided "
                f"in this clone: {blind}. Until it can, `--merge` is refused whatever the "
                f"file says - a version the original project had that this clone cannot see "
                f"reads as one it never had, which is upstream's `merge` (and upstream's "
                f"`gate`, which this tool runs as shell) taken for this fork's word. "
                f"Nothing else is refused: run the same command without `--merge` and have "
                f"the merge request merged by hand.")
    if state == "trunk_own":
        return generic                  # this fork's own file; it just does not say "self"
    if state == "trunk_upstreams":
        return (f"the `{CONFIG_FILE}` committed on `{trunk_name}` {theirs}. A sync brought "
                f"it in and it was taken whole. {yours}on a branch off `{trunk_name}`, "
                f"commit it there and `{ship}` it; once that merge request is merged, "
                f"`--merge` works here.")
    if state == "branch_upstreams":
        # upstream's file, on the branch: no command - shipping it puts upstream's bytes
        # on the trunk, where `merge` is still upstream's word and this refusal returns
        return (f"the `{CONFIG_FILE}` this branch carries {theirs}, and a config the "
                f"checked-out branch carries is not read for `merge` in any case - only "
                f"the one on `{trunk_name}`, and only while it is this fork's own. Edit "
                f"the file in your fork, with `merge = \"self\"` set by you, and get "
                f"that onto `{trunk_name}` the way everything else gets there.")
    if state == "branch_own":
        return (f"{fork_merge_source(ctx)}; the `{CONFIG_FILE}` this branch carries is not "
                f"read for it - committed on a branch it is neither untracked nor on "
                f"`{trunk_name}`. Get it onto the trunk first: `{ship}`, have that merge "
                f"request merged, then: {land_cmd()}. A `merge = \"self\"` it carries "
                f"counts from then on.")
    if state == "untracked_upstreams":
        return (f"the untracked `{CONFIG_FILE}` in the working tree {theirs}. A sync brought "
                f"it in and it was untracked by hand. {yours}and run this again; nothing has "
                f"to be committed, an untracked config is read while none is on "
                f"`{trunk_name}`.")
    # `untracked_own` that does not say "self", and `none`: no declaration to point at
    return generic


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
        raise Fail(f"resolve the conflicts and `git add` them, "
                   f"then run `{continue_cmd('sync', args)}` again")

    # a conflict on upstream's `.ForkFlow.toml` resolved with `git rm` takes the one file a
    # case-insensitive filesystem holds for both names: the fork's `.forkflow.toml` stays in
    # the index and is gone from the tree, and this run would go on with no config - no gate
    staged = config_name_in(tracked_config_names(ctx.root))
    try:
        on_disk = config_name_in(os.listdir(ctx.root))
    except OSError:
        on_disk = staged
    if staged and on_disk is None:
        raise Fail(f"`{staged}` is in the index but not in the working tree - removed by hand, "
                   f"maybe as another case of its name, which a case-insensitive filesystem "
                   f"makes the same file - and without it this sync would check nothing. Put "
                   f"it back from the index with `git checkout -- {sh_arg(staged)}` (nothing is "
                   f"there for it to overwrite), then run `{continue_cmd('sync', args)}` again")

    merging = merge_in_progress(ctx.root)
    merge_sha = sync_merge_commit(ctx)
    if not merging and not merge_sha:
        # a branch under today's sync name is one plain `sync` refuses ("already exists -
        # resume it with --continue"), which is this very message: only `--force` recreates it
        again = rerun_cmd("sync", args, " --force" if name == sync_branch(ctx) else "")
        raise Fail(f"nothing to continue: `{name}` carries no sync merge - run `{again}`")
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


def merge_gate(ctx: Ctx, args: argparse.Namespace, resume_sync: bool = False) -> None:
    """`--merge` refused, or nothing - before the fetch, the backup and any push.

    Config AND flag: the fork declares once, in `.forkflow.toml`, that its merge requests are
    merged by whoever opened them (`merge = "self"`), and the flag asks for it per run. Either
    alone does nothing, so a reviewed fork can never be merged by accident. The declaration
    is the fork's own - `fork_merge_mode`, never the checked-out tree, which on a sync branch
    or a resumed sync holds upstream's file. The second check is knowable now too: a merge
    command addresses the fork by URL (`mr_target`), and an origin that names no project
    would otherwise be a push followed by a failure. A branch name both tools would read as
    a merge request number is `ship_preflight`'s, with the other names `ship` will not
    take: it is a fact about the branch, not about this fork's config."""
    if not getattr(args, "merge", False):
        return
    if fork_merge_mode(ctx) != MERGE_SELF:
        # a resumed sync has a merge made and a branch to finish: the resume named keeps
        # `--mr` (which `--merge` implies), so it still opens the request the reviewer merges
        by_hand = (f" Resume without it - `{continue_cmd('sync', argparse.Namespace(mr=True))}`"
                   f" - and have the merge request merged by hand." if resume_sync else "")
        raise Fail(f"--merge needs `merge = \"self\"` in this fork's own config - "
                   f"{fork_merge_refusal(ctx)}{by_hand}")
    target, reason = mr_target(ctx)
    if not target:
        raise Fail(f"--merge: `{ctx.origin_url or '-'}` names no project to merge on "
                   f"({reason})")

def cmd_sync(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=True)
    header(ctx, "sync")
    # before `--continue`: a conflicted sync resumes with it, from a tree that is the merge
    merge_gate(ctx, args, resume_sync=bool(getattr(args, "cont", False)))
    if getattr(args, "cont", False):
        return cmd_sync_continue(ctx, args)

    branch = current_branch(ctx)
    if not branch:
        raise Fail("HEAD is detached: switch to a branch before syncing")
    if not clean_tree(ctx):
        raise Fail("the working tree has uncommitted changes: commit or stash them first")
    name = sync_branch(ctx)
    force = bool(getattr(args, "force", False))
    sync_branch_is_free(ctx, name, force, args)   # before the backup: none orphaned on a rerun
    if force:
        name = free_sync_name(ctx, name)      # a published sync branch is never pushed over
    tail = (f" unless --merge lands it - then you are on `{ctx.trunk}`"
            if getattr(args, "merge", False) else "; the trunk is never touched")
    print(f"  leaving `{branch}`, switching to `{name}` (you stay on it when this "
          f"finishes{tail})")

    stale = fetch_both(ctx)
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
        if ctx.dry_run and ctx.up() in stale:
            # `target` is `<upstream>/<branch>` as the LAST FETCH left it and this run did
            # not fetch, so "already in sync" is being said about a commit the server has
            # already moved past. It is a CONCLUSION and not a preview: it returns 0, and
            # everything the dry run exists to run - the merge simulation, the
            # untracked-in-the-way and case-collision preflights - is below it and does
            # not run. `advance_mirror` and `judge_landings` say this where they would
            # otherwise conclude from a ref the fetch line has just called stale
            raise Fail(f"this dry run did not fetch, so it cannot tell whether this fork "
                       f"is in sync: from the refs on disk {ctx.origin}/{ctx.trunk} "
                       f"already contains {short(target)}, which is `{ctx.up()}` as the "
                       f"last fetch left it - the `fetch` line above says what "
                       f"`{ctx.upstream}` has now, and none of the checks below this ran. "
                       f"Run `git fetch {sh_arg(ctx.upstream)}` and try again, or run the "
                       f"same command without `--dry-run`, which fetches first")
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
        raise Fail(f"git refuses a merge that would overwrite an untracked file, and nothing "
                   f"was backed up or branched for this sync. "
                   + in_the_way_advice(ctx, blocked, f"run `{rerun_cmd('sync', args)}` again",
                                       args))

    log = git("log", "--oneline", "--no-decorate", f"{ctx.origin}/{ctx.trunk}..{target}",
              cwd=ctx.root, check=False)
    commits = [ln for ln in log.splitlines() if ln]
    step("commits", f"git log --oneline {sh_arg(f'{ctx.origin}/{ctx.trunk}..{short(target)}')}",
         f"{len(commits)} upstream commit(s) to take")
    for line in commits:
        print(f"    {line}")

    backup_ref = backup(ctx, "pre-sync", f"{ctx.origin}/{ctx.trunk}")
    make_sync_branch(ctx, name, force, args)
    resume_unrecorded(ctx, "sync", write_state(ctx, "sync", {"branch": name,
                                                             "backup": backup_ref}), backup_ref)
    merge_upstream(ctx, name, target, commits, args)

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


def ship_preflight(ctx: Ctx, args: Optional[argparse.Namespace] = None) -> str:
    """The branch `ship` may rewrite, or Fail(2). Everything else is refused by name:
    ship rebases and squashes what it is on, and only a feature branch may be rewritten."""
    if rebase_in_progress(ctx):
        # `--continue` only resumes a ship *this clone* started: naming it after a rebase
        # that is not one (a `git pull --rebase` this run's own advice asked for, say) sends
        # the user to a command that refuses them. Either way it carries `--merge`/`--mr`:
        # followed to the letter, a resume without it opens or merges nothing
        being = rebasing_branch(ctx)
        resume = (continue_cmd("ship", args) if being and resumable(ctx, "ship", being)
                  else rerun_cmd("ship", args))
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
        raise Fail(f"`{branch}` is a sync branch: finish it with "
                   f"`{continue_cmd('sync', args)}`; a sync is merged, never squashed")
    if branch.startswith(ctx.backup_prefix):
        raise Fail(f"`{branch}` is a backup branch: it is a restore point, not a feature branch")
    if not valid_branch_name(branch):
        # refused here rather than after the squash: `push()` would refuse it at the end
        raise Fail(f"`{branch}` is a name forkflow will not push: git's refspec grammar does "
                   f"not read it as one branch (a leading `+` means force, a leading `-` an "
                   f"option) - rename it with `git branch -m <name>`")
    if getattr(args, "merge", False) and re.fullmatch(r"#?[0-9]+", branch):
        # `--merge` addresses the merge request by its source branch, and both tools read
        # `123` (or `#123`) as a merge request *number* - some other request entirely.
        # Here, with the other names this branch cannot be shipped under, and not in
        # `merge_gate`, which answers for the fork's config and not for the branch
        raise Fail(f"--merge: `{branch}` reads as a merge request number to glab and gh, "
                   f"which is how the merge is addressed - rename the branch "
                   f"(`git branch -m {sh_arg(branch)} <name>`) and ship again")
    if not clean_tree(ctx):
        raise Fail("the working tree has uncommitted changes: commit or stash them first")
    return branch


def rebase_onto(ctx: Ctx, branch: str, trunk_name: str,
                args: Optional[argparse.Namespace] = None) -> None:
    """Rebase locally so the trunk can fast-forward: `rebase locally, merge globally`."""
    cmd = f"git rebase {sh_arg(trunk_name)}"
    tip = rev(ctx.root, trunk_name)
    if ctx.dry_run:
        step("rebase", cmd, f"would replay `{branch}` onto {short(tip)}", dry=True)
        return
    rc, out, err = git_rc("rebase", trunk_name, cwd=ctx.root)
    if rc == 0:
        step("rebase", cmd, f"`{branch}` now sits on {short(tip)}")
        return
    if not rebase_in_progress(ctx):
        raise Fail(f"rebase of `{branch}` onto {trunk_name} failed:\n{(err or out).strip()}")
    unmerged = unmerged_paths(ctx)
    report_paths("rebase", cmd, unmerged, "conflicting file(s)")
    raise Fail(f"resolve the conflicts, `git add` them, run `git rebase --continue`, "
               f"then `{continue_cmd('ship', args)}`", EXIT_CONFLICT)


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
        raise Fail(f"cannot commit the squashed change:\n{(err or out).strip()}", EXIT_UNSAFE)
    tree_after = git("rev-parse", "HEAD^{tree}", cwd=ctx.root)
    if tree_after != tree_before:
        git("reset", "--soft", head_before, cwd=ctx.root, check=False)   # leave no bad commit
        step("squash", cmd, "TREE MISMATCH")
        raise Fail(f"the squashed commit's tree {short(tree_after)} differs from "
                   f"{short(tree_before)}: refusing to push a squash that changed the "
                   f"result", EXIT_UNSAFE)
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
    trunk_name = f"{ctx.origin}/{ctx.trunk}"
    # here, not in `cmd_ship`: a rebase that dropped every commit (`git rebase --skip`) lands on
    # the trunk's tip too, and `--continue` resumes straight into this function
    if not ctx.dry_run and rev(ctx.root, "HEAD") == rev(ctx.root, trunk_name):
        print(f"  nothing to ship: every commit of `{branch}` is already on {trunk_name} "
              f"(the backup `{backup_ref}` still holds the branch as it was)")
        return 0
    mb = git("merge-base", trunk_name, "HEAD", cwd=ctx.root)
    records = commit_records(ctx, mb)
    step("commits", f"git log --oneline {sh_arg(f'{trunk_name}..HEAD')}",
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
        if exc.code == EXIT_UNSAFE and rollback:
            print(rollback)
        raise

    if ctx.dry_run:
        warn_upstream_tracked(ctx, touched)      # the real run prints it after the squash
        step("check", "forkflow check", "not run (dry run)", dry=True)
    elif run_check(ctx, touched) == EXIT_CHECK:
        if rollback:
            print(rollback)
        # the squash has already happened, so a plain rerun of `ship` starts from a branch whose
        # commits are no longer the published ones; `--continue` resumes this run instead
        print(f"  fix that on `{branch}` and commit it, then: {continue_cmd('ship', args)}")
        return EXIT_CHECK

    base = rev(ctx.root, trunk_name)                    # what the squash was built on
    try:
        push(ctx, branch, lease=lease or None, backup_ref=backup_ref)
    except Fail as exc:
        if exc.code == EXIT_UNSAFE and rollback:
            print(rollback)
        raise
    title = (getattr(args, "title", None)
             or (message.strip().splitlines() or [f"ship {branch}"])[0])
    return publish(ctx, args, "ship", branch, base, title, ship_body(ctx, message, touched))


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
    branch = ship_preflight(ctx, args)
    merge_gate(ctx, args)      # before `--continue`, the fetch, the backup and the push
    trunk_name = f"{ctx.origin}/{ctx.trunk}"

    if getattr(args, "cont", False):
        # a ship in progress is one this clone started: its backup and the lease its fetch
        # saw are recorded, and without them there is nothing to resume and nothing to
        # force-push behind (rule 4)
        state = resumable(ctx, "ship", branch)
        if not state.get("backup"):
            raise Fail(f"no ship to continue on `{branch}`: `--continue` resumes the run that "
                       f"made the pre-ship backup - run `{rerun_cmd('ship', args)}`")
        cmd = f"git merge-base --is-ancestor {sh_arg(trunk_name)} HEAD"
        rc, _, _ = git_rc("merge-base", "--is-ancestor", trunk_name, "HEAD", cwd=ctx.root)
        if rc != 0:
            step("continue", cmd, f"`{branch}` is not on {trunk_name}'s tip")
            raise Fail(f"the rebase did not complete; run `{rerun_cmd('ship', args)}` again")
        step("continue", cmd, "the rebase completed - resuming at the squash")
        return finish_ship(ctx, args, branch, state["backup"], state.get("lease", ""))

    rc, cmd, result, err, _ = fetch(ctx, (ctx.origin,), [trunk_name])
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    step("fetch", cmd, result)

    rc, _, _ = git_rc("merge-base", "--is-ancestor", "HEAD", trunk_name, cwd=ctx.root)
    if rc == 0:
        print(f"  nothing to ship: `{branch}` has no commits beyond {trunk_name}")
        return 0

    # the lease is what the fetch just saw; the rebase and the squash come after it. A lease
    # only proves nobody pushed after that fetch - so what the fetch found has to be ours
    # already, or shipping would rewrite someone else's commits out of the branch.
    branch_ref = f"refs/remotes/{ctx.origin}/{branch}"
    lease = rev(ctx.root, branch_ref)
    if lease and origin_has_branch(ctx, branch) is False:
        # the fetch does not prune, so `origin/<branch>` can outlive the branch on origin -
        # GitLab's `--remove-source-branch` after a merge, `land --force` keeping the branch.
        # Offered as the lease it is refused ("stale info") on every later ship. Origin has
        # nothing under that name, so there is nothing to force-push away and nothing whose
        # ownership to prove: the push creates the branch, without a lease - and a plain
        # push cannot replace what somebody publishes there in the meantime (it only ever
        # fast-forwards). The backup is made as always
        drop_stale_tracking(ctx, branch)
        lease = ""
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
            saved = f"{ctx.backup_prefix}{utc_stamp('%Y%m%d-%H%M%S')}-theirs"
            raise Fail(
                f"`{ctx.origin}/{branch}` carries commits that `{branch}` does not and that "
                f"this clone has no record of publishing: shipping would force-push them "
                f"away. If they are somebody else's, take them in first "
                f"(`git pull --rebase {sh_arg(ctx.origin)} {sh_arg(branch)}`). If they are "
                f"yours from elsewhere, keep them first (`git branch {sh_arg(saved)} "
                f"{sh_arg(ctx.origin + '/' + branch)} && git push {sh_arg(ctx.origin)} "
                f"{sh_arg('refs/heads/' + saved)}:{sh_arg('refs/heads/' + saved)}`), then "
                f"ship again")

    backup_ref = backup(ctx, "pre-ship", "HEAD")
    resume_unrecorded(ctx, "ship", write_state(ctx, "ship", {"branch": branch,
                                                             "backup": backup_ref,
                                                             "lease": lease}), backup_ref)
    rebase_onto(ctx, branch, trunk_name, args)
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
                   f"is not in this clone any more (its branch deleted and pruned?) - there is "
                   f"nothing to judge; `{land_cmd(force=True)}` catches the trunk up "
                   f"unverified, "
                   f"keeps the branch and forgets the record")
    if entry["kind"] != "ship":
        return (None, "")
    # an empty ship (the squash allows one) has no patch: `git cherry` would match it with
    # any empty commit on the trunk. Ancestry is its only landing
    if git_ok("diff", "--quiet", base, commit, cwd=ctx.root):
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
            if not still_carries(ctx, trunk_ref, commit):
                return (None, "")
            return (sha.strip(), "rewritten")
    return (None, "")


def still_carries(ctx: Ctx, trunk_ref: str, commit: str) -> bool:
    """Does the trunk's tip still have `commit`'s change? A patch-equivalent trunk commit is
    no landing when a later commit reverted it - `git cherry` matches the patch, not the
    result. Merging `commit` into the trunk (`merge-tree`, nothing written but objects) gives
    back the trunk's own tree exactly when the change is there already. A conflict is not
    "reverted" - later work on the same lines is the ordinary case - and git older than the
    merge simulation needs cannot ask, so both keep `git cherry`'s answer."""
    if git_version() < MERGE_TREE_GIT:
        return True
    rc, out, _ = merge_tree(ctx.root, "--write-tree", trunk_ref, commit)
    merged = out.split()[:1]
    if rc != 0 or not merged:
        return True
    return merged[0] == git("rev-parse", f"{trunk_ref}^{{tree}}", cwd=ctx.root)


def pending_verdict(ctx: Ctx, entry: dict) -> str:
    """`status`'s word on the pending entry, from the refs as they are: never raises.

    Three states. `landed()` needs `origin/<trunk>` and the pending commit in this clone,
    and `status` resolves with `need_trunk=False` - a fresh fork has no `origin/<trunk>`, and
    a commit whose branch was deleted and pruned is gone; `landed()` raises for either (git
    cannot resolve what it is asked to compare), which is "cannot verify here", not an
    error. Git only, so the answer is the same under `--offline`; `--fetch` moves the refs it
    is read from.

    The landed verdict names the branch: plain `forkflow land` on a branch with a record of
    its own lands that record only, so run from there it would answer "not merged yet" about
    another request and never reach this one. Named, it lands this record from any branch
    (and from a linked worktree gets a refusal that says where to run it)."""
    try:
        sha, _ = landed(ctx, entry)
    except Fail:
        return "cannot verify here"
    return (f"landed: run {land_cmd(entry['branch'])}" if sha
            else f"not on {ctx.origin}/{ctx.trunk} yet")


def pending_line(ctx: Ctx, branch: str, entry: dict) -> str:
    """A pending record as a report line: `status` prints one per record, and `land`
    prints one for each it kept. status/SKILL.md and land/SKILL.md document it as one
    format ("as `status` shows it"), so there is one place it is written."""
    return (f"  pending  {entry['kind']} {branch} -> MR {entry.get('mr') or '-'} - "
            f"{pending_verdict(ctx, entry)}")


def trunk_worktree_elsewhere(ctx: Ctx) -> str:
    """The other worktree the trunk is checked out in, or "" (none, or this one)."""
    wt = branch_worktree(ctx, ctx.trunk)
    return wt if wt and os.path.realpath(wt) != os.path.realpath(ctx.root) else ""


def trunk_elsewhere(ctx: Ctx, resume: str = "") -> None:
    """Fail(2) when the trunk is checked out in another worktree: it has to be checked out
    and fast-forwarded there, as `advance_mirror` insists for the mirror.

    That is a route that works: the `pending` records are shared by every worktree of the
    clone (`SHARED_STATE`), so `resume` there finishes the run made here - it names the
    branch when this run was landing one record, since HEAD there is on another branch.
    Removing the other worktree is not offered - the main worktree cannot be removed, and
    it is the usual one."""
    wt = trunk_worktree_elsewhere(ctx)
    if wt:
        raise Fail(f"trunk `{ctx.trunk}` is checked out in {wt}: run "
                   f"`{resume or land_cmd()}` there - "
                   f"every worktree of this clone sees the same pending records")


# `trunk_ref` is always the full `refs/remotes/<origin>/<trunk>` and `trunk_name` always the
# short `<origin>/<trunk>` git also accepts and messages print. A wrong refspec here means "no
# such ref", silently, so the two never share a name.
def land_trunk(ctx: Ctx) -> Tuple[str, str]:
    """Check the local trunk out and fast-forward it to `origin/<trunk>`. (old sha, new sha).

    The checkout is unconditional: HEAD is usually on the branch about to be deleted, and
    `land` leaves you on the trunk either way (`git checkout`, not `switch` - the git floor
    is 2.20). The fast-forward is `merge --ff-only` - the second owner of that call after
    `advance_mirror`, pinned by `TestSourceInvariants` - and never a ref move: the branch is
    checked out, so its files have to follow. A local trunk with commits origin lacks is
    refused before anything moves: this plugin never creates such commits (rule 2), so they
    are someone's by-hand work and not this script's to lose. A trunk checked out in another
    worktree is `land_preflight`'s, refused before anything moves."""
    t = ctx.trunk
    trunk_ref = f"refs/remotes/{ctx.origin}/{t}"
    trunk_name = f"{ctx.origin}/{t}"
    new = rev(ctx.root, trunk_ref)
    if not new:
        raise Fail(f"`{trunk_name}` does not resolve after the fetch: is the trunk still on "
                   f"{ctx.origin}?")
    old = rev(ctx.root, f"refs/heads/{t}")
    if old and old != new:
        rc, _, _ = git_rc("merge-base", "--is-ancestor", old, trunk_ref, cwd=ctx.root)
        if rc != 0:
            raise Fail(f"`{t}` has commits {ctx.origin} lacks - the plugin never creates "
                       f"these; resolve by hand (`git log {sh_arg(f'{trunk_name}..{t}')}`), "
                       f"then run `{land_cmd()}` again")
    if not old:
        cmd = f"git branch --no-track {sh_arg(t)} {sh_arg(trunk_name)}"
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
    cmd = f"git merge --ff-only {sh_arg(trunk_name)}"
    if ctx.dry_run:
        step("trunk", cmd, f"{short(old)} -> {short(new)}", dry=True)
        return (old, new)
    rc, out, err = git_rc("merge", "--ff-only", trunk_ref, cwd=ctx.root)
    if rc != 0:                     # unreachable after the ancestor check, but git has the say
        step("trunk", cmd, "REFUSED")
        raise Fail(f"cannot fast-forward `{t}`:\n{(err or out).strip()}")
    step("trunk", cmd, f"{short(old)} -> {short(new)}")
    return (old, new)


def land_preflight(ctx: Ctx, resume: str) -> None:
    """What `land` cannot do from here, refused by name: a stopped rebase (before the tree -
    it always leaves the tree dirty), uncommitted changes, the trunk checked out in another
    worktree (where `resume` has to run)."""
    if rebase_in_progress(ctx):
        raise Fail("a rebase is in progress: finish it (`git rebase --continue`) or abort "
                   "it (`git rebase --abort`) first")
    if not clean_tree(ctx):
        raise Fail("the working tree has uncommitted changes: commit or stash them first")
    trunk_elsewhere(ctx, resume)


def landed_others(ctx: Ctx, entry: dict) -> list:
    """The branches of every other pending record that is on the trunk, as `landed` judges
    it from the refs this run fetched; one it cannot judge here is left out."""
    found = []
    for name, other in sorted(pending_entries(ctx).items()):
        if name == entry["branch"]:
            continue
        try:
            sha, _ = landed(ctx, other)
        except Fail:
            continue
        if sha:
            found.append(name)
    return found


def pending_to_land(ctx: Ctx, entry: Optional[dict], branch: str, force: bool) -> list:
    """The records this `land` acts on: `entry` when the caller holds it (`--merge` lands
    the one its own run wrote); the one for `branch` when it is named; the one for the
    branch HEAD is on; otherwise every record there is - ships from other worktrees wait in
    the same file, and each that has landed is landed. `force` judges nothing, so it acts
    on one record only: with several and none chosen, it needs the branch named."""
    if entry is not None:
        return [entry]
    entries = pending_entries(ctx)
    if not entries:
        raise Fail(f"nothing pending: `{rerun_cmd('ship', None)}` or "
                   f"`{rerun_cmd('sync', None)}` first - `land` finishes the run that pushed "
                   f"a branch")
    names = ", ".join(f"`{b}`" for b in sorted(entries))
    if branch:
        if branch not in entries:
            raise Fail(f"nothing pending for `{branch}` - what is pending: {names}")
        return [entries[branch]]
    here = current_branch(ctx)
    if here in entries:
        return [entries[here]]
    if force and len(entries) > 1:
        raise Fail(f"`--force` lands one record unverified, and {len(entries)} are pending "
                   f"({names}): name the one - `{land_cmd(force=True)} <branch>`")
    return [entries[b] for b in sorted(entries)]


def judge_landings(ctx: Ctx, chosen: Sequence[dict], force: bool, after_merge: bool,
                   implicit: bool) -> list:
    """Which of `chosen` are on `origin/<trunk>`: [(entry, the sha it landed as, how)].

    One verdict per record (`landed`), printed as it is reached, and the refusals that
    belong to the verdict rather than to the catch-up that follows it:

    - one record that has not landed is exit 2, "not merged yet" - or exit 6 right after
      `--merge`, where the tool reported the request merged and nothing reached the trunk
      (a merge train, auto-merge, a required pipeline);
    - one record whose commit is not in this clone raises out of `landed`, unless `force`,
      which judges nothing and lands it with a sha of None: the trunk is caught up and the
      branch is kept;
    - with several, an unjudged record is listed and kept instead, and none of them landed
      is the same exit 2, naming each with its merge request.

    Rule 5 is reported here, not enforced: a shipped commit reachable only through a merge
    commit's second parent is a WARNING beside its verdict."""
    trunk_name = f"{ctx.origin}/{ctx.trunk}"
    trunk_ref = f"refs/remotes/{trunk_name}"
    several = len(chosen) > 1
    landings = []                          # (entry, landed sha or None under --force, how)
    waiting = []                           # (entry, why) - only with several
    for e in chosen:
        kind, commit = e["kind"], e["commit"]
        label = f"`{e['branch']}` " if several else ""
        ancestry = f"git merge-base --is-ancestor {sh_arg(short(commit))} {sh_arg(trunk_name)}"
        try:
            sha, how = landed(ctx, e)
        except Fail:
            if several:
                step("landed?", ancestry, f"{label}cannot verify here - {short(commit)} is "
                                          f"not in this clone")
                waiting.append((e, "cannot verify here"))
                continue
            if not force:
                raise
            sha, how = None, ""         # nothing to judge - and `--force` judges nothing
        if sha is None:
            if several:
                step("landed?", ancestry, f"no - {label}{short(commit)} is not on {trunk_name}")
                waiting.append((e, f"not on {trunk_name} yet"))
                continue
            if not force:
                step("landed?", ancestry, f"no - {short(commit)} is not on {trunk_name}")
                if after_merge:
                    there = trunk_worktree_elsewhere(ctx)
                    where = f" in {there}, where `{ctx.trunk}` is checked out" if there else ""
                    raise Fail(f"the platform tool reported the merge request merged, but "
                               f"{trunk_name} does not have {short(commit)} - the merge is "
                               f"queued or waiting (a merge train, auto-merge, a required "
                               f"pipeline?); once it is on {trunk_name}, run "
                               f"`{land_cmd(e['branch'])}`{where}",
                               EXIT_NOT_MERGED)
                note = ""
                if kind == "sync":
                    note = (f"; if it was squashed or rebased in the UI, rule 5 was broken "
                            f"(see rules.md) - `{land_cmd(force=True)}` fast-forwards anyway")
                others = landed_others(ctx, e) if implicit else []
                if others:
                    note += ("\n  other merge requests have landed - land each by its name: "
                             + ", ".join(f"`{land_cmd(b)}`" for b in others))
                request = e.get("mr") or f"`{e['branch']}`"
                if ctx.dry_run:
                    # a dry run does not fetch - a fetch writes FETCH_HEAD, the
                    # remote-tracking refs and objects - so the ancestry above was asked of
                    # the refs on disk. Saying "merge it" on that would be a guess; the
                    # `fetch` line says what the server has, and this says what it means
                    raise Fail(f"this dry run did not fetch, so it cannot tell whether MR "
                               f"{request} has landed: from the refs on disk "
                               f"{short(commit)} is not on {trunk_name}, and the `fetch` "
                               f"line above says what {ctx.origin} has now. Run "
                               f"`{land_cmd()}` without `--dry-run` to fetch and "
                               f"decide{note}")
                raise Fail(f"MR {request} is not on {trunk_name} yet - merge it, then run "
                           f"`{land_cmd()}` again{note}")
            step("landed?", ancestry, "landing not verified (--force): fast-forwarding to "
                                      f"whatever {trunk_name} holds")
        else:
            cmd = ancestry if how == "ancestor" else (
                f"git cherry {sh_arg(short(commit))} {sh_arg(trunk_name)} "
                f"{sh_arg(short(e['base']))}")
            step("landed?", cmd, f"yes - {label}as {short(sha)} ({how})")
            if kind == "ship" and how == "ancestor":
                # a fast-forward puts the shipped commit on the trunk's first-parent line; a
                # merge commit keeps its SHA too, but hangs it off a second parent
                first = git("rev-list", "--first-parent", f"{e['base']}..{trunk_ref}",
                            cwd=ctx.root, check=False).split()
                if sha not in first:
                    print(f"  WARNING: the ship MR {label}was merged as a merge commit - rule "
                          f"5 asks for a fast-forward; check the project's merge method "
                          f"(`forkflow setup` reports it)")
        landings.append((e, sha, how))
    if not landings:                       # only with several: one alone has raised above
        lines = "".join(f"\n  `{e['branch']}` (MR {e.get('mr') or '-'}): {why}"
                        for e, why in waiting)
        if ctx.dry_run:
            # the sentence the single-record path above raises, for the same reason: a dry
            # run does not fetch, so every "no" printed above was read off the refs on
            # disk. Sending a user to merge requests that are already merged - and that
            # the very next non-dry `land` lands without complaint - is the guess this
            # caveat exists to stop, and it was written for one record only
            raise Fail(f"this dry run did not fetch, so it cannot tell whether anything "
                       f"pending has landed: from the refs on disk none of these is on "
                       f"{trunk_name}, and the `fetch` line above says what {ctx.origin} "
                       f"has now. Run `{land_cmd()}` without `--dry-run` to fetch and "
                       f"decide:{lines}")
        raise Fail(f"nothing pending is on {trunk_name} yet - merge, then run `{land_cmd()}` "
                   f"again:{lines}")

    return landings


def report_landing(ctx: Ctx, landings: Sequence[tuple], leftovers: Sequence[str],
                   several: bool, old: str, new: str) -> None:
    """What the run did, in the three things it has to say: where the trunk went and that
    HEAD is on it; a branch origin may still have (GitHub deletes none itself, and only a
    verified landing gets the hint); and whatever is still pending, in the one format
    `status` prints it in (`pending_line`)."""
    moved = f"{short(old)}..{short(new)}" if old != new else f"at {short(new)}"
    verified = [e["branch"] for e, sha, _ in landings if sha is not None]
    said = "landed" if verified else "caught up, landing NOT verified"
    if several:
        said += " " + ", ".join(f"`{b}`" for b in verified)
    print(f"  {DRY_PREFIX if ctx.dry_run else ''}{said}: {ctx.trunk} {moved} - you are on "
          f"{ctx.trunk}")
    if ctx.platform == "github":
        for name in leftovers:
            print(f"  {ctx.origin}/{name} may still exist: git push {sh_arg(ctx.origin)} "
                  f"--delete {sh_arg(name)}")
    done = {e["branch"] for e, _, _ in landings}
    rest = {b: e for b, e in pending_entries(ctx).items() if b not in done}
    for b in sorted(rest):
        print(pending_line(ctx, b, rest[b]))


def land_pending(ctx: Ctx, force: bool = False, after_merge: bool = False,
                 entry: Optional[dict] = None, branch: str = "") -> None:
    """The closing step of a ship or a sync, from the `pending` records: fetch, recognise
    the landing, fast-forward the local trunk, delete the landed branch, forget the record.

    Works after a human merged the merge request, in any later session, and after a
    "squash and merge" or a "rebase and merge" of a ship (see `landed`). A merge request
    that is not on the trunk yet is exit 2 and not an error - "not merged yet" - unless
    `force`, which fast-forwards to whatever origin has, deletes no branch (nothing was
    verified) and clears the record: the escape hatch for a landing the tool cannot see,
    for a request that was closed instead of merged, and for a record whose commit is no
    longer in this clone. Right after `--merge` (`after_merge`) "not on the trunk" is the
    tool's success that did not land - a merge train, auto-merge - and that is exit 6, not
    "merge it". Rule 5 is reported, not enforced: a ship that landed as a merge commit is
    said out loud. A dry run fetches, decides, and prints every mutating step as `would:`;
    the records stay.

    Which records: see `pending_to_land`. With several, each verified one lands - one
    fast-forward of the trunk, then each branch - and the rest are reported and kept; none
    verified is the same exit 2 as for one. Whatever is still pending afterwards is listed
    as `status` lists it. The record of the branch HEAD is on, picked because nothing was
    named, answers for itself only: when it has not landed, the exit 2 names every other
    record that has, each as the `forkflow land <branch>` that lands it.

    Order: a plain `land` refuses what it cannot do here (a stopped rebase, a dirty tree, the
    trunk checked out in another worktree) before it fetches. After `--merge` the verdict
    comes first: whether the request reached the trunk is the one thing that run has to
    say, and a queued merge is exit 6 wherever the trunk is checked out - refused as "the
    trunk is elsewhere" it read as merged, and `land` there then said "merge it" of a request
    that was already queued."""
    chosen = pending_to_land(ctx, entry, branch, force)
    several = len(chosen) > 1
    implicit = (entry is None and not branch and not several
                and chosen[0]["branch"] == current_branch(ctx))
    resume = land_cmd("" if several else chosen[0]["branch"], force)
    if not after_merge:
        land_preflight(ctx, resume)

    trunk_name = f"{ctx.origin}/{ctx.trunk}"
    rc, cmd, result, err, _ = fetch(ctx, (ctx.origin,), [trunk_name])
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    step("fetch", cmd, result)

    landings = judge_landings(ctx, chosen, force, after_merge, implicit)

    if after_merge:                        # landed: now what this worktree cannot do
        land_preflight(ctx, resume)
    old, new = land_trunk(ctx)

    leftovers = []
    for e, sha, how in landings:
        name = e["branch"]
        own_branch = name not in (ctx.trunk, ctx.mirror)
        if sha is not None and own_branch:
            land_branch(ctx, e, sha, how)
        elif sha is None:
            print(f"  `{name}` is kept: its landing was not verified")
        # a stale `origin/<branch>` goes under `--force` too: the kept branch is the one most
        # likely to be shipped again, and that is decided by origin's answer, not by the
        # landing. The remote-delete hint is for a verified landing only
        if own_branch and remote_branch_left(ctx, name) and sha is not None:
            leftovers.append(name)
        if not ctx.dry_run and not forget_pending(ctx, e):
            step("pending", "-", f"kept: the record for `{name}` changed while this ran (a new "
                                 f"ship of it?) - it is not the one that landed")
        elif not ctx.dry_run and pending_entries(ctx).get(name) == e:
            # cleared, says `forget_pending`, and it is still there: the write failed
            step("pending", "-", f"NOT cleared: `{STATE_FILE}` could not be written - `{name}` "
                                 f"has landed and keeps showing as pending until "
                                 f"`{land_cmd(name)}` can clear it")

    report_landing(ctx, landings, leftovers, several, old, new)


def land_branch(ctx: Ctx, entry: dict, sha: str, how: str) -> None:
    """Delete the landed branch - what landed is the commit that was pushed, not whatever
    the branch holds now. A commit made on it after the ship is on no trunk and, pushed by
    nobody, on no remote: the branch goes only while its tip is still exactly the recorded
    commit. Then `-d` for an ancestor landing, and `-D` for a rewritten one, which `-d`
    refuses (the tip is unreachable from the trunk) - the patch is verifiably on the trunk,
    and the tip is the commit whose patch it is. `-d` alone would not be the guard: `push`
    sets the branch's upstream, and `-d` accepts a tip that `origin/<branch>` contains."""
    branch = entry["branch"]
    flag = "-d" if how == "ancestor" else "-D"
    cmd = f"git branch {flag} {sh_arg(branch)}"
    tip = rev(ctx.root, f"refs/heads/{branch}")
    if not tip:
        step("branch", cmd, f"`{branch}` is already gone")
    elif tip != entry["commit"]:
        step("branch", f"git rev-parse {sh_arg(branch)}",
             f"kept: `{branch}` is at {short(tip)}, not the {short(entry['commit'])} that "
             f"was pushed - its later commits did not land")
    elif ctx.dry_run:
        step("branch", cmd, f"would delete `{branch}` (landed as {short(sha)})", dry=True)
    else:
        rc, out, err = git_rc("branch", flag, branch, cwd=ctx.root)
        if rc != 0:                 # the landing is done; a branch that will not go is said
            why = ((err or out).strip().splitlines() or [f"git exit {rc}"])[-1]
            step("branch", cmd, f"NOT deleted: {why}")
        else:
            step("branch", cmd, f"deleted (landed as {short(sha)})")


def origin_has_branch(ctx: Ctx, branch: str) -> Optional[bool]:
    """Origin's own answer (`ls-remote`), not a remote-tracking ref: None when it cannot be
    asked."""
    rc, out, _ = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{branch}",
                        cwd=ctx.root)
    return None if rc != 0 else bool(out.strip())


def drop_stale_tracking(ctx: Ctx, branch: str) -> None:
    """Remove `origin/<branch>` once origin has said it has no such branch. Left behind -
    GitLab's `--remove-source-branch`, or anyone's delete, is not seen by a fetch that does
    not prune - it is the lease the next `ship` of that name offers, and the push refuses a
    lease on a ref that no longer exists ("stale info")."""
    tracking = f"{ctx.origin}/{branch}"
    cmd = f"git branch -d -r {sh_arg(tracking)}"
    if ctx.dry_run:
        step("origin", cmd, f"would remove `{tracking}` (gone from {ctx.origin})", dry=True)
        return
    rc, out, err = git_rc("branch", "-d", "-r", tracking, cwd=ctx.root)
    step("origin", cmd, f"removed - `{branch}` is gone from {ctx.origin}" if rc == 0
         else f"NOT removed: {((err or out).strip().splitlines() or [''])[-1]}")


def remote_branch_left(ctx: Ctx, branch: str) -> bool:
    """After a landing: is `branch` still on origin? When origin no longer has it, the
    remote-tracking ref this clone kept is stale and goes too (`drop_stale_tracking`).
    Answers False when there is nothing on origin, or nothing is known here to begin with."""
    if not has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{branch}"):
        return False
    there = origin_has_branch(ctx, branch)
    if there is None:
        return False                    # unreachable: say nothing, remove nothing
    if not there:
        drop_stale_tracking(ctx, branch)
    return there


def cmd_land(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=False)
    header(ctx, "land")
    land_pending(ctx, force=bool(getattr(args, "force", False)),
                 branch=getattr(args, "branch", None) or "")
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
        (f'merge = {toml_string(MERGE_MANUAL)}',
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
    rc, cmd, result, err, _ = fetch(ctx, (ctx.origin, upstream))
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
         f"`{m}` is a pure copy of `{ctx.up()}` (behind is fine - "
         f"`{rerun_cmd('sync', None)}` advances it)")


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


def shown_argv(cmd: Sequence[str]) -> str:
    """An argv list as the one line printed for it: quoted with `shlex.quote`, so what is
    shown is exactly the argv that ran. The merge request's two steps both run a tool and
    print what they ran; this is the one place that renders it."""
    return " ".join(shlex.quote(c) for c in cmd)


def sh_arg(value: str) -> str:
    """One word of a command this script *prints* for a human or Claude to paste.

    Every branch and remote name reaches such a command, and `git check-ref-format` accepts
    `;`, a backtick and `$(` - names that come from `.forkflow.toml`, a tracked file a sync
    can bring in from upstream. Quoted only when it has to be (`shlex.quote`, as
    `shown_argv` renders an argv), so an ordinary name prints exactly as the reader expects
    and only a name that would otherwise be more than one word changes shape."""
    return shlex.quote(value)


def sh_quote(value: str) -> str:
    """`value` as one single-quoted shell word. Always quoted, unlike `shlex.quote`, so the
    hook reads the same whatever the name is - and a value carrying `'` or `$` cannot end
    the word early and turn the rest of it into code.

    Used for the generated hook and for the platform report's fix commands, which are pasted
    into a shell by hand; `shown_argv` renders an argv list this script also runs itself, and
    quotes that with `shlex.quote` so what is shown is exactly the argv that ran."""
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
    if not tracked_config_names(ctx.root):
        print(f"    it is untracked: when you are happy with it, commit it on a branch off "
              f"{ctx.origin}/{ctx.trunk} and ship that - never on the trunk or the mirror - so "
              f"the branch names and the gate are the same for everyone")


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
                             "write no config or hook - and fetch nothing (`git ls-remote` "
                             "reports what each remote has instead). The merge is still "
                             "simulated, into a scratch directory")
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
                    help="open the merge request, merge it, then land it (you end on the "
                         "trunk); needs merge = \"self\" in " + CONFIG_FILE)
    sy.add_argument("--title", help="merge-request title")

    sh = sub.add_parser("ship", parents=[common], help="squash a feature branch onto the trunk's tip")
    sh.add_argument("--continue", dest="cont", action="store_true",
                    help="resume after `git rebase --continue`")
    sh.add_argument("--mr", action="store_true", help="run the merge-request command")
    sh.add_argument("--merge", action="store_true",
                    help="open the merge request, merge it, then land it (you end on the "
                         "trunk); needs merge = \"self\" in " + CONFIG_FILE)
    sh.add_argument("--title", help="merge-request title")
    sh.add_argument("--message-file", help="file with the squashed commit message")

    la = sub.add_parser("land", parents=[common],
                        help="the MR is merged: fast-forward the local trunk and delete the branch")
    la.add_argument("branch", nargs="?", metavar="BRANCH",
                    help="land this branch's pending record (default: the current branch's, "
                         "else every pending one that has landed)")

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

    def odb(root: str) -> set:
        """Every file in a clone's object database - loose objects, packs and their indexes.

        What a `--dry-run` must leave exactly as it found it, along with `FETCH_HEAD` and the
        remote-tracking refs: `git fetch` writes all three and `git merge-tree --write-tree`
        writes objects, and the earlier rounds' snapshots covered neither."""
        objects = git_path(root, "objects")
        return {os.path.join(where, name)
                for where, _, names in os.walk(objects) for name in names}

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

    def post_receive(tmp: str, pattern: str, action: str) -> None:
        """A post-receive hook in the bare origin that runs `action` (shell, with `$ref` and
        `$new`) for every accepted ref matching `pattern` - after the push has succeeded."""
        hooks = os.path.join(tmp, "origin.git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        body = ("#!/bin/sh\n"
                "while read old new ref; do\n"
                "  case \"$ref\" in %s) %s;; esac\n"
                "done\nexit 0\n" % (pattern, action))
        path = os.path.join(hooks, "post-receive")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def delete_after_receive(tmp: str, pattern: str = "refs/heads/backup/*") -> None:
        """Origin drops the ref it has just accepted: the push reports success, so only the
        ls-remote confirmation can catch it."""
        post_receive(tmp, pattern, 'git update-ref -d "$ref" "$new"')

    def commit_after_receive(tmp: str, pattern: str = "refs/heads/feat/*",
                             onto: str = "") -> None:
        """Origin puts one more commit on the branch it has just accepted - a teammate
        pushing between the push and the merge; only the head-commit guard at merge time can
        catch it. With `onto`, the commit goes on that branch instead: somebody else's merge
        request landing on the trunk in the same window."""
        target = 'refs/heads/%s' % onto if onto else '"$ref"'
        post_receive(tmp, pattern,
                     'git update-ref %s "$(git commit-tree "%s^{tree}" -p %s -m teammate)"'
                     % (target, target, target))

    MERGING_TOOL_URL = {"glab": "https://example.invalid/-/merge_requests/1",
                        "gh": "https://example.invalid/pull/1"}

    # the fixture fork really does push to a local origin, so the platform and the fork it
    # names are both supplied here; `TestMrCommandsNameTheFork` proves the URL these stand for
    # is built from the origin. What the tests using this prove is that the argv the tool
    # receives carries it - a fake `gh`/`glab` resolves no base repository itself
    PLATFORM_FORKS = {"gitlab": "ssh://gitlab.example.com/acme/team/widget",
                      "github": "https://github.com/acme/widget"}

    def on_platform(platform: str):
        """The fixture fork as a fork on `platform`, named as `PLATFORM_FORKS` has it."""
        return mock.patch.multiple(sys.modules[__name__],
                                   detect_platform=lambda url: platform,
                                   mr_target=lambda ctx: (PLATFORM_FORKS[platform], ""))

    def as_gitlab():
        """The fixture fork as a GitLab fork (`on_platform`), for the tests that read what
        a merge request command was given."""
        return on_platform("gitlab")

    def as_github():
        return on_platform("github")

    def merging_tool(tmp: str, name: str, trunk: str = "develop") -> None:
        """A glab/gh on PATH that opens AND merges: the platform simulated, not faked away.

        Its argv goes one argument per line into `<name>-<subcommand>-argv.txt` - `create`
        and `merge` get separate logs, because the one binary is run twice under `--merge`
        and a single truncating log would lose the create argv and the body copy
        (`<name>-body-copy.md`). On `create` it prints a URL in the platform's shape
        (`FORKFLOW_FAKE_CREATE=fail` makes it exit 1, `=nourl` succeed without a URL). On
        `merge` it reads the sha from its own `--sha` / `--match-head-commit`, compares it
        with the tip of the source branch in the bare origin and exits 1 with "head
        mismatch" when they differ - so the head-commit guard is tested as behaviour, not
        as an argv string. glab without `--auto-merge=false` does what the real one does:
        arms merge-when-pipeline-succeeds, says so, exits 0 and moves nothing. Then it
        merges the way the platform would: glab fast-forwards the bare origin's trunk
        (`update-ref`) and refuses, as GitLab's ff method does, when the trunk is not an
        ancestor of the tip; gh `--merge` makes a real merge commit ("Create a merge
        commit" always does, even when a fast-forward was possible) and gh `--rebase`
        cherry-picks the commit - a one-commit rebase, new SHA, same patch - both in a temp
        clone of the origin pushed back, since a bare repository can do neither.
        `--remove-source-branch` deletes the source branch on origin once merged.
        `FORKFLOW_FAKE_FAIL=1` in the environment makes the merge exit 1 with a stderr
        line and move nothing."""
        origin_git = os.path.join(tmp, "origin.git")
        script = (
            'sub="$2"\n'
            'for a in "$@"; do echo "$a"; done > {logs}/{name}-"$sub"-argv.txt\n'
            'case "$sub" in\n'
            'create)\n'
            '  for a in "$@"; do [ -f "$a" ] && cp "$a" {body}; done\n'
            '  case "$FORKFLOW_FAKE_CREATE" in\n'
            '  fail) echo "{name}: not authenticated (FORKFLOW_FAKE_CREATE)" >&2; exit 1;;\n'
            '  nourl) echo "Creating merge request"; exit 0;;\n'
            '  esac\n'
            '  echo "Creating merge request"\n'
            '  echo {url}\n'
            '  exit 0;;\n'
            'merge)\n'
            '  if [ -n "$FORKFLOW_FAKE_FAIL" ]; then\n'
            '    echo "{name}: merge refused (FORKFLOW_FAKE_FAIL)" >&2; exit 1\n'
            '  fi\n'
            '  branch="$3"; want=""; method=""; now=0; rmsrc=0; prev=""\n'
            '  for a in "$@"; do\n'
            '    case "$prev" in --sha|--match-head-commit) want="$a";; esac\n'
            '    case "$a" in\n'
            '    --rebase|--merge|--squash) method="$a";;\n'
            '    --auto-merge=false) now=1;;\n'
            '    --remove-source-branch) rmsrc=1;;\n'
            '    esac\n'
            '    prev="$a"\n'
            '  done\n'
            '  tip=$(git --git-dir={origin} rev-parse "refs/heads/$branch") || exit 1\n'
            '  if [ "$want" != "$tip" ]; then\n'
            '    echo "{name}: head mismatch: $branch is at $tip, not $want" >&2; exit 1\n'
            '  fi\n'
            '  if [ {name} = glab ] && [ "$now" = 0 ]; then\n'
            '    echo "{name}: auto-merge enabled: $branch merges when its pipeline succeeds"\n'
            '    exit 0\n'
            '  fi\n'
            '  case "$method" in\n'
            '  --rebase|--merge)\n'
            '    clone={clone}; rm -rf "$clone"\n'
            '    git clone -q -b {trunk} {origin} "$clone" >/dev/null 2>&1 || exit 1\n'
            '    git -C "$clone" config commit.gpgsign false\n'
            '    GIT_COMMITTER_NAME="{name} platform" GIT_COMMITTER_EMAIL=noreply@example.invalid\n'
            '    export GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL\n'
            '    if [ "$method" = --rebase ]; then\n'
            '      git -C "$clone" cherry-pick "$tip" >/dev/null 2>&1 || {{\n'
            '        echo "{name}: rebase and merge failed" >&2; exit 1; }}\n'
            '    else\n'
            '      git -C "$clone" merge --no-ff -q -m "Merge pull request from $branch" \\\n'
            '        "$tip" >/dev/null 2>&1 || {{\n'
            '        echo "{name}: create a merge commit failed" >&2; exit 1; }}\n'
            '    fi\n'
            '    git -C "$clone" push -q origin HEAD:{trunk} >/dev/null 2>&1 || exit 1;;\n'
            '  *)\n'
            '    trunk=$(git --git-dir={origin} rev-parse refs/heads/{trunk}) || exit 1\n'
            '    git --git-dir={origin} merge-base --is-ancestor "$trunk" "$tip" || {{\n'
            '      echo "{name}: fast-forward merge is not possible - rebase needed" >&2; exit 1; }}\n'
            '    git --git-dir={origin} update-ref refs/heads/{trunk} "$tip" "$trunk" || exit 1;;\n'
            '  esac\n'
            '  if [ "$rmsrc" = 1 ]; then\n'
            '    git --git-dir={origin} update-ref -d "refs/heads/$branch" "$tip" || exit 1\n'
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

    def lines_of(path: str) -> list:
        """A recorded file as its lines, newline stripped - one argument per line, which is
        how every fake tool here writes the argv it was given."""
        with open(path) as fh:
            return [ln.rstrip("\n") for ln in fh]

    def tool_argv(tmp: str, name: str, sub: str) -> list:
        """What `merging_tool` recorded for `<name> <mr|pr> <sub>`, [] when it never ran."""
        path = os.path.join(tmp, "%s-%s-argv.txt" % (name, sub))
        return lines_of(path) if os.path.exists(path) else []

    def put_pending(fork: str, *entries: dict) -> None:
        """The shared `pending` map holding exactly `entries`, keyed by branch as
        `record_pending` keeps it - for a record no run of this clone could have written."""
        write_state(ctx_for(fork, need_trunk=False, strict_mirror=False), "pending",
                    {e["branch"]: e for e in entries})

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

    def printed_cmd(text: str, start: str) -> str:
        """The first command in `text` that begins with `start` - `backticked`, or the tail
        of a `then: forkflow ...` line: a printed remedy, to be run exactly as the user
        would paste it."""
        for cmd in (re.findall(r"`([^`]+)`", text)
                    + re.findall(r"then: (forkflow [^`\n]+)", text)):
            if cmd.startswith(start):
                return cmd.strip()
        raise AssertionError("no `%s...` printed in: %s" % (start, text))

    def remedy_of(text: str) -> Tuple[str, str]:
        """The printed case-variant remedy - (the command as printed, the path it copies the
        working file to first) - read off the command itself, `test ! -e <path> && cp ...`."""
        cmd = printed_cmd(text, "test ! -e ")
        return cmd, shlex.split(cmd)[3]

    def no_remedy(text: str) -> None:
        """Nothing in `text` to run: no command that touches the file or commits."""
        for start in ("test ", "cp ", "mv ", "git mv", "git rm", "git checkout", "git update-index",
                      "git commit"):
            if any(c.startswith(start) for c in re.findall(r"`([^`]+)`", text)):
                raise AssertionError("a `%s...` command is printed in: %s" % (start, text))

    def run_printed(text: str, start: str, fork: str):
        """Run the printed `forkflow ...` command that begins with `start`, in `fork`."""
        return run("-C", fork, *shlex.split(printed_cmd(text, start))[1:])

    SHELL_VERBS = ("test ", "cp ", "mv ", "rm ", "printf ", "git ")

    def run_every_printed(text: str, fork: str) -> list:
        """Run EVERY shell command `text` prints, in order, exactly as printed, in `fork`;
        answer what each one exited with.

        A refusal is read by a person who pastes what it shows them, and twice on this
        branch a remedy that read well did something else when it was actually run. So the
        tests that cover a printed remedy run the whole of it, from the state it was printed
        in, and then look at the fork."""
        ran = []
        for cmd in re.findall(r"`([^`]+)`", text):
            if not cmd.startswith(SHELL_VERBS):
                continue
            ran.append((cmd, subprocess.run(["sh", "-c", cmd], cwd=fork,
                                            capture_output=True).returncode))
        return ran

    def without_stamp(text: str) -> str:
        """The same message with `keep_aside`'s `forkflow-config-<UTC>.toml` names taken
        out. Two calls a second apart word a refusal identically but for that stamp, so a
        test comparing one refusal with another must not depend on which side of a tick of
        the clock it landed on."""
        return re.sub(r"forkflow-config-\d{8}-\d{6}\.toml", "forkflow-config-STAMP.toml", text)

    def kept_configs(fork: str) -> list:
        """The copies a remedy made, newest name last - `keep_aside`'s destinations."""
        gitdir = os.path.dirname(git_path(fork, "x"))
        return sorted(os.path.join(gitdir, n) for n in os.listdir(gitdir)
                      if n.startswith("forkflow-config-"))

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

        def value(self, argv: Sequence[str], flag: str) -> str:
            """The argument after `flag` in a recorded argv - which has to carry the flag."""
            self.assertIn(flag, argv)
            return argv[list(argv).index(flag) + 1]

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
            # the way out it names keeps the file and its settings: every line a comment
            self.assertIn("into a `#` comment", str(cm.exception))
            path = os.path.join(self.tmp, CONFIG_FILE)
            with open(path) as fh:
                lines = fh.read().splitlines()
            write(self.tmp, CONFIG_FILE, "".join("# %s\n" % ln for ln in lines))
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                self.assertEqual(load_config(self.tmp), {})
            with open(path) as fh:
                self.assertIn('trunk = "trunk"', fh.read())

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

        def test_a_name_that_differs_only_in_case_is_refused(self):
            """The directory lists `.ForkFlow.toml` and no `.forkflow.toml` - true on every
            filesystem once that one file is written. A case-insensitive one would open it
            as the config while git's case-exact paths call it absent; a case-sensitive one
            would silently ignore the file every other tool there reads. Refused either way,
            naming the file that was found."""
            for variant in (".ForkFlow.toml", ".FORKFLOW.TOML", ".forkflow.TOML"):
                path = write(self.tmp, variant, 'gate = ["true"]\n')
                self.assertIn(variant, os.listdir(self.tmp))
                self.assertNotIn(CONFIG_FILE, os.listdir(self.tmp))
                with self.assertRaises(Fail) as cm:
                    load_config(self.tmp)
                self.assertEqual(cm.exception.code, 2, variant)
                self.assertIn("`%s`" % variant, str(cm.exception))
                os.unlink(path)
            self.assertEqual(load_config(self.tmp), {})

        @needs_tomllib
        def test_an_untracked_variant_is_renamed_by_the_command_printed(self):
            """Nothing tracked: the printed remedy copies the file into the git directory,
            then renames it to the exact name - run as printed, the file keeps its content
            and is read, and the copy is where the message said. No `.forkflow.toml` is
            listed beside it, so on either kind of filesystem the rename replaces nothing.
            The same line run twice stops before it could copy over the saved file."""
            sh("git", "init", "-q", self.tmp)
            write(self.tmp, ".ForkFlow.toml", 'trunk = "mine"\n')
            with self.assertRaises(Fail) as cm:
                load_config(self.tmp)
            cmd, kept = remedy_of(str(cm.exception))
            sh("sh", "-c", cmd, cwd=self.tmp)
            self.assertIn(CONFIG_FILE, os.listdir(self.tmp))
            self.assertNotIn(".ForkFlow.toml", os.listdir(self.tmp))
            self.assertEqual(load_config(self.tmp), {"trunk": "mine"})
            self.assertEqual(os.path.dirname(kept), os.path.join(self.tmp, ".git"))
            with open(kept) as fh:
                self.assertEqual(fh.read(), 'trunk = "mine"\n')
            write(self.tmp, CONFIG_FILE, 'trunk = "edited since"\n')
            self.assertNotEqual(subprocess.run(["sh", "-c", cmd], cwd=self.tmp,
                                               capture_output=True).returncode, 0)
            with open(kept) as fh:
                self.assertEqual(fh.read(), 'trunk = "mine"\n')             # not copied over
            self.assertEqual(load_config(self.tmp), {"trunk": "edited since"})

        @needs_tomllib
        def test_the_exact_name_is_read_when_a_variant_sits_beside_it(self):
            """Only a case-sensitive filesystem can hold both; there git and the open() agree
            on which file is `.forkflow.toml`, so it is read and the variant is not the
            config."""
            write(self.tmp, CONFIG_FILE, 'trunk = "trunk"\n')
            with mock.patch.object(os, "listdir", return_value=[".ForkFlow.toml", CONFIG_FILE]):
                self.assertEqual(load_config(self.tmp), {"trunk": "trunk"})

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
            code, out, err = run("-C", fork, "ship", "--dry-run")   # origin only, and no fetch
            self.assertEqual(code, 0, err + out)
            # a dry run writes nothing, so it does not fetch either - and against an
            # `upstream/*` this stale it says that, rather than calling a teammate's sync
            # rule 6. The real run fetches upstream itself and goes through
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 2, err + out)
            self.assertIn("did not fetch", err)
            code, out, err = run("-C", fork, "sync")
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
            # checkout it would run, with the trunk's name in it (the fast-forward, the branch
            # deletion and the remote delete: `test_a_verified_landing_prints_every_...`)
            put_pending(fork, {"kind": "ship", "branch": "feat/x", "commit": rev(fork, "feat/x"),
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

        @needs_tomllib
        def test_a_verified_landing_prints_every_command_quoted(self):
            """The landing that moves the trunk and deletes a branch prints the most: the
            checkout, the fast-forward, the branch deletion and, on GitHub, the remote
            delete - with a hostile trunk name and a hostile branch name in them."""
            feature = "feat;touch$(id)"
            fork = make_fork(self.tmp, trunk=self.TRUNK, config='trunk = "%s"\n' % self.TRUNK)
            sh("git", "checkout", "-b", feature, self.TRUNK, cwd=fork)
            commit = commit_fork(fork, "ours/f.txt", "ours\n", "ours: step")
            sh("git", "push", "origin", "refs/heads/%s:refs/heads/%s" % (feature, feature),
               cwd=fork)
            put_pending(fork, {"kind": "ship", "branch": feature, "commit": commit,
                               "base": origin_sha(fork, self.TRUNK), "mr": ""})
            sh("git", "--git-dir=" + os.path.join(self.tmp, "origin.git"), "update-ref",
               "refs/heads/" + self.TRUNK, commit)                  # merged: the trunk moves
            sh("git", "fetch", "-q", "origin", cwd=fork)     # a dry run of its own does not
            with on_platform("github"):
                code, out, err = run("-C", fork, "land", "--dry-run")
            self.assertEqual(code, 0, err + out)
            printed = self.commands(out)
            for verb in ("git checkout ", "git merge --ff-only ", "git branch -d ",
                         "git push origin --delete "):
                self.assertTrue([c for c in printed if c.startswith(verb)], verb + ":\n" + out)
            self.assertEqual(self.quoted(out, (self.TRUNK, feature)), {self.TRUNK, feature})

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
            """READING stays tolerant, so nothing that does not depend on the state file
            fails over one that cannot be read. Writing does not: see
            `test_a_state_file_that_cannot_be_read_refuses_merge_and_is_kept`."""
            ctx = ctx_for(make_fork(self.tmp))
            self.assertEqual(read_state(ctx), {})                        # absent
            self.assertEqual(state_unreadable(ctx), "")                  # and no complaint
            for text in ("{not json", '["a", "list"]', ""):
                with open(state_path(ctx), "w") as fh:
                    fh.write(text)
                self.assertEqual(read_state(ctx), {}, repr(text))
                self.assertEqual(resumable(ctx, "ship", "feat/x"), {})
                self.assertIn(state_path(ctx), state_unreadable(ctx), repr(text))
            os.unlink(state_path(ctx))              # what the refusal says to do about it
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

        def test_the_pending_record_is_one_for_every_worktree(self):
            """What waits to land is the clone's, not the worktree's: a ship in a linked
            worktree lands where the trunk is checked out. The resume entries stay apart."""
            fork = make_fork(self.tmp)
            other = os.path.join(self.tmp, "wt")
            sh("git", "worktree", "add", "-b", "feat/other", other, "develop", cwd=fork)
            here, there = ctx_for(fork), ctx_for(other)
            write_state(there, "ship", {"branch": "feat/other", "backup": "there"})
            record_pending(there, "ship", "feat/other", rev(fork, "origin/develop"))
            self.assertEqual(state_path(here, shared=True), state_path(there, shared=True))
            self.assertEqual(state_path(here, shared=True), state_path(here))  # the main one's
            self.assertEqual(list(pending_entries(here)), ["feat/other"])
            self.assertEqual(pending_entries(here), pending_entries(there))
            self.assertEqual(resumable(here, "ship", "feat/other"), {})       # not shared
            write_state(here, "pending", None)
            self.assertEqual(pending_entries(there), {})
            self.assertEqual(resumable(there, "ship", "feat/other")["backup"], "there")

        def test_an_interrupted_write_leaves_the_old_file_whole(self):
            """Written in place, a write that dies half way truncates the JSON, and the next
            read answers {} - every record in it lost. Replaced whole, it is old or new."""
            ctx = ctx_for(make_fork(self.tmp))
            write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})

            def dies(data, fh, **kw):
                fh.write('{"half')
                raise OSError("disk full")

            with mock.patch.object(json, "dump", dies):
                write_state(ctx, "sync", {"branch": "sync/x", "backup": "b2"})
            self.assertEqual(resumable(ctx, "ship", "feat/x")["backup"], "b")
            self.assertEqual(os.listdir(os.path.dirname(state_path(ctx))).count(STATE_FILE), 1)
            self.assertEqual([f for f in os.listdir(os.path.dirname(state_path(ctx)))
                              if f.startswith(STATE_FILE + ".")],
                             [os.path.basename(state_lock_path(state_path(ctx)))])
            # no temp left: the one file beside it is the lock, which is never removed

        def test_a_write_that_cannot_happen_is_answered_not_swallowed(self):
            """Every failure here was swallowed, so a state file that cannot be written -
            a read-only git directory, a full disk, a `forkflow-state.json` that is not a
            file - was invisible, and the run went on to report a branch as landable with
            no record of it. The answer is the reason, for the caller to say what it cost."""
            ctx = ctx_for(make_fork(self.tmp))
            self.assertEqual(write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"}), "")
            os.unlink(state_path(ctx))
            os.mkdir(state_path(ctx))                       # nothing can be written there
            why = write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
            self.assertIn(STATE_FILE, why)
            self.assertIn("nothing was written", why)
            self.assertEqual(read_state(ctx), {})
            # a directory there cannot be read either, and that is what is answered first -
            # a file that cannot be READ is never written over. `save_state` answers for
            # itself too, so a caller reaching it directly is not left guessing
            self.assertIn(STATE_FILE, save_state(ctx, {"a": 1}))
            os.rmdir(state_path(ctx))
            self.assertEqual(write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"}), "")

        def test_the_read_and_the_write_it_leads_to_are_one_operation(self):
            """`os.replace` makes each write whole for a READER and does nothing about the
            window between a caller's read and its own write. Two worktrees recording their
            own ship read the same map, each adds its branch, and the second write puts the
            map back as the first found it: eight concurrent ships left ONE record, and the
            ships whose records went are landable by nothing the tool offers.

            Deterministic here: another run takes the file's lock and writes its record
            only after this one has asked for it. Unlocked, this run's read comes first and
            its write is the one the other run then overwrites."""
            import threading
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            sh("git", "branch", "feat/b", "develop", cwd=fork)
            path = state_path(ctx, shared=True)
            theirs = {"kind": "ship", "branch": "feat/a", "commit": "a" * 40,
                      "base": "b" * 40, "mr": ""}
            holding = threading.Event()

            def other_run():
                held = take_state_lock(path)
                holding.set()
                time.sleep(0.2)                    # long after this run has asked for it
                save_state(ctx, {"pending": {"feat/a": theirs}}, shared=True)
                drop_state_lock(held)

            thread = threading.Thread(target=other_run)
            thread.start()
            self.assertTrue(holding.wait(5))
            mine = record_pending(ctx, "ship", "feat/b", rev(fork, "origin/develop"))
            thread.join()
            self.assertEqual(pending_entries(ctx), {"feat/a": theirs, "feat/b": mine})
            # the lock file stays; what was given up is the kernel's lock on it, so the
            # next run takes it without anything having to decide the file is stale
            self.assertTrue(os.path.exists(state_lock_path(path)))
            drop_state_lock(take_state_lock(path))

        def test_the_lock_a_killed_run_held_is_released_by_the_operating_system(self):
            """The whole reason the lock is the kernel's. A real other process takes it and
            is KILLED holding it - no chance to clean up, the case the old design answered
            by calling a ten-minute-old lock file "stale" and unlinking it. Here nothing is
            judged and nothing is unlinked: the lock dies with the process that held it, so
            the next run takes it at once, and the lock FILE is still there afterwards
            because removing it is what let one run release another's."""
            if fcntl is None:                             # pragma: no cover - not POSIX
                self.skipTest("this platform's lock is not fcntl.flock")
            ctx = ctx_for(make_fork(self.tmp))
            path = state_path(ctx, shared=True)
            lock = state_lock_path(path)
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import fcntl, os, sys, time\n"
                 "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
                 "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                 "sys.stdout.write('held\\n')\n"
                 "sys.stdout.flush()\n"
                 "time.sleep(300)\n", lock],
                stdout=subprocess.PIPE, universal_newlines=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                with mock.patch.object(sys.modules[__name__], "STATE_LOCK_WAIT", 0.05):
                    why = write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
                self.assertIn("held by another forkflow run", why)   # while it lives
                self.assertEqual(read_state(ctx), {})
            finally:
                holder.kill()
                holder.wait()
                holder.stdout.close()
            started = time.monotonic()
            self.assertEqual(write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"}), "")
            self.assertLess(time.monotonic() - started, STATE_LOCK_WAIT)
            self.assertEqual(resumable(ctx, "ship", "feat/x")["backup"], "b")
            self.assertTrue(os.path.exists(lock))         # never removed, by anyone

        def test_a_platform_with_no_lock_refuses_rather_than_writing_unserialised(self):
            """`fcntl` on POSIX, `msvcrt` on Windows; a Python with neither cannot keep two
            runs apart, and the answer is a refusal that says which module is missing -
            never a write that silently loses another run's records."""
            ctx = ctx_for(make_fork(self.tmp))
            module = sys.modules[__name__]
            with mock.patch.object(module, "fcntl", None), \
                 mock.patch.object(module, "msvcrt", None):
                why = write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
            self.assertIn("neither `fcntl` nor `msvcrt`", why)
            self.assertIn("nothing was written", why)
            self.assertEqual(read_state(ctx), {})
            self.assertEqual(write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"}), "")

        def test_a_filesystem_that_will_not_lock_refuses_and_names_the_reason(self):
            """A network mount can answer the lock call with an error rather than with "held
            by somebody else". That is not another run to wait for, so there is nothing to
            wait for: it refuses at once, names what the operating system said, and writes
            nothing. No fallback - every fallback here is another staleness judgement."""
            if fcntl is None:                             # pragma: no cover - not POSIX
                self.skipTest("this platform's lock is not fcntl.flock")
            ctx = ctx_for(make_fork(self.tmp))

            def unsupported(fd, flags):
                raise OSError(errno.ENOLCK, "No locks available")

            with mock.patch.object(fcntl, "flock", unsupported):
                started = time.monotonic()
                why = write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
            self.assertLess(time.monotonic() - started, STATE_LOCK_WAIT)
            self.assertIn("will not lock", why)
            self.assertIn("No locks available", why)
            self.assertIn("nothing was written", why)
            self.assertEqual(read_state(ctx), {})

        def test_a_lock_held_now_is_waited_for_and_then_given_up(self):
            """The wait is bounded: a run that cannot get the lock answers why, and says
            nothing was written rather than writing over what the holder is writing."""
            ctx = ctx_for(make_fork(self.tmp))
            path = state_path(ctx, shared=True)
            held = take_state_lock(path)
            try:
                with mock.patch.object(sys.modules[__name__], "STATE_LOCK_WAIT", 0.05):
                    why = write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
            finally:
                drop_state_lock(held)
            self.assertIn("held by another forkflow run", why)
            self.assertIn(state_lock_path(path), why)   # the file, so it can be looked at
            self.assertIn("released the moment the run holding it ends", why)
            self.assertEqual(read_state(ctx), {})
            self.assertEqual(write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"}), "")

        def test_a_full_memory_refuses_for_good_and_never_drops_a_digest(self):
            """Hashes, never contents, and a bounded number of them - but reaching the bound
            FAILS CLOSED instead of forgetting. It used to drop the oldest, and HOW MANY
            versions get published is the original project's choice: publish enough and the
            digest of the file sitting untracked in this working tree falls out, then
            withdraw that version from every ref, and upstream's own config reads as this
            fork's own with no fail-closed condition firing (scratchpad `f10/repro15.py`).

            So nothing is dropped. The memory fills, the state file says so, and `--merge`
            is refused here from then on - which costs a real fork nothing, since no project
            publishes 500 versions of one small file, and costs this one an opt-in flag
            rather than any work."""
            ctx = ctx_for(make_fork(self.tmp))
            digests = ["%064x" % n for n in range(UPSTREAM_CONFIG_KEEP + 10)]
            known, why = remember_upstream_configs(ctx, set(digests[:5]))
            self.assertEqual((known, why), (set(digests[:5]), ""))
            for start in range(5, UPSTREAM_CONFIG_KEEP, 25):
                chunk = digests[start:min(start + 25, UPSTREAM_CONFIG_KEEP)]
                self.assertEqual(remember_upstream_configs(ctx, set(chunk))[1], "")
            kept = remembered_upstream_configs(ctx)
            self.assertEqual(len(kept), UPSTREAM_CONFIG_KEEP)
            self.assertEqual(sorted(kept), sorted(digests[:UPSTREAM_CONFIG_KEEP]))
            self.assertEqual(config_memory_unprovable(ctx), "")       # full, and still fine

            known, why = remember_upstream_configs(ctx, {digests[-1]})   # one too many
            self.assertEqual(known, set())
            self.assertIn(str(UPSTREAM_CONFIG_KEEP), why)
            self.assertIn("will not drop one to make room", why)
            self.assertIn("merged by hand", why)
            self.assertIn(state_path(ctx, shared=True), why)   # the file, and what losing it costs
            # nothing was dropped, and the refusal is now this clone's, not this run's
            self.assertEqual(sorted(remembered_upstream_configs(ctx)),
                             sorted(digests[:UPSTREAM_CONFIG_KEEP]))
            self.assertEqual(config_memory_unprovable(ctx), why)
            # and it stands on every later run, whatever that run walks - the one reader
            # every provenance answer goes through asks before it asks anything else
            self.assertEqual(upstream_config_digests(ctx), (set(), why))
            self.assertEqual(upstream_config_digests(ctx_for(ctx.root, dry_run=True)),
                             (set(), why))

        def test_a_full_memory_is_a_refusal_a_dry_run_makes_too(self):
            """A dry run must answer what the real run would, and write nothing doing it."""
            ctx = ctx_for(make_fork(self.tmp), dry_run=True)
            digests = {"%064x" % n for n in range(UPSTREAM_CONFIG_KEEP + 1)}
            known, why = remember_upstream_configs(ctx, digests)
            self.assertEqual(known, set())
            self.assertIn("will not drop one to make room", why)
            self.assertFalse(os.path.exists(state_path(ctx, shared=True)))

        def test_a_memory_another_run_filled_in_between_is_not_dropped_either(self):
            """The room left is worked out twice on purpose: once from what was read before
            the lock was taken, and again under it, because another worktree can fill the
            memory in between. Under the lock nothing is dropped either - the flag goes in
            and this run refuses too. Here the reader is made to answer as it would have
            before that other run wrote, which is the window itself."""
            ctx = ctx_for(make_fork(self.tmp))
            digests = ["%064x" % n for n in range(UPSTREAM_CONFIG_KEEP + 5)]
            full = digests[:UPSTREAM_CONFIG_KEEP]
            change_state(ctx, True, lambda data: data.update({"upstream_configs": full}))
            with mock.patch.object(sys.modules[__name__], "remembered_upstream_configs",
                                   lambda c: []):
                known, why = remember_upstream_configs(ctx, set(digests[-3:]))
            self.assertEqual(known, set())
            self.assertIn("will not drop one to make room", why)
            self.assertEqual(remembered_upstream_configs(ctx), full)      # nothing dropped
            self.assertEqual(config_memory_unprovable(ctx), why)          # and it stands

        def test_a_memory_that_cannot_be_written_is_a_refusal_not_a_short_answer(self):
            """A version the original project has since withdrawn would read as one it never
            had, so a memory that cannot be written answers with the reason and leaves the
            way out that needs no state file at all - have the request merged by hand."""
            ctx = ctx_for(make_fork(self.tmp))
            os.mkdir(state_path(ctx, shared=True))          # nothing can be written there
            known, why = remember_upstream_configs(ctx, {"a" * 64})
            self.assertEqual(known, set())
            self.assertIn("cannot write down which", why)
            self.assertIn(CONFIG_FILE, why)
            self.assertIn("merged by hand", why)
            os.rmdir(state_path(ctx, shared=True))
            self.assertEqual(remember_upstream_configs(ctx, {"a" * 64}), ({"a" * 64}, ""))

        def test_a_dry_run_writes_down_no_upstream_config(self):
            """The no-write contract: a dry run answers with the memory plus what it walked
            and leaves no file behind."""
            ctx = ctx_for(make_fork(self.tmp), dry_run=True)
            self.assertEqual(remember_upstream_configs(ctx, {"a" * 64}), ({"a" * 64}, ""))
            self.assertFalse(os.path.exists(state_path(ctx, shared=True)))

        def test_a_state_file_that_cannot_be_read_refuses_merge_and_is_kept(self):
            """An unreadable state file used to answer `{}` and say nothing, which forgets
            `upstream_configs` - the only thing that answers a version upstream has
            withdrawn from every ref. One truncated write and upstream's own file read as
            this fork's own (`f10/repro15.py`). Worse, the next write put the file back with
            one key in it, so the record was gone for good and the refusal could be flushed
            by any ordinary ship.

            Now: `--merge` is refused, naming the file and what deleting it costs; the file
            is not written over; and everything that does not depend on the memory goes on
            working, because a corrupt state file is not a reason for the tool to stop."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            shared = state_path(ctx, shared=True)
            remember_upstream_configs(ctx, {"a" * 64})
            with open(shared) as fh:
                whole = fh.read()
            for damage in (whole[:len(whole) // 2], "", "[1, 2]", "not json at all"):
                with open(shared, "w") as fh:
                    fh.write(damage)
                why = config_memory_unprovable(ctx)
                self.assertIn(shared, why, repr(damage))
                self.assertIn("delete it", why)
                self.assertIn("merged by hand", why)
                self.assertEqual(upstream_config_digests(ctx), (set(), why))
                self.assertEqual(fork_config_state(ctx)[0], "unprovable")
                self.assertEqual(fork_merge_mode(ctx), "manual")
                # not written over, and the reason says what the write would have cost
                failed = write_state(ctx, "ship", {"branch": "feat/x", "backup": "b"})
                self.assertIn(shared, failed)
                self.assertIn("nothing was written", failed)
                self.assertIn("lose whatever is in there for good", failed)
                with open(shared) as fh:
                    self.assertEqual(fh.read(), damage)
                # and what does not depend on the memory is unaffected
                self.assertEqual(read_state(ctx, shared=True), {})
                self.assertEqual(pending_entries(ctx), {})
                self.assertEqual(run("-C", fork, "status")[0], 0)
            os.unlink(shared)                       # the user's choice, and the way back
            self.assertEqual(config_memory_unprovable(ctx), "")
            self.assertEqual(remember_upstream_configs(ctx, {"a" * 64}), ({"a" * 64}, ""))

        def test_a_memory_that_is_not_a_list_of_digests_is_a_refusal_too(self):
            """The hand-edited shape of the same fact."""
            ctx = ctx_for(make_fork(self.tmp))
            for record in ({"a": 1}, "a" * 64, [1, 2], ["ok" * 32, 7]):
                change_state(ctx, True, lambda d: d.update({"upstream_configs": record}))
                why = config_memory_unprovable(ctx)
                self.assertIn("not a list of digests", why, repr(record))
                self.assertEqual(fork_config_state(ctx)[0], "unprovable")
                self.assertEqual(fork_merge_mode(ctx), "manual")

    class TestPendingEntry(Base):
        """`land` works from the `pending` record and nothing else, so what is read back
        has to be exactly what `record_pending` wrote - or nothing at all."""

        def test_round_trip_and_the_url_added_afterwards(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            self.assertEqual(pending_entries(ctx), {})
            sh("git", "branch", "feat/x", "develop", cwd=fork)
            base = rev(fork, "origin/develop")
            written = record_pending(ctx, "ship", "feat/x", base)
            self.assertEqual(pending_entries(ctx), {"feat/x": {
                "kind": "ship", "branch": "feat/x", "commit": rev(fork, "feat/x"),
                "base": base, "mr": ""}})
            self.assertEqual(written, pending_entries(ctx)["feat/x"])      # what it returns
            written = record_pending(ctx, "ship", "feat/x", base, "https://example.invalid/pull/1")
            self.assertEqual(pending_entries(ctx)["feat/x"]["mr"],
                             "https://example.invalid/pull/1")
            self.assertEqual(pending_entries(ctx)["feat/x"]["commit"], rev(fork, "feat/x"))
            self.assertEqual(written, pending_entries(ctx)["feat/x"])
            self.assertTrue(forget_pending(ctx, written))
            self.assertEqual(pending_entries(ctx), {})
            self.assertNotIn("pending", read_state(ctx, shared=True))   # no empty map left

        def test_one_entry_per_branch(self):
            """Worktrees ship different branches at the same time: each keeps its own entry,
            and only a second run of the same branch replaces one."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            base = rev(fork, "origin/develop")
            sh("git", "branch", "feat/x", "develop", cwd=fork)
            sh("git", "branch", "feat/y", "develop", cwd=fork)
            record_pending(ctx, "ship", "feat/x", base)
            record_pending(ctx, "sync", "feat/y", base)
            self.assertEqual(sorted(pending_entries(ctx)), ["feat/x", "feat/y"])
            self.assertEqual(pending_entries(ctx)["feat/x"]["kind"], "ship")
            self.assertEqual(pending_entries(ctx)["feat/y"]["kind"], "sync")
            commit_fork(fork, "ours/x.txt", "x\n", "ours: x")             # on develop
            sh("git", "branch", "-f", "feat/x", "develop", cwd=fork)
            record_pending(ctx, "ship", "feat/x", base, "https://example.invalid/pull/2")
            self.assertEqual(pending_entries(ctx)["feat/x"]["commit"], rev(fork, "feat/x"))
            self.assertEqual(pending_entries(ctx)["feat/y"]["kind"], "sync")   # untouched

        def test_forget_clears_only_the_entry_that_is_still_the_one_landed(self):
            """`land` clears what it landed - not a newer ship of the same branch recorded
            while it ran, and never another branch's entry."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            base = rev(fork, "origin/develop")
            sh("git", "branch", "feat/x", "develop", cwd=fork)
            sh("git", "branch", "feat/y", "develop", cwd=fork)
            landed_one = record_pending(ctx, "ship", "feat/x", base)
            other = record_pending(ctx, "ship", "feat/y", base)
            commit_fork(fork, "ours/x.txt", "x\n", "ours: x")
            sh("git", "branch", "-f", "feat/x", "develop", cwd=fork)
            newer = record_pending(ctx, "ship", "feat/x", base)          # shipped again
            self.assertFalse(forget_pending(ctx, landed_one))
            self.assertEqual(pending_entries(ctx), {"feat/x": newer, "feat/y": other})
            self.assertTrue(forget_pending(ctx, newer))
            self.assertEqual(pending_entries(ctx), {"feat/y": other})
            self.assertTrue(forget_pending(ctx, newer))                 # gone already: fine
            self.assertFalse(forget_pending(ctx_for(fork, dry_run=True), other))
            self.assertEqual(pending_entries(ctx), {"feat/y": other})

        def test_the_single_record_an_earlier_build_wrote_is_still_read(self):
            """An upgrade between a ship and its landing: the state file holds one bare
            entry, not a map. It reads as a map of one, and the next record keeps it."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            base = rev(fork, "origin/develop")
            sh("git", "branch", "feat/x", "develop", cwd=fork)
            sh("git", "branch", "feat/y", "develop", cwd=fork)
            old = {"kind": "ship", "branch": "feat/x", "commit": rev(fork, "feat/x"),
                   "base": base, "mr": ""}
            save_state(ctx, {"pending": old}, shared=True)
            self.assertEqual(pending_entries(ctx), {"feat/x": old})
            new = record_pending(ctx, "ship", "feat/y", base)
            self.assertEqual(pending_entries(ctx), {"feat/x": old, "feat/y": new})
            self.assertEqual(read_state(ctx, shared=True)["pending"],
                             {"feat/x": old, "feat/y": new})              # a map from now on

        def test_anything_but_the_written_shape_is_no_entry(self):
            ctx = ctx_for(make_fork(self.tmp))
            good = {"kind": "ship", "branch": "feat/x", "commit": "abc", "base": "def"}
            for bad in (["a", "list"],                                   # not a dict
                        {k: v for k, v in good.items() if k != "commit"},  # missing a field
                        dict(good, commit=1),                            # non-string field
                        dict(good, base=None),
                        dict(good, mr=7),                                # non-string URL
                        "abc"):
                for raw in (bad, {"feat/x": bad}):                       # bare or in the map
                    save_state(ctx, {"pending": raw})
                    self.assertEqual(pending_entries(ctx), {}, repr(raw))
            save_state(ctx, {"pending": {"feat/y": good}})               # under another name
            self.assertEqual(pending_entries(ctx), {})
            save_state(ctx, {"pending": {"feat/x": good, "feat/z": dict(good, kind=3)}})
            self.assertEqual(pending_entries(ctx), {"feat/x": good})     # `mr` may be absent

        def test_a_dry_run_records_nothing(self):
            """Asked of the shared file (`SHARED_STATE`), which is where `record_pending`
            writes: the per-worktree path is the same file in a main worktree, so asking
            that one would pass in a linked worktree with the record written."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork, dry_run=True)
            record_pending(ctx, "ship", "develop", rev(fork, "origin/develop"))
            self.assertFalse(os.path.exists(state_path(ctx, shared=True)))

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
                self.assertEqual(branch_worktree(ctx, "main"), "")   # `develop` is checked out
                sh("git", "switch", "main", cwd=fork)
                self.assertEqual(branch_worktree(ctx, "main"), ctx.root)

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

        @unittest.skipIf(git_version() < MERGE_TREE_GIT, "needs git 2.38+")
        def test_the_simulation_writes_no_object_into_this_clone(self):
            """`merge-tree --write-tree` writes the merged tree and a blob per conflicting
            file into the object database. Nothing needs them after the call - both callers
            read stdout - and a `--dry-run` that promises to write nothing, and a `status`
            that only reports, both reach this. So they go to a scratch directory and the
            answer is the same one the unredirected call gives."""
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf",
                        'resource "null_resource" "a" {\n  count = 2\n}\n', "ours: shared",
                        push=True)
            commit_upstream(self.tmp, "shared.tf",
                            'resource "null_resource" "a" {\n  count = 3\n}\n')
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n")
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)
            target = rev(fork, ctx.up())
            before = odb(fork)
            result, _, _ = capture(simulate_merge, ctx, target)
            self.assertEqual(result, (False, ["shared.tf"]))
            self.assertEqual(odb(fork), before)               # not one object
            # the same question asked the way git would answer it on its own
            plain = git_rc("merge-tree", "--write-tree", "--name-only",
                           "origin/develop", target, cwd=fork)
            redirected = merge_tree(fork, "--write-tree", "--name-only",
                                    "origin/develop", target)
            self.assertEqual(redirected, plain)
            self.assertNotEqual(odb(fork), before)            # git's own call did write

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

        def test_a_dry_run_does_not_call_a_stale_fork_already_in_sync(self):
            """"Already in sync" is a CONCLUSION - it returns 0 and skips every preflight
            below it - and a dry run does not fetch, so on refs the server has moved past it
            is a conclusion about a commit that is no longer upstream's tip. Said there, it
            hid the merge simulation and the untracked-in-the-way and case-collision checks
            the dry run exists to run, two lines under a `fetch` line reporting the newer
            commit. `advance_mirror` and `judge_landings` already said so where they would
            conclude from a stale ref; this is the third.

            The refusal is about STALENESS and not about `--dry-run`: with `upstream/<branch>`
            the same here as on the server, the same dry run still answers "already in sync"
            and exits 0, both with nothing at all to take and with the trunk already carrying
            what upstream has."""
            fork = make_fork(self.tmp)
            code, out, err = run("-C", fork, "sync", "--dry-run")   # nothing new anywhere
            self.assertEqual(code, 0, err + out)
            self.assertIn("already in sync", out)

            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            before = sh("git", "for-each-ref", cwd=fork)
            code, out, err = run("-C", fork, "sync", "--dry-run")   # not fetched here
            self.assertEqual(code, 2, err + out)
            self.assertIn("did not fetch", err)
            self.assertNotIn("already in sync", out)
            self.assertIn("NOT fetched (dry run)", out)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), before)   # and wrote nothing

            # a teammate synced it and this clone fetched: the trunk carries what upstream
            # has and every ref is the server's, so the same dry run concludes again
            push_upstream_into_origin(self.tmp, "develop")
            push_upstream_into_origin(self.tmp, "main")
            sh("git", "fetch", "-q", "--multiple", "origin", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("already in sync", out)

            # and the ref that decides is `upstream/<branch>` alone. Somebody pushed to the
            # trunk on origin and this clone has not fetched that either - it changes
            # nothing about whether the trunk already carries what upstream has, so
            # refusing on it would be a refusal of an answerable question
            second_clone_commit(self.tmp)
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("already in sync", out)
            self.assertIn("NOT fetched (dry run)", out)      # origin/develop IS stale here

        def test_a_stale_dry_run_still_runs_the_collision_preflight_after_a_fetch(self):
            """What the refusal is protecting: the untracked-in-the-way preflight. Upstream
            starts tracking a `.forkflow.toml` while this fork keeps its own untracked, which is
            exit 2 with the remedy - and from the refs on disk it was exit 0 "already in
            sync" with nothing checked at all."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "self"\n')       # untracked, as `setup` leaves it
            commit_upstream(self.tmp, CONFIG_FILE, 'merge = "manual"\n', "theirs: config")
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 2, err + out)
            self.assertIn("did not fetch", err)
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 2, err + out)
            self.assertIn("overwrite an untracked file", err)

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
            sh("git", "fetch", "-q", "upstream", cwd=fork)   # the dry run of its own does not

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

        def test_a_dry_run_fetches_nothing_and_simulates_without_writing_objects(self):
            """The no-write contract against the two writing steps a `sync --dry-run` took.
            `git fetch` rewrites `FETCH_HEAD`, moves the remote-tracking refs and brings
            objects in: `git ls-remote` asks the same server, writes none of them, and the
            line says what upstream has and that what follows is judged from the refs on
            disk. `git merge-tree --write-tree` writes the merged tree and a blob per
            conflict: the simulation still runs, with the objects sent to a scratch
            directory (`merge_tree`), so the answer is the same and the clone gains
            nothing."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            commit_upstream(self.tmp, "docs/more.md", "more\n", "theirs: more")   # unfetched
            fetch_head = git_path(fork, "FETCH_HEAD")
            if os.path.exists(fetch_head):
                os.unlink(fetch_head)
            before, refs = odb(fork), sh("git", "for-each-ref", cwd=fork)

            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("git ls-remote upstream refs/heads/main", out)
            self.assertIn("NOT fetched (dry run)", out)
            self.assertIn("1 upstream commit(s) to take", out)       # from the refs on disk
            if git_version() >= MERGE_TREE_GIT:
                self.assertIn("merges clean", out)                   # simulated all the same
            self.assertFalse(os.path.exists(fetch_head))
            self.assertEqual(odb(fork), before)
            self.assertEqual(sh("git", "for-each-ref", cwd=fork), refs)

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
            path = os.path.join(fork, CONFIG_FILE)
            with open(path) as fh:
                ours = fh.read()
            commit_upstream(self.tmp, CONFIG_FILE, "gate = []\n", "theirs: forkflow too")
            before_trunk = origin_sha(fork, "develop")

            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, err + out)
            self.assertIn(CONFIG_FILE, out + err)
            self.assertIn("untracked", out + err)
            self.assertNotIn("backup/", local_branches(fork))          # nothing to clean up
            self.assertEqual(sh("git", "ls-remote", "origin", "refs/heads/backup/*",
                                cwd=fork), "")
            self.assertEqual(origin_sha(fork, self.sync_name()), "")
            self.assertEqual(origin_sha(fork, "develop"), before_trunk)
            with open(path) as fh:
                self.assertEqual(fh.read(), ours)
            self.assertNotIn("Move them out", err)          # the config is never moved away

            # the remedy it names, run literally: the config committed on a branch off the
            # trunk and shipped, the merge request merged, landed - then the sync again. It
            # used to say "remove the file", which deleted this fork's config for good
            sh("git", "checkout", "-q", "-b", "cfg", "origin/develop", cwd=fork)
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "-m", "ours: forkflow config", cwd=fork)
            code, out, err2 = run_printed(err, "forkflow ship", fork)
            self.assertEqual(code, 0, err2 + out)
            sh("git", "--git-dir=" + os.path.join(self.tmp, "origin.git"), "update-ref",
               "refs/heads/develop", origin_sha(fork, "cfg"))
            code, out, err2 = run("-C", fork, "land")
            self.assertEqual(code, 0, err2 + out)
            code, out, err2 = run_printed(err, "forkflow sync", fork)
            # both sides track the file now: an add/add conflict the user resolves in the
            # open, with this fork's config committed on the trunk - nothing lost
            self.assertEqual(code, 4, err2 + out)
            self.assertIn(CONFIG_FILE, sh("git", "diff", "--name-only", "--diff-filter=U",
                                          cwd=fork))
            self.assertEqual(sh("git", "show", "HEAD:" + CONFIG_FILE, cwd=fork), ours.strip())

        @needs_tomllib
        def test_the_untracked_merge_fallback_names_a_rerun_that_is_not_a_closed_loop(self):
            """`cmd_sync`'s preflight answers this before the backup; the fallback in
            `merge_upstream` is what is left when the tree changes under the run, and by then
            the sync branch exists. Naming plain `forkflow sync` there is a closed loop -
            that run refuses the existing branch and sends the user to `--continue`, which
            has no merge to resume. Both halves of the loop are walked here, after the
            remedy it names - the file moved out of the tree, which keeps it."""
            fork = make_fork(self.tmp)
            write(fork, "docs/theirs.md", "ours, untracked\n")
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            with mock.patch.object(sys.modules[__name__], "untracked_in_the_way",
                                   lambda ctx, target: []):
                code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, out + err)
            self.assertIn("forkflow sync --force", err)
            self.assertIn(self.sync_name(), local_branches(fork))    # the branch is there now

            self.assertIn("Move them out of the working tree", err)
            kept = os.path.join(self.tmp, "theirs.md")
            os.rename(os.path.join(fork, "docs", "theirs.md"), kept)
            # the two commands the old message pointed at are the loop
            code, out, err2 = run("-C", fork, "sync")
            self.assertEqual(code, 2, out + err2)
            self.assertIn("already exists", err2)
            code, out, err2 = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 2, out + err2)
            self.assertIn("nothing to continue", err2)
            # and the one it names now gets out of it
            next_utc_second()
            code, out, err2 = run_printed(err, "forkflow sync --force", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assertEqual(rev(fork, self.sync_name() + "^2"), rev(fork, "upstream/main"))
            with open(kept) as fh:
                self.assertEqual(fh.read(), "ours, untracked\n")

        @needs_tomllib
        def test_the_untracked_merge_fallback_for_the_config_keeps_it(self):
            """The fallback meeting the untracked config gives the config's own answer -
            ship it, then `--force` - and followed to the letter it loses nothing: the next
            sync meets upstream's copy as a tracked file, in a conflict the user resolves."""
            fork = make_fork(self.tmp)
            ours = "# ours, untracked\n"
            path = write(fork, CONFIG_FILE, ours)
            commit_upstream(self.tmp, CONFIG_FILE, "gate = []\n", "theirs: forkflow too")
            with mock.patch.object(sys.modules[__name__], "untracked_in_the_way",
                                   lambda ctx, target: []):
                code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, out + err)
            with open(path) as fh:
                self.assertEqual(fh.read(), ours)
            self.assertNotIn("Move them out", err)          # the config is never moved away

            sh("git", "checkout", "-q", "-b", "cfg", "origin/develop", cwd=fork)
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "-m", "ours: forkflow config", cwd=fork)
            next_utc_second()
            code, out, err2 = run_printed(err, "forkflow ship", fork)
            self.assertEqual(code, 0, err2 + out)
            sh("git", "--git-dir=" + os.path.join(self.tmp, "origin.git"), "update-ref",
               "refs/heads/develop", origin_sha(fork, "cfg"))
            self.assertEqual(run("-C", fork, "land")[0], 0)
            next_utc_second()
            code, out, err2 = run_printed(err, "forkflow sync --force", fork)
            self.assertEqual(code, 4, err2 + out)
            self.assertIn(CONFIG_FILE, sh("git", "diff", "--name-only", "--diff-filter=U",
                                          cwd=fork))
            self.assertEqual(sh("git", "show", "HEAD:" + CONFIG_FILE, cwd=fork), ours.strip())

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
            # the remedy it names, run literally: moved out of the tree, kept, and the sync
            # goes through
            kept = os.path.join(self.tmp, "README.md")
            os.rename(os.path.join(fork, "docs", "README.md"), kept)
            code, out, err2 = run_printed(err, "forkflow sync", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assertEqual(rev(fork, self.sync_name() + "^2"), rev(fork, "upstream/main"))
            with open(kept) as fh:
                self.assertEqual(fh.read(), "# ours, untracked\n")

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
            self.assertTrue(merge_in_progress(fork))    # still uncommitted
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
                    push: bool = False, only_own: bool = False) -> str:
            """A feature branch off the trunk carrying `commits` commits of its own files.

            `only_own` stages just the file it writes: `commit_fork` stages everything,
            which would commit an untracked `.forkflow.toml` and turn a test about the
            untracked config into one about a tracked one."""
            sh("git", "checkout", "-b", name, "develop", cwd=fork)
            for i in range(commits):
                path, message = "ours/f%d.txt" % i, "ours: step %d" % i
                if not only_own:
                    commit_fork(fork, path, "line %d\n" % i, message, push=push)
                    continue
                write(fork, path, "line %d\n" % i)
                sh("git", "add", path, cwd=fork)
                sh("git", "commit", "-q", "-m", message, cwd=fork)
                if push:
                    sh("git", "push", "origin", "%s:refs/heads/%s" % (name, name), cwd=fork)
            return name

        def backup_branch(self, fork: str) -> str:
            out = sh("git", "for-each-ref", "--format=%(refname:short)",
                     "refs/heads/" + DEFAULT_BACKUP_PREFIX + "*", cwd=fork)
            return out.splitlines()[0] if out else ""

        def message_of(self, fork: str, ref: str = "HEAD") -> str:
            return sh("git", "log", "-1", "--format=%B", ref, cwd=fork).strip()

        @staticmethod
        def pending_of(fork: str, branch: Optional[str] = None) -> dict:
            """A `pending` entry as `land` and `status` read it: `branch`'s, or without one
            the only entry there is - {} when there is none. A test with several names the
            branch (or reads `pending_entries` whole)."""
            entries = pending_entries(ctx_for(fork, need_trunk=False, strict_mirror=False))
            if branch is not None:
                return entries.get(branch, {})
            if len(entries) > 1:
                raise AssertionError("several pending entries, name one: %r" % entries)
            return next(iter(entries.values()), {})

        def human(self, *cmds: Sequence[str]) -> str:
            """A clone somebody else merges from: origin/develop checked out, each of `cmds`
            run there, the result pushed; answers with the trunk's new tip.

            Under their own committer identity: a cherry-pick onto the same parent by the
            same committer in the same second is byte-for-byte the shipped commit again,
            and a "rewrite" that keeps the SHA would prove nothing."""
            clone = os.path.join(self.tmp, "human")
            if not os.path.exists(clone):
                sh("git", "clone", "-q", os.path.join(self.tmp, "origin.git"), clone)
                identity(clone)
            with mock.patch.dict(os.environ, {"GIT_COMMITTER_NAME": "a human",
                                              "GIT_COMMITTER_EMAIL": "human@example.invalid"}):
                sh("git", "fetch", "-q", "origin", cwd=clone)
                sh("git", "checkout", "-q", "-B", "develop", "origin/develop", cwd=clone)
                for cmd in cmds:
                    sh("git", *cmd, cwd=clone)
                sh("git", "push", "-q", "origin", "develop", cwd=clone)
            return sh("git", "rev-parse", "HEAD", cwd=clone)

        def move_trunk(self, sha: str) -> None:
            """A human merged the MR by fast-forward: the bare origin's trunk moves to `sha`."""
            sh("git", "--git-dir=" + os.path.join(self.tmp, "origin.git"),
               "update-ref", "refs/heads/develop", sha)

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

        def assert_landed(self, fork: str, name: str, tip: str) -> None:
            """The trunk is at `tip` on origin, as fetched and locally, and checked out; the
            branch is gone, the record is cleared, and the tree is clean."""
            self.assertEqual(rev(fork, "refs/heads/develop"), tip)
            self.assertEqual(rev(fork, "refs/remotes/origin/develop"), tip)
            self.assertEqual(origin_sha(fork, "develop"), tip)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(rev(fork, "refs/heads/" + name), "")
            self.assertEqual(self.pending_of(fork), {})
            self.assertEqual(sh("git", "status", "--porcelain", cwd=fork), "")

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
            # committed on a branch and shipped - never on the trunk or the mirror
            self.assertIn("commit it on a branch off origin/develop", out)
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

        def report_argv(self) -> list:
            """The argv the report's own `glab api` / `gh api` fake recorded."""
            return lines_of(self.log)

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
            self.assertEqual(self.report_argv(), [
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
            self.assertEqual(self.report_argv(), ["api", "projects/acme%2Fteam%2Fwidget"])

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
            self.assertEqual(self.report_argv(), [
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
            self.assertEqual(self.report_argv(), [
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
            self.assertEqual(self.report_argv(), [
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
        def as_gitlab_host(self):
            """The fork under test pushes to a local origin: pretend it is a GitLab one, both
            for the host and for the project path the report builds out of it. Not
            `as_gitlab`, which names the fork for the merge request commands instead."""
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
            with self.as_gitlab_host():
                code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertIn("default branch: `main` - it must be the trunk `develop`", out)
            self.assertIn("-f default_branch='develop'", out)
            self.assertIn("trunk `develop`: NOT protected", out)

        def test_a_dry_run_still_runs_the_read_only_report(self):
            fork = make_fork(self.tmp)
            self.gitlab_tool()
            with self.as_gitlab_host():
                code, out, err = run("-C", fork, "setup", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("read-only: default_branch=develop  merge_method=ff", out)
            self.assertIn("trunk `develop`: protected, force-push disallowed, "
                          "direct push blocked: ok", out)
            self.assertEqual(self.report_argv(), [
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
            url, out, _ = capture(open_mr, ctx, "feat/x", "a title", "body\n", True)
            self.assertEqual(url, "")
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
                url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            argv = list(ran.call_args[0][0])
            self.assertEqual(argv[argv.index("--repo") + 1],
                             "ssh://gitlab.example.com/acme/team/widget")
            self.assertNotIn("original/widget", " ".join(argv))
            self.assertIn("--repo ssh://gitlab.example.com/acme/team/widget", out)
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
                url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("glab mr create", out)
            self.assertEqual(url, "https://gitlab.example.com/acme/team/widget/-/merge_requests/2")
            silent = subprocess.CompletedProcess([], 0, b"done\n", b"")
            with mock.patch.object(subprocess, "run", return_value=silent):
                url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("glab mr create", out)
            self.assertEqual(url, "")
            failed = subprocess.CompletedProcess([], 1, b"https://gitlab.example.com/x\n",
                                                 b"glab: pipeline required\n")
            with mock.patch.object(subprocess, "run", return_value=failed):
                url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("glab mr create", out)
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
                _url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            argv = list(ran.call_args[0][0])
            self.assertIn("--yes", argv)
            self.assertIs(ran.call_args[1].get("stdin"), subprocess.DEVNULL)
            self.assertIn("--yes", out)           # the printed command is the runnable one
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
                url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            ran.assert_not_called()
            self.assertEqual(url, "")
            self.assertIn("names no project there", out)
            self.assertIn("open the merge request manually: feat/x -> develop", out)

        def test_missing_tool_is_reported_and_never_raises(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            ctx.platform = "gitlab"
            ctx.origin_url = "git@gitlab.example.com:acme/team/widget.git"
            missing = FileNotFoundError(2, "No such file or directory")
            with mock.patch.object(subprocess, "run", side_effect=missing):
                url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("glab mr create --repo ssh://gitlab.example.com/acme/team/widget",
                          out)
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
            url, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("gh pr create --repo https://github.com/acme/widget", out)
            self.assertIn("<description file>", out)
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
            terminal - the failure guarded against is a suite (or a `--mr`) that hangs. The
            test's own stdin is swapped for a pipe first: a runner whose stdin is already
            at end-of-file would otherwise pass whether or not the tool inherits it."""
            ctx = ctx_for(make_fork(self.tmp))
            probe = ("import os, sys; a, b = os.fstat(0), os.stat(os.devnull); "
                     "print((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino))")
            saved = os.dup(0)
            r, w = os.pipe()
            try:
                os.dup2(r, 0)
                p = run_tool(ctx, [sys.executable, "-c", probe])
            finally:
                os.dup2(saved, 0)
                for fd in (saved, r, w):
                    os.close(fd)
            self.assertEqual(p.stdout.strip(), b"True")

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

        def body_of(self, argv: Sequence[str], flag: str) -> str:
            self.value(argv, flag)                       # the file was named on the command line
            with open(self.body_copy) as fh:
                return fh.read()

        GL_FORK = PLATFORM_FORKS["gitlab"]         # see `on_platform`
        GH_FORK = PLATFORM_FORKS["github"]

        def test_sync_mr_runs_glab_with_the_sync_body(self):
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf",
                        'resource "null_resource" "a" {\n  count = 2\n}\n',
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            log = self.record("glab")
            name = sync_branch_name()

            with as_gitlab():
                code, out, err = run("-C", fork, "sync", "--mr")
            self.assertEqual(code, 0, err + out)

            argv = lines_of(log)
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

            with as_github():
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)

            argv = lines_of(log)
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
            with as_gitlab():
                code, out, err = run("-C", fork, "sync", "--title", "sync: hand written")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--title 'sync: hand written'", out)
            self.assertIn("not run (add --mr to run it)", out)

        def test_ship_title_is_overridden(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-b", "feat/x", "develop", cwd=fork)
            commit_fork(fork, "ours/a.txt", "a\n", "ours: a")
            second_clone_commit(self.tmp)
            with as_github():
                code, out, err = run("-C", fork, "ship", "--title", "ship: hand written")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--title 'ship: hand written'", out)

        def test_a_failing_tool_leaves_the_branch_pushed_and_exits_0(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            fake_tool(os.path.join(self.tmp, "bin"), "glab",
                      'echo "glab: not authenticated" >&2\nexit 1\n')
            name = sync_branch_name()

            with as_gitlab():
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

            with as_github(), mock.patch.object(
                    sys.modules[__name__], "mr_command", lambda *a: absent):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            self.assertIn("forkflow-no-such-tool unavailable", out)
            self.assertEqual(origin_sha(fork, "feat/x"), rev(fork, "HEAD"))

    class TestPendingRecord(ShipBase):
        """Every `ship` and `sync` that pushed a branch leaves the `pending` record `land`
        works from - with or without `--mr`, whether or not the merge request could be
        opened, and with the URL the tool printed when it could."""

        def url_tool(self, name: str, url: str) -> None:
            """A glab/gh that prints a chatty line first, then the URL - as both tools do."""
            fake_tool(os.path.join(self.tmp, "bin"), name,
                      'echo "Creating a merge request for $2"\necho %s\n' % shlex.quote(url))

        @staticmethod
        def parents_of(fork: str, sha: str) -> list:
            return sh("git", "rev-list", "--parents", "-1", sha, cwd=fork).split()[1:]

        def test_a_run_that_cannot_take_the_lock_records_nothing_and_says_so(self):
            """The bounded wait ends in a REFUSAL, never in a write beside the holder. Run,
            not read: another run holds the state file's lock throughout a real `ship`, and
            afterwards the file is byte for byte what it was - while the push and the branch
            are exactly as the run left them, so nothing is undone by it. The run says which
            record it could not write and what to run in place of `forkflow land`."""
            fork = make_fork(self.tmp)
            name = self.feature(fork)
            ctx = ctx_for(fork)
            self.assertEqual(write_state(ctx, "sync", {"branch": "sync/x", "backup": "b"}), "")
            path = state_path(ctx, shared=True)
            with open(path) as fh:
                before = fh.read()
            held = take_state_lock(path)
            try:
                with mock.patch.object(sys.modules[__name__], "STATE_LOCK_WAIT", 0.05):
                    code, out, err = run("-C", fork, "ship")
            finally:
                drop_state_lock(held)
            self.assertEqual(code, 0, err + out)        # the work happened: not a failure
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            with open(path) as fh:
                self.assertEqual(fh.read(), before)     # nothing written beside the holder
            self.assertIn("held by another forkflow run", out)
            self.assertIn("could not record that", out)
            self.assertNotIn("after the MR is merged, next:", out)

        def test_a_run_on_a_python_that_cannot_lock_records_nothing_and_says_so(self):
            """The same refusal, for the platform rather than for the holder: a Python with
            neither `fcntl` nor `msvcrt` has no lock to take, and forkflow will not write the
            state file unserialised. Run, not read - a real `ship`, whose push and branch are
            left exactly as it made them while the state file keeps the bytes it had."""
            fork = make_fork(self.tmp)
            name = self.feature(fork)
            ctx = ctx_for(fork)
            self.assertEqual(write_state(ctx, "sync", {"branch": "sync/x", "backup": "b"}), "")
            path = state_path(ctx, shared=True)
            with open(path) as fh:
                before = fh.read()
            module = sys.modules[__name__]
            with mock.patch.object(module, "fcntl", None), \
                 mock.patch.object(module, "msvcrt", None):
                code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)        # the work happened: not a failure
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            with open(path) as fh:
                self.assertEqual(fh.read(), before)     # nothing written unserialised
            self.assertIn("neither `fcntl` nor `msvcrt`", out)
            self.assertIn("could not record that", out)
            self.assertNotIn("after the MR is merged, next:", out)

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
            with on_platform("github"):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["mr"]),
                             ("ship", name, "https://github.com/acme/widget/pull/7"))
            self.assertEqual(entry["commit"], origin_sha(fork, name))

        def test_a_record_that_could_not_be_written_is_said_not_assumed(self):
            """The push and the merge request both succeeded and the record did not, so the
            run used to print "after the MR is merged, next: forkflow land" about a record
            that does not exist - and `land` then answered "nothing pending" about a branch
            that is pushed with its request open. The record is read back; when it is not
            there the run says what stands and how to finish by hand. Nothing is undone,
            nothing commits, and every command printed is one `land` would have run."""
            fork = make_fork(self.tmp)
            name = self.feature(fork)
            url = "https://github.com/acme/widget/pull/7"
            self.url_tool("gh", url)
            state = state_path(ctx_for(fork), shared=True)
            os.mkdir(state)                          # nothing can be written there
            with on_platform("github"):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)     # the work happened: it is not a failure
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            self.assertIn("could not record that", out)
            self.assertIn(url, out)
            self.assertIn(state, out)
            self.assertNotIn("after the MR is merged, next:", out)
            for cmd in ("git fetch origin", "git checkout develop",
                        "git merge --ff-only origin/develop", "git branch -d feat/x"):
                self.assertIn(cmd, out)
            for forbidden in ("git commit", "--force", "push origin develop"):
                self.assertNotIn(forbidden, out.split("WARNING:")[-1])
            os.rmdir(state)
            self.assertEqual(run("-C", fork, "land")[0], EXIT_PRECONDITION)   # as it warned

        def test_the_url_glab_prints_is_recorded_for_a_sync(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            url = "https://gitlab.example.com/acme/team/widget/-/merge_requests/3"
            self.url_tool("glab", url)
            with on_platform("gitlab"):
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
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["mr"]), ("ship", name, ""))
            self.assertEqual(entry["commit"], origin_sha(fork, name))

            sh("git", "checkout", "-b", "feat/y", "develop", cwd=fork)
            commit_fork(fork, "ours/y.txt", "y\n", "ours: y")
            next_utc_second()                              # a second `backup/<time>-pre-ship`
            absent = ["forkflow-no-such-tool", "mr", "create"]
            with on_platform("gitlab"), mock.patch.object(
                    sys.modules[__name__], "mr_command", lambda *a: absent):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork, "feat/y")        # this run's, URL-less
            self.assertEqual((entry["kind"], entry["branch"], entry["mr"]),
                             ("ship", "feat/y", ""))
            self.assertEqual(self.pending_of(fork, name)["commit"], origin_sha(fork, name))

        def test_the_record_is_written_before_the_merge_request_step(self):
            """The branch is on origin the moment the push succeeds; a run that dies in the
            merge request step (Ctrl-C at glab's prompt, a temp file that cannot be written)
            must still have left what `land` needs."""
            fork = make_fork(self.tmp)
            name = self.feature(fork)
            with mock.patch.object(sys.modules[__name__], "open_mr",
                                   side_effect=KeyboardInterrupt):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, EXIT_INTERRUPTED, err + out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"]), ("ship", name))
            self.assertEqual(entry["commit"], origin_sha(fork, name))
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            with mock.patch.object(sys.modules[__name__], "open_mr",
                                   side_effect=KeyboardInterrupt):
                code, out, err = run("-C", fork, "sync", "--mr")
            self.assertEqual(code, EXIT_INTERRUPTED, err + out)
            entry = self.pending_of(fork, sync_branch_name())
            self.assertEqual((entry["kind"], entry["branch"]), ("sync", sync_branch_name()))
            self.assertEqual(entry["commit"], origin_sha(fork, sync_branch_name()))
            self.assertEqual(self.pending_of(fork, name)["commit"], origin_sha(fork, name))

        def test_a_dry_run_writes_no_record(self):
            fork = make_fork(self.tmp)
            self.feature(fork)
            code, out, err = run("-C", fork, "ship", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertFalse(os.path.exists(git_path(fork, STATE_FILE)))
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            # the dry run does not fetch, and against an `upstream/*` the server has moved
            # past it says so instead of concluding anything
            # (`test_a_dry_run_does_not_call_a_stale_fork_already_in_sync`)
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            code, out, err = run("-C", fork, "sync", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertFalse(os.path.exists(git_path(fork, STATE_FILE)))

    @needs_tomllib
    class MergeBase(ShipBase):
        """A `merge = "self"` fork on a named platform, with `merging_tool` as the platform:
        what the `--merge` tests and the `land`-after-`--merge` tests share. The fixture's
        origin is a local path, so the platform and the fork it names are supplied
        (`TestMrCommandsNameTheFork` proves the URL; `on_platform` supplies it)."""

        TOOLS = {"gitlab": "glab", "github": "gh"}

        def self_fork(self) -> str:
            return make_fork(self.tmp, config='merge = "self"\n')

        def platform(self, name: str) -> str:
            """A merging glab/gh on PATH; answers with the platform it stands for."""
            merging_tool(self.tmp, self.TOOLS[name])
            return name

        def tool_argv_for(self, platform: str, sub: str) -> list:
            """What the merging fake for `platform` recorded for its `create` or `merge`."""
            return tool_argv(self.tmp, self.TOOLS[platform], sub)

        def upstream_change(self) -> None:
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")

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
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            # fast-forwarded to the one squashed commit this run pushed, on the base
            shipped = origin_sha(fork, "develop")
            self.assertNotEqual(shipped, base)
            self.assertEqual(sh("git", "rev-list", "--parents", "-1", shipped,
                                cwd=fork).split()[1:], [base])
            self.assertEqual(self.tool_argv_for("gitlab", "create")[:2], ["mr", "create"])
            argv = self.tool_argv_for("gitlab", "merge")
            self.assertEqual(argv[:3], ["mr", "merge", name])          # by branch, not URL
            self.assertEqual(self.value(argv, "--repo"), PLATFORM_FORKS["gitlab"])
            self.assertEqual(self.value(argv, "--sha"), shipped)       # the pushed head
            self.assertIn("--auto-merge=false", argv)
            self.assertIn("--remove-source-branch", argv)
            self.assertIn("--yes", argv)
            for flag in ("--squash", "--rebase", "--merge"):
                self.assertNotIn(flag, argv)               # the project's method decides
            self.assertEqual(origin_sha(fork, name), "")               # the source is removed
            self.assertIn(MERGING_TOOL_URL["glab"], out)
            self.assertIn("  merge  $ glab mr merge", out)             # the step, not the fake

        def test_gitlab_sync_merges_the_merge_commit_fast_forward(self):
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            base = origin_sha(fork, "develop")
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            synced = origin_sha(fork, "develop")                      # fast-forwarded to ...
            self.assertEqual(sh("git", "rev-list", "--parents", "-1", synced, cwd=fork).split(),
                             [synced, base, rev(fork, "upstream/main")])  # ... the sync merge
            argv = self.tool_argv_for("gitlab", "merge")
            self.assertEqual(argv[:3], ["mr", "merge", name])
            self.assertEqual(self.value(argv, "--sha"), synced)
            self.assertEqual(origin_sha(fork, name), "")               # the source is removed

        def test_github_ship_merges_with_rebase_and_the_head_guard(self):
            """"Rebase and merge" rewrites the commit: the trunk gets a new SHA with the
            same patch on top of the base - which is what the fake does, and what `land`
            has to recognise later."""
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            with on_platform(self.platform("github")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            shipped = origin_sha(fork, name)
            argv = self.tool_argv_for("github", "merge")
            self.assertEqual(argv[:3], ["pr", "merge", name])
            self.assertEqual(self.value(argv, "--repo"), PLATFORM_FORKS["github"])
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
            """"Create a merge commit" makes one even where a fast-forward was possible: the
            trunk gets a new merge commit whose second parent is the sync's own."""
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            base = origin_sha(fork, "develop")
            with on_platform(self.platform("github")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            synced = origin_sha(fork, name)                    # gh deletes no branch
            argv = self.tool_argv_for("github", "merge")
            self.assertEqual(argv[:3], ["pr", "merge", name])
            self.assertEqual(self.value(argv, "--match-head-commit"), synced)
            self.assertIn("--merge", argv)
            self.assertNotIn("--rebase", argv)
            self.assertNotIn("--squash", argv)
            tip = origin_sha(fork, "develop")
            self.assertEqual(sh("git", "rev-list", "--parents", "-1", tip, cwd=fork).split()[1:],
                             [base, synced])

        # -- exit 6: the branch is pushed, the record is landable ------------------------

        def refused_merge(self, fork: str, name: str, kind: str, base: str,
                          platform: str, *argv: str) -> dict:
            """Run with --merge, expect exit 6, and answer with the intact record."""
            with on_platform(platform):
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
            with on_platform(self.platform(platform)):
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
            self.assertEqual(self.value(self.tool_argv_for(platform, "merge"), flag),
                             entry["commit"])
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
            with on_platform(self.platform("gitlab")):
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
            self.platform("gitlab")
            os.environ["FORKFLOW_FAKE_CREATE"] = "fail"
            entry = self.refused_merge(fork, name, "ship", base, "gitlab")
            # the create ran ...
            self.assertEqual(self.tool_argv_for("gitlab", "create")[:2], ["mr", "create"])
            self.assertEqual(entry["mr"], "")
            self.assertEqual(self.tool_argv_for("gitlab", "merge"), [])   # ... the merge never did
            self.assertEqual(origin_sha(fork, name), entry["commit"])   # pushed all the same

        def test_a_merge_request_the_tool_gave_no_url_for_is_exit_6(self):
            """`create` exited 0 but printed no URL: nothing says a merge request exists, so
            nothing is merged - by branch name or otherwise."""
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            self.platform("gitlab")
            os.environ["FORKFLOW_FAKE_CREATE"] = "nourl"
            entry = self.refused_merge(fork, name, "ship", base, "gitlab")
            self.assertEqual(entry["mr"], "")
            self.assertEqual(self.tool_argv_for("gitlab", "merge"), [])
            self.assertEqual(origin_sha(fork, name), entry["commit"])

        def test_a_merge_the_tool_only_queued_is_exit_6_not_merged(self):
            """The tool answers 0 but nothing reaches the trunk - here glab arming auto-merge,
            as it does without `--auto-merge=false`; a merge train or a queue look the same.
            That request is not merged: exit 6, the record intact, nothing moved - never an
            exit 2 that opens with "the merge request was merged"."""
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            real = merge_command

            def armed(*a):
                return [c for c in real(*a) if c != "--auto-merge=false"]

            with on_platform(self.platform("gitlab")), \
                    mock.patch.object(sys.modules[__name__], "merge_command", armed):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 6, err + out)
            self.assertNotIn("was merged;", err)
            self.assertIn("forkflow land", err)
            self.assertEqual(origin_sha(fork, "develop"), base)
            self.assertEqual(rev(fork, "refs/heads/develop"), base)
            self.assertEqual(checked_out(fork), name)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["commit"]),
                             ("ship", name, rev(fork, "refs/heads/" + name)))

        def test_a_trunk_that_moved_after_the_push_is_not_fast_forwarded_over(self):
            """Somebody else's merge request landed between the push and the merge: GitLab's
            fast-forward method refuses ("rebase needed"), so it is exit 6 with the record
            intact - and the other commit stays on the trunk."""
            fork = self.self_fork()
            name = self.feature(fork)
            commit_after_receive(self.tmp, onto="develop")
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 6, err + out)
            self.assertIn("NOT MERGED (exit 1)", out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"]), ("ship", name))
            origin_git = "--git-dir=" + os.path.join(self.tmp, "origin.git")
            self.assertEqual(sh("git", origin_git, "log", "-1", "--format=%s %P", "develop"),
                             "teammate " + entry["base"])               # not fast-forwarded over
            self.assertEqual(origin_sha(fork, name), entry["commit"])   # nothing removed

        def test_a_missing_merge_tool_is_exit_6(self):
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            self.platform("github")
            absent = ["forkflow-no-such-tool", "pr", "merge", name]
            with mock.patch.object(sys.modules[__name__], "merge_command", lambda *a: absent):
                with on_platform("github"):
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
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--mr")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.tool_argv_for("gitlab", "create")[:2], ["mr", "create"])
            self.assertEqual(self.tool_argv_for("gitlab", "merge"), [])
            self.assertEqual(origin_sha(fork, "develop"), base)
            self.assertEqual(self.pending_of(fork)["mr"], MERGING_TOOL_URL["glab"])
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), name)

        def test_a_dry_run_runs_nothing_and_writes_nothing(self):
            fork = self.self_fork()
            self.upstream_change()
            sh("git", "fetch", "-q", "upstream", cwd=fork)   # a dry run of its own does not
            before = (origin_sha(fork, "develop"), origin_sha(fork, "main"))
            self.platform("gitlab")
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would: merge", out)
            self.assertIn("would: land", out)
            self.assertIn("unless --merge lands it", out)         # where you end up
            name = self.feature(fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would: merge", out)
            self.assertIn("glab mr merge " + name, out)
            self.assertIn("would: land", out)
            self.assertEqual(self.tool_argv_for("gitlab", "create"), [])       # neither tool ran
            self.assertEqual(self.tool_argv_for("gitlab", "merge"), [])
            self.assertFalse(os.path.exists(git_path(fork, STATE_FILE)))    # no state at all
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

        def parent_of(self, sha: str) -> str:
            return sh("git", "rev-parse", sha + "^", cwd=os.path.join(self.tmp, "human"))

        def with_mr(self, fork: str, entry: dict) -> dict:
            """The record as a run with `--mr` leaves it: the merge request's URL known."""
            entry = dict(entry, mr=self.MR)
            put_pending(fork, entry)
            return entry

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

        def test_a_rebase_stopped_on_a_clean_tree_is_exit_2(self):
            """A rebase stopped by a failing `--exec` leaves the tree clean, so only the
            rebase check can refuse it - checking the trunk out would walk out of it."""
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "checkout", "-q", "-b", "other", "develop", cwd=fork)
            commit_fork(fork, "ours/other.txt", "other\n", "other: one")
            sh("git", "rebase", "--no-ff", "--exec", "false", "HEAD~1", cwd=fork, check=False)
            ctx = ctx_for(fork, strict_mirror=False)
            self.assertTrue(rebase_in_progress(ctx))
            self.assertTrue(clean_tree(ctx))
            head = rev(fork, "HEAD")
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["base"])
            self.assertEqual(rev(fork, "HEAD"), head)
            self.assertTrue(rebase_in_progress(ctx))
            self.assertEqual(self.pending_of(fork), entry)

        def test_a_failed_fetch_is_exit_2_even_when_the_refs_show_the_landing(self):
            """The refs on disk already say "landed", but `land` fetches first and a fetch
            that fails stops it: nothing moves on a stale view."""
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "fetch", "-q", "origin", cwd=fork)
            sh("git", "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"),
               cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("fetch failed", err)
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
            """Refused before the fetch, naming the worktree - and the route it names works:
            `land` there sees the same record, lands, and forgets it for both."""
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            wt = os.path.join(self.tmp, "wt")
            sh("git", "worktree", "add", "-q", wt, "develop", cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("checked out in %s:" % wt, err)           # the path is named
            self.assert_untouched(fork, name, entry, on=name)
            # refused before the fetch: the remote-tracking trunk has not seen the merge
            self.assertEqual(rev(fork, "refs/remotes/origin/develop"), entry["base"])
            self.assertEqual(self.pending_of(wt), entry)            # the same record there
            code, out, err = self.land(wt)
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(wt, "refs/heads/develop"), entry["commit"])
            self.assertEqual(checked_out(wt), "develop")
            self.assertEqual(self.pending_of(wt), {})
            self.assertEqual(self.pending_of(fork), {})
            # the branch is checked out in the main worktree, so git will not delete it; the
            # landing is done all the same
            self.assertEqual(rev(fork, "refs/heads/" + name), entry["commit"])
            self.assertIn("NOT deleted", out)

        def test_a_ship_from_a_linked_worktree_lands_in_the_main_one(self):
            """The usual worktree layout: the main worktree stays on the trunk, the feature
            is shipped from a linked one. `land` there names the main worktree, and `land`
            in the main worktree finds the record the linked one wrote."""
            fork = make_fork(self.tmp)
            linked = os.path.join(self.tmp, "linked")
            sh("git", "worktree", "add", "-q", "-b", "feat/x", linked, "develop", cwd=fork)
            commit_fork(linked, "ours/f0.txt", "line 0\n", "ours: step 0")
            code, out, err = run("-C", linked, "ship")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"]), ("ship", "feat/x"))
            self.assertEqual(self.pending_of(linked), entry)
            self.move_trunk(entry["commit"])
            code, out, err = self.land(linked)
            self.assertEqual(code, 2, err + out)
            self.assertIn("checked out in %s:" % fork, err)
            self.assertEqual(rev(linked, "refs/remotes/origin/develop"), entry["base"])
            self.assertEqual(checked_out(linked), "feat/x")
            self.assertEqual(self.pending_of(fork), entry)
            code, out, err = self.land(fork)                        # the route it named
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["commit"])
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(fork), {})
            self.assertEqual(self.pending_of(linked), {})
            self.assertEqual(rev(fork, "refs/heads/feat/x"), entry["commit"])  # checked out there
            self.assertEqual(checked_out(linked), "feat/x")

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

        def test_a_sync_squashed_in_the_ui_is_not_landed_and_says_rule_5(self):
            """A human squashed the sync: upstream's commits are on the trunk under new
            SHAs, so the merge commit never becomes an ancestor. Patch equivalence is a
            ship's test only - for a sync it would call this landed and `-D` the branch -
            so it is exit 2 with the rule-5 note, and `status` agrees it has not landed."""
            fork, name, entry = self.synced()
            tip = self.human(("merge", "--squash", "origin/" + name),
                             ("commit", "-q", "-m", "a sync, squashed by hand"))
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("rule 5", err)
            self.assert_untouched(fork, name, entry, on=name)
            self.assertEqual(rev(fork, "refs/remotes/origin/develop"), tip)   # it did fetch
            code, out, err = run("-C", fork, "status", "--offline")
            self.assertEqual(code, 0, err + out)
            self.assertIn("- not on origin/develop yet", out)
            self.assertEqual(self.pending_of(fork), entry)

        def test_the_ship_of_another_clone_cannot_be_verified_here(self):
            """A record whose commit this clone does not have - the state file copied from
            elsewhere, say - is refused rather than guessed at; `--force` clears it without
            touching a branch."""
            fork, name, shipped = self.shipped()
            elsewhere = commit_upstream(self.tmp, "docs/x.md", "x\n")   # never fetched here
            entry = dict(shipped, commit=elsewhere, branch="feat/other")
            put_pending(fork, entry)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertIn("cannot verify", err)
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["base"])
            self.assertEqual(checked_out(fork), name)                # nothing moved ...
            self.assertEqual(self.pending_of(fork), entry)           # ... and nothing forgotten
            # `--force` judges nothing, so a commit it cannot see is no obstacle: the trunk
            # follows origin, no branch is touched, and the record goes
            other = second_clone_commit(self.tmp)
            code, out, err = self.land(fork, "--force")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), other)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(rev(fork, "refs/heads/" + name), shipped["commit"])  # untouched
            self.assertIn("landing NOT verified", out)
            self.assertEqual(self.pending_of(fork), {})

        def test_a_ship_whose_commit_was_pruned_is_cleared_by_force(self):
            """The merge request was abandoned, the branch deleted and its commit pruned:
            `status` can only say "cannot verify", plain `land` refuses to guess - and
            `--force`, the documented way to clear a closed request, clears it. Two commits,
            so the squash is a commit of its own and not the tip the pre-ship backup keeps."""
            fork, name, entry = self.shipped(commits=2)
            sh("git", "checkout", "-q", "develop", cwd=fork)
            sh("git", "branch", "-D", name, cwd=fork)
            sh("git", "push", "-q", "origin", "--delete", name, cwd=fork)
            sh("git", "reflog", "expire", "--expire=now", "--all", cwd=fork)
            sh("git", "gc", "-q", "--prune=now", cwd=fork)
            self.assertEqual(sh("git", "cat-file", "-t", entry["commit"], cwd=fork,
                                check=False), "")                    # gone from the clone
            code, out, err = run("-C", fork, "status", "--offline")
            self.assertIn("- cannot verify here", out)
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assertEqual(self.pending_of(fork), entry)
            code, out, err = self.land(fork, "--force")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.pending_of(fork), {})
            self.assertEqual(checked_out(fork), "develop")

        def test_an_empty_ship_is_not_matched_to_an_empty_trunk_commit(self):
            """The squash allows an empty commit, and an empty patch matches every empty
            commit: only ancestry can land an empty ship."""
            fork = make_fork(self.tmp)
            name = self.feature(fork, commits=0)
            commit_fork(fork, "ours/tmp.txt", "tmp\n", "ours: add")
            sh("git", "rm", "-q", "ours/tmp.txt", cwd=fork)
            sh("git", "commit", "-q", "-m", "ours: remove", cwd=fork)
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual(sh("git", "diff", entry["base"], entry["commit"], cwd=fork), "")
            self.human(("commit", "-q", "--allow-empty", "-m", "somebody's empty commit"))
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assert_untouched(fork, name, entry, on=name)

        @needs_merge_tree
        def test_a_rewritten_patch_that_was_reverted_is_not_landed(self):
            """The patch reached the trunk and was reverted since: `git cherry` still finds
            it, but the trunk no longer has the change - not a landing, and not a reason to
            delete the branch."""
            fork, name, entry = self.shipped()
            self.human(("cherry-pick", "origin/" + name), ("revert", "--no-edit", "HEAD"))
            code, out, err = self.land(fork)
            self.assertEqual(code, 2, err + out)
            self.assert_untouched(fork, name, entry, on=name)

        @needs_merge_tree
        def test_a_rewritten_patch_edited_again_since_is_still_landed(self):
            """Later work on the very lines the ship changed is not a revert: the patch
            landed, and the trunk has moved on from it."""
            fork, name, entry = self.shipped()
            self.human(("cherry-pick", "origin/" + name))
            write(os.path.join(self.tmp, "human"), "ours/f0.txt", "line 0, moved on\n")
            tip = self.human(("commit", "-q", "-am", "somebody moved it on"))
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("(rewritten)", out)
            self.assert_landed(fork, name, tip)

        def test_a_branch_already_deleted_by_hand_still_lands(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "checkout", "-q", "develop", cwd=fork)
            sh("git", "branch", "-D", name, cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("`%s` is already gone" % name, out)       # the only difference
            self.assert_landed(fork, name, entry["commit"])

        def test_the_mirror_is_never_deleted_whatever_the_record_says(self):
            """A hand-edited record naming the mirror, whose tip is on the trunk: it lands
            (the trunk catches up) and `main` stays."""
            fork = make_fork(self.tmp)
            mirror = rev(fork, "refs/heads/main")
            put_pending(fork, {"kind": "ship", "branch": "main", "commit": mirror,
                               "base": rev(fork, "refs/remotes/origin/develop"), "mr": ""})
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/main"), mirror)
            self.assertEqual(self.pending_of(fork), {})

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

        # -- the branch moved on after the ship: only the pushed commit landed ---------------

        def assert_kept_at(self, fork: str, name: str, tip: str, landed_tip: str) -> None:
            """The landing is done - trunk caught up, HEAD on it, record cleared - and the
            branch is still there at `tip`, the commit that did not land."""
            self.assertEqual(rev(fork, "refs/heads/develop"), landed_tip)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(fork), {})
            self.assertEqual(rev(fork, "refs/heads/" + name), tip)       # kept, not rewound
            rc, _, _ = git_rc("merge-base", "--is-ancestor", tip, "refs/heads/develop",
                              cwd=fork)
            self.assertEqual(rc, 1)                     # the later commit is on no trunk ...
            self.assertIn("refs/heads/" + name,        # ... and still reachable from a branch
                          sh("git", "for-each-ref", "--contains", tip,
                             "--format=%(refname)", "refs/heads/", cwd=fork).splitlines())

        def test_a_commit_after_a_rewritten_ship_keeps_the_branch(self):
            """"Rebase and merge" of the pushed commit, and one more commit made on the
            branch after the ship: the patch that landed is the shipped one, so `-D` of the
            branch would destroy the later commit. It is kept, and the landing still runs."""
            fork, name, entry = self.shipped()
            later = commit_fork(fork, "ours/later.txt", "later\n", "ours: after the ship")
            tip = self.human(("cherry-pick", entry["commit"]))
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("(rewritten)", out)
            self.assertNotIn("git branch -D", out)
            self.assert_kept_at(fork, name, later, tip)

        def test_a_commit_after_the_ship_pushed_by_hand_still_keeps_the_branch(self):
            """The later commit pushed by hand: `push` set the branch's upstream, so a
            plain `git branch -d` would take the branch now that `origin/<branch>` contains
            its tip - the recorded commit is the guard, not `-d`."""
            fork, name, entry = self.shipped()
            later = commit_fork(fork, "ours/later.txt", "later\n", "ours: after the ship",
                                push=True)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(rev(fork, "refs/remotes/origin/" + name), later)
            tip = self.human(("cherry-pick", entry["commit"]))
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assert_kept_at(fork, name, later, tip)

        def test_a_commit_after_a_fast_forwarded_ship_keeps_the_branch(self):
            fork, name, entry = self.shipped()
            later = commit_fork(fork, "ours/later.txt", "later\n", "ours: after the ship")
            self.move_trunk(entry["commit"])
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("(ancestor)", out)
            self.assert_kept_at(fork, name, later, entry["commit"])

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
            # `--no-track` as state, not as the printed command: the recreated trunk has no
            # upstream, so a stray `git pull` on it later is refused rather than merged
            self.assertEqual(sh("git", "config", "--get", "branch.develop.remote", cwd=fork,
                                check=False), "")
            self.assertEqual(sh("git", "config", "--get", "branch.develop.merge", cwd=fork,
                                check=False), "")

        def test_land_from_an_unrelated_branch_ends_on_the_trunk(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "checkout", "-q", "-b", "other", "develop", cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assert_landed(fork, name, entry["commit"])
            self.assertEqual(rev(fork, "refs/heads/other"), entry["base"])   # left alone

        def test_land_from_the_trunk_itself(self):
            """The trunk checked out here is not "another worktree": `land` runs on it."""
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "checkout", "-q", "develop", cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assert_landed(fork, name, entry["commit"])

        def test_a_trunk_already_caught_up_is_still_checked_out(self):
            """The local trunk was pulled by hand and HEAD went back to the branch: there is
            nothing to fast-forward, but `land` still leaves you on the trunk - and only from
            there can the landed branch be deleted."""
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "fetch", "-q", "origin", cwd=fork)
            sh("git", "checkout", "-q", "develop", cwd=fork)
            sh("git", "merge", "-q", "--ff-only", "origin/develop", cwd=fork)
            sh("git", "checkout", "-q", name, cwd=fork)
            code, out, err = self.land(fork)
            self.assertEqual(code, 0, err + out)
            self.assert_landed(fork, name, entry["commit"])

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
            with on_platform("github"):
                code, out, err = self.land(fork, "--force")
            self.assertEqual(code, 0, err + out)
            self.assertIn("not verified (--force)", out)
            self.assertIn("`%s` is kept" % name, out)
            self.assertIn("caught up, landing NOT verified: develop", out)   # not "landed:"
            self.assertNotIn("may still exist", out)      # the branch is kept: no delete hint
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
            """The landing is already in this clone's refs - `ship --merge` and a merge by
            hand both leave it fetched - so the dry run judges it and previews every step."""
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            sh("git", "fetch", "-q", "origin", cwd=fork)      # a dry run of its own does not
            code, out, err = self.land(fork, "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertIn("would: checkout  $ git checkout develop", out)
            self.assertIn("would: trunk  $ git merge --ff-only origin/develop", out)
            self.assertIn("would: branch  $ git branch -d " + name, out)
            self.assertIn("would: landed", out)
            self.assertEqual(rev(fork, "refs/remotes/origin/develop"), entry["commit"])
            self.assert_untouched(fork, name, entry, on=name)

        def test_a_dry_run_fetches_nothing_and_says_what_it_could_not_tell(self):
            """The no-write contract, against the one writing step every `--dry-run` path
            took: `git fetch` rewrites `FETCH_HEAD`, moves the remote-tracking refs and
            brings objects in. `git ls-remote` asks the same server and writes none of them,
            so the run says what origin has and that the verdict below it is from the refs
            on disk - rather than fetching, or saying "merge it" about a request that is
            merged. The refusal names the run that can decide."""
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])
            fetch_head = git_path(fork, "FETCH_HEAD")
            if os.path.exists(fetch_head):
                os.unlink(fetch_head)
            before = odb(fork)
            code, out, err = self.land(fork, "--dry-run")
            self.assertEqual(code, EXIT_PRECONDITION, err + out)
            self.assertIn("did not fetch", err)
            self.assertIn("without `--dry-run`", err)
            self.assertIn("git ls-remote origin refs/heads/develop", out)
            self.assertIn("NOT fetched (dry run)", out)
            self.assertFalse(os.path.exists(fetch_head))             # no FETCH_HEAD
            self.assertEqual(odb(fork), before)                      # no objects
            self.assertNotEqual(rev(fork, "refs/remotes/origin/develop"), entry["commit"])
            self.assert_untouched(fork, name, entry, on=name)
            code, out, err = self.land(fork)                         # the named run decides
            self.assertEqual(code, 0, err + out)
            self.assert_landed(fork, name, entry["commit"])

    class TestStatusPending(ShipBase):
        """`status`'s `pending` line: the record a ship or a sync left and whether it has
        landed, judged with git only from the refs as they are. Assertions are on the
        decision the line carries (landed / not landed / cannot verify), read back through
        `verdict()`, never on the sentence around it."""

        @staticmethod
        def pending_line(out: str) -> Optional[str]:
            lines = [ln for ln in out.splitlines() if ln.startswith("  pending")]
            return lines[0] if lines else None

        def verdict(self, out: str) -> Optional[str]:
            """The decision on the pending line: "landed", "not landed", "cannot verify",
            or None when there is no such line."""
            line = self.pending_line(out)
            if line is None:
                return None
            if "landed: run forkflow land" in line:
                return "landed"
            if "cannot verify" in line:
                return "cannot verify"
            self.assertIn("not on origin/develop yet", line)
            return "not landed"

        def status(self, fork: str, *flags: str) -> str:
            code, out, err = run("-C", fork, "status", *flags)
            self.assertEqual(code, 0, err + out)
            return out

        def test_no_line_when_nothing_is_pending(self):
            fork = make_fork(self.tmp)
            self.assertIsNone(self.verdict(self.status(fork)))

        def test_not_landed_until_the_trunk_moves_and_fetch_flips_the_verdict(self):
            fork, name, entry = self.shipped()
            out = self.status(fork)
            self.assertEqual(self.verdict(out), "not landed")
            self.assertIn(f"pending  ship {name} -> MR -", self.pending_line(out))
            self.move_trunk(entry["commit"])
            # without --fetch the verdict is from the refs on disk, which have not moved
            self.assertEqual(self.verdict(self.status(fork)), "not landed")
            self.assertEqual(self.verdict(self.status(fork, "--fetch")), "landed")
            # the fetch moved the refs, so a plain status now agrees
            self.assertEqual(self.verdict(self.status(fork)), "landed")
            self.assertEqual(self.pending_of(fork), entry)        # status writes nothing

        def test_a_sync_is_judged_by_ancestry_of_its_merge_commit(self):
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 0, err + out)
            entry = self.pending_of(fork)
            self.assertEqual(entry["kind"], "sync")
            self.assertEqual(self.verdict(self.status(fork)), "not landed")
            self.move_trunk(entry["commit"])
            sh("git", "fetch", "origin", cwd=fork)
            self.assertEqual(self.verdict(self.status(fork)), "landed")

        def test_the_url_is_shown_when_the_record_has_one(self):
            fork, name, entry = self.shipped()
            url = "https://example.invalid/-/merge_requests/7"
            put_pending(fork, dict(entry, mr=url))
            self.assertIn(f"-> MR {url} -", self.pending_line(self.status(fork)))

        def test_offline_gives_the_same_verdict(self):
            """The check is git-only: `--offline` skips the server round-trip and nothing else."""
            fork, name, entry = self.shipped()
            self.assertEqual(self.pending_line(self.status(fork, "--offline")),
                             self.pending_line(self.status(fork)))
            self.assertEqual(self.verdict(self.status(fork, "--offline")), "not landed")
            self.move_trunk(entry["commit"])
            sh("git", "fetch", "origin", cwd=fork)
            self.assertEqual(self.verdict(self.status(fork, "--offline")), "landed")
            self.assertEqual(self.pending_line(self.status(fork, "--offline")),
                             self.pending_line(self.status(fork)))

        def test_cannot_verify_without_the_trunk_on_origin(self):
            """A fresh fork has no `origin/develop`; `status` resolves without one and must
            not raise for this line."""
            fork = make_fresh_fork(self.tmp)
            head = rev(fork, "HEAD")
            put_pending(fork, {"kind": "ship", "branch": "feat/x", "commit": head,
                               "base": head, "mr": ""})
            self.assertEqual(self.verdict(self.status(fork)), "cannot verify")

        def test_cannot_verify_when_the_commit_is_not_in_this_clone(self):
            """The record is the other clone's: `landed()` would raise, `status` does not."""
            fork = make_fork(self.tmp)
            put_pending(fork, {"kind": "ship", "branch": "feat/x", "commit": "1" * 40,
                               "base": rev(fork, "refs/remotes/origin/develop"), "mr": ""})
            self.assertEqual(self.verdict(self.status(fork)), "cannot verify")

        def test_a_malformed_entry_is_ignored(self):
            fork = make_fork(self.tmp)
            write_state(ctx_for(fork, strict_mirror=False), "pending",
                        {"kind": "ship", "branch": "feat/x"})
            self.assertIsNone(self.verdict(self.status(fork)))

    class TestMergeLands(MergeBase):
        """`--merge` ends by running `land` in the same process: the trunk is
        fast-forwarded locally, you are left on it, the branch is gone, the record cleared -
        on both platforms and for both kinds. A merge that succeeded and a catch-up that
        then could not run is exit 2 opening with "the merge request was merged"."""

        def test_gitlab_ship_lands_by_fast_forward(self):
            fork = self.self_fork()
            name = self.feature(fork, commits=2)
            old = rev(fork, "refs/heads/develop")
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            # what was pushed
            shipped = self.value(self.tool_argv_for("gitlab", "merge"), "--sha")
            self.assertEqual(sh("git", "rev-list", "--parents", "-1", shipped,
                                cwd=fork).split()[1:], [old])
            self.assertIn("(ancestor)", out)
            self.assertIn("landed: develop %s..%s" % (short(old), short(shipped)), out)
            self.assertEqual(origin_sha(fork, name), "")             # glab removed it ...
            self.assertNotIn("may still exist", out)                 # ... so no hint ...
            self.assertEqual(rev(fork, "refs/remotes/origin/" + name), "")   # ... nor a stale ref
            self.assert_landed(fork, name, shipped)
            # which is what lets the name be shipped again: a stale `origin/<branch>` would
            # be offered as the lease, and `--force-with-lease` refuses it ("stale info")
            next_utc_second()
            sh("git", "checkout", "-q", "-b", name, "develop", cwd=fork)
            commit_fork(fork, "ours/again.txt", "again\n", "ours: again")
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(fork), {})

        def test_gitlab_sync_lands_its_merge_commit(self):
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertIn("unless --merge lands it", out)
            synced = self.value(self.tool_argv_for("gitlab", "merge"), "--sha")
            self.assertEqual(len(sh("git", "rev-list", "--parents", "-1", synced,
                                    cwd=fork).split()), 3)           # the sync's merge commit
            self.assert_landed(fork, name, synced)
            self.assertEqual(rev(fork, "refs/heads/main"), rev(fork, "upstream/main"))

        def test_github_ship_lands_its_rebased_copy(self):
            fork = self.self_fork()
            name = self.feature(fork)
            base = origin_sha(fork, "develop")
            with on_platform(self.platform("github")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            tip = origin_sha(fork, "develop")
            self.assertNotIn(tip, (base, origin_sha(fork, name)))     # rewritten by the platform
            self.assertIn("(rewritten)", out)
            self.assertIn("git branch -D " + name, out)
            self.assertIn("git push origin --delete " + name, out)    # gh leaves the remote branch
            self.assert_landed(fork, name, tip)

        def test_github_sync_lands_its_merge_commit(self):
            """GitHub's merge commit sits on top of the sync's: the sync's commit is on the
            trunk under its own SHA, off the new commit's second parent - which is rule 5
            for a sync, so no WARNING (that is a ship's)."""
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            with on_platform(self.platform("github")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            synced = origin_sha(fork, name)
            tip = origin_sha(fork, "develop")
            self.assertNotEqual(tip, synced)                         # a new merge commit
            self.assertIn("yes - as %s (ancestor)" % short(synced), out)
            self.assertNotIn("WARNING", out)
            self.assert_landed(fork, name, tip)
            # gh deletes no branch: the sync branch is still on origin, and the way to remove
            # it is printed - for a sync as for a ship
            self.assertNotEqual(origin_sha(fork, name), "")
            self.assertIn("git push origin --delete " + name, out)

        def test_a_conflicted_sync_resumed_with_merge_lands(self):
            """The conflicted run says how to resume it - with `--merge`, which it was given
            - and the resumed run merges and lands like an unconflicted one."""
            fork = self.self_fork()
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                            "theirs: shared")
            name = sync_branch_name()
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 4, err + out)
            self.assertIn("forkflow sync --continue --merge", err)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--continue", "--merge")
            self.assertEqual(code, 0, err + out)
            synced = self.value(self.tool_argv_for("gitlab", "merge"), "--sha")
            self.assertEqual(len(sh("git", "rev-list", "--parents", "-1", synced,
                                    cwd=fork).split()), 3)
            self.assert_landed(fork, name, synced)

        def test_a_stopped_ship_resumed_with_merge_lands(self):
            fork = self.self_fork()
            name = self.feature(fork, commits=0)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared")
            second_clone_commit(self.tmp, path="shared.tf",
                                content=self.BASE_TF.replace("count = 1", "count = 9"))
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 4, err + out)
            self.assertIn("forkflow ship --continue --merge", err)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            sh("git", "rebase", "--continue", cwd=fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--continue", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assert_landed(fork, name,
                               self.value(self.tool_argv_for("gitlab", "merge"), "--sha"))
            with open(os.path.join(fork, "shared.tf")) as fh:
                self.assertIn("count = 4", fh.read())                # the resolution landed

        def test_a_refused_merge_leaves_a_record_a_by_hand_merge_lands_from(self):
            """Exit 6 says "the branch is pushed; merge by hand, then `forkflow land`" - so
            that has to work: the platform refuses, a human merges the open request in the
            UI, and a later `land` finishes from the record the failed run left. The
            landable state is proven by landing, not by the record's shape alone."""
            fork = self.self_fork()
            name = self.feature(fork, commits=2)
            old = rev(fork, "refs/heads/develop")
            os.environ["FORKFLOW_FAKE_FAIL"] = "1"
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 6, err + out)
            del os.environ["FORKFLOW_FAKE_FAIL"]
            shipped = origin_sha(fork, name)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"], entry["commit"], entry["mr"]),
                             ("ship", name, shipped, MERGING_TOOL_URL["glab"]))
            self.assertEqual(rev(fork, "refs/heads/develop"), old)          # nothing landed
            self.assertEqual(origin_sha(fork, "develop"), entry["base"])
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), name)
            self.move_trunk(shipped)            # a human merges the request by fast-forward
            code, out, err = run("-C", fork, "land")
            self.assertEqual(code, 0, err + out)
            self.assertIn("(ancestor)", out)
            self.assert_landed(fork, name, shipped)

        def test_a_merged_request_whose_catch_up_cannot_run_says_so_first(self):
            """The local trunk carries a commit of its own: the platform merged the request,
            `land` refuses to move the trunk, and the exit says both - merged, not landed."""
            fork = self.self_fork()
            name = self.feature(fork)
            sh("git", "checkout", "-q", "develop", cwd=fork)
            own = commit_fork(fork, "ours/local.txt", "local\n", "local: by hand")
            sh("git", "checkout", "-q", name, cwd=fork)
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertTrue(err.startswith("forkflow: the merge request was merged; "
                                           "the local catch-up did not run: "), err)
            self.assertIn("commits origin lacks", err)
            shipped = self.value(self.tool_argv_for("gitlab", "merge"), "--sha")
            self.assertEqual(origin_sha(fork, "develop"), shipped)   # merged ...
            self.assertEqual(rev(fork, "refs/heads/develop"), own)   # ... not landed
            self.assertEqual(sh("git", "symbolic-ref", "--short", "HEAD", cwd=fork), name)
            self.assertEqual(rev(fork, "refs/heads/" + name), shipped)
            entry = self.pending_of(fork)                            # `land` can run later
            self.assertEqual((entry["kind"], entry["branch"], entry["commit"]),
                             ("ship", name, shipped))
            self.assertEqual(entry["mr"], MERGING_TOOL_URL["glab"])

        def test_a_merge_from_a_linked_worktree_is_landed_from_the_main_one(self):
            """`ship --merge` in a linked worktree while the main one has the trunk checked
            out: merged, the catch-up names the main worktree, and `land` there finishes -
            the record the linked worktree wrote is the one it reads."""
            fork = self.self_fork()
            linked = os.path.join(self.tmp, "linked")
            sh("git", "worktree", "add", "-q", "-b", "feat/x", linked, "develop", cwd=fork)
            commit_fork(linked, "ours/f0.txt", "line 0\n", "ours: step 0")
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", linked, "ship", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertTrue(err.startswith("forkflow: the merge request was merged; "), err)
            self.assertIn("checked out in %s:" % fork, err)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"]), ("ship", "feat/x"))
            self.assertEqual(origin_sha(fork, "develop"), entry["commit"])      # merged
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "land")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["commit"])
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(linked), {})

        def queued(self):
            """glab arming auto-merge instead of merging - a merge train or a queue look the
            same: the tool answers 0 and nothing reaches the trunk."""
            real = merge_command

            def armed(*a):
                return [c for c in real(*a) if c != "--auto-merge=false"]

            return mock.patch.object(sys.modules[__name__], "merge_command", armed)

        def assert_queued_then_landed_where_it_says(self, fork: str, linked: str, err: str,
                                                    kind: str, name: str, base: str) -> None:
            """Exit 6's state - nothing moved, the record intact, both worktrees where they
            were - then the queue completes and the command it printed, run in the worktree
            it named, lands the record."""
            self.assertFalse(err.startswith("forkflow: the merge request was merged"), err)
            self.assertEqual(origin_sha(fork, "develop"), base)
            self.assertEqual(rev(fork, "refs/heads/develop"), base)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(checked_out(linked), name)
            entry = self.pending_of(fork)
            self.assertEqual((entry["kind"], entry["branch"]), (kind, name))
            self.assertEqual(printed_cmd(err, "forkflow land"), "forkflow land " + name)
            self.assertIn(" in %s," % fork, err)
            self.move_trunk(entry["commit"])
            with on_platform("gitlab"):
                code, out, err2 = run_printed(err, "forkflow land", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), entry["commit"])
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(fork), {})

        def test_a_queued_ship_merge_from_a_linked_worktree_is_exit_6(self):
            """The usual layout - the trunk in the main worktree, the work in a linked one -
            and a merge the tool only queued. The landing was judged after "the trunk is
            checked out elsewhere", so it read "the merge request was merged" (exit 2), and
            `land` in the main worktree then said "merge it" of a request already queued.
            The verdict comes first now: exit 6, with the command for once it is through."""
            fork = self.self_fork()
            linked = os.path.join(self.tmp, "linked")
            sh("git", "worktree", "add", "-q", "-b", "feat/x", linked, "develop", cwd=fork)
            commit_fork(linked, "ours/f0.txt", "line 0\n", "ours: step 0")
            base = origin_sha(fork, "develop")
            with on_platform(self.platform("gitlab")), self.queued():
                code, out, err = run("-C", linked, "ship", "--merge")
            self.assertEqual(code, 6, err + out)
            self.assert_queued_then_landed_where_it_says(fork, linked, err, "ship", "feat/x",
                                                         base)

        def test_a_queued_sync_merge_from_a_linked_worktree_is_exit_6(self):
            fork = self.self_fork()
            linked = os.path.join(self.tmp, "linked")
            sh("git", "worktree", "add", "-q", "-b", "scratch", linked, "develop", cwd=fork)
            self.upstream_change()
            base = origin_sha(fork, "develop")
            with on_platform(self.platform("gitlab")), self.queued():
                code, out, err = run("-C", linked, "sync", "--merge")
            self.assertEqual(code, 6, err + out)
            self.assert_queued_then_landed_where_it_says(fork, linked, err, "sync",
                                                         sync_branch_name(), base)

    # ------------------------------------------------------------------- #
    # argument parsing and main
    # ------------------------------------------------------------------- #

    class TestPendingPerBranch(MergeBase):
        """Worktrees of one clone shipping different branches before either lands.

        `pending` is shared by every worktree, one entry per branch: a ship in one worktree
        must neither replace nor land another worktree's, `land` on a branch lands that
        branch's record, `land` off every recorded branch lands whichever have landed and
        keeps the rest, and `--merge` lands the record its own run wrote. Both trunk layouts:
        checked out in the main worktree, and checked out nowhere."""

        def entries(self, fork: str) -> dict:
            return pending_entries(ctx_for(fork, need_trunk=False, strict_mirror=False))

        def worktree(self, fork: str, name: str, path: str = "", content: str = "") -> str:
            """A linked worktree on a new branch `name` off develop, with one commit of a
            file of its own (or `content` for shared.tf)."""
            wt = os.path.join(self.tmp, path or name.replace("/", "-"))
            sh("git", "worktree", "add", "-q", "-b", name, wt, "develop", cwd=fork)
            if content:
                commit_fork(wt, "shared.tf", content, "ours: " + name)
            else:
                commit_fork(wt, "ours/%s.txt" % name.replace("/", "-"), name + "\n",
                            "ours: " + name)
            return wt

        def ship_in(self, wt: str, *flags: str) -> None:
            next_utc_second()                     # each ship names a `backup/<time>-pre-ship`
            code, out, err = run("-C", wt, "ship", *flags)
            self.assertEqual(code, 0, err + out)

        def two_shipped(self, fork: str) -> Tuple[str, str, dict, dict]:
            w1, w2 = self.worktree(fork, "feat/a"), self.worktree(fork, "feat/b")
            self.ship_in(w1)
            self.ship_in(w2)
            entries = self.entries(fork)
            self.assertEqual(sorted(entries), ["feat/a", "feat/b"])     # neither replaced
            for name, entry in entries.items():
                self.assertEqual(entry["commit"], origin_sha(fork, name))
            self.assertEqual(self.entries(w1), entries)                  # one map for all
            return w1, w2, entries["feat/a"], entries["feat/b"]

        def test_trunk_in_the_main_worktree(self):
            fork = make_fork(self.tmp)
            w1, w2, a, b = self.two_shipped(fork)
            base = rev(fork, "refs/heads/develop")

            # `status` anywhere lists both
            code, out, err = run("-C", w1, "status", "--offline")
            self.assertEqual(code, 0, err + out)
            lines = [ln for ln in out.splitlines() if ln.startswith("  pending  ")]
            self.assertEqual([ln.split()[2] for ln in lines], ["feat/a", "feat/b"])

            # in W1 the trunk is elsewhere: refused before anything, naming W1's record
            code, out, err = run("-C", w1, "land")
            self.assertEqual(code, 2, err + out)
            self.assertIn("run `forkflow land feat/a` there", err)
            self.assertEqual(self.entries(fork), {"feat/a": a, "feat/b": b})

            # in the main worktree, off every recorded branch: nothing has landed yet
            code, out, err = run("-C", fork, "land")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), base)
            self.assertEqual(self.entries(fork), {"feat/a": a, "feat/b": b})

            # a merges: `land` there lands a - and only a
            self.move_trunk(a["commit"])
            code, out, err = run("-C", fork, "land")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), a["commit"])
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.entries(fork), {"feat/b": b})
            self.assertEqual(rev(fork, "refs/heads/feat/a"), a["commit"])   # W1 has it out
            self.assertEqual(rev(fork, "refs/heads/feat/b"), b["commit"])
            self.assertEqual(checked_out(w1), "feat/a")
            self.assertEqual(checked_out(w2), "feat/b")

            # b merges later, rebased onto a by the platform: the route W1's refusal named,
            # with the branch, lands it by patch
            tip = self.human(("cherry-pick", b["commit"]))
            code, out, err = run("-C", fork, "land", "feat/b")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), tip)
            self.assertEqual(self.entries(fork), {})

        def test_trunk_checked_out_nowhere(self):
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-q", "-b", "scratch", "develop", cwd=fork)
            w1, w2, a, b = self.two_shipped(fork)
            base = rev(fork, "refs/heads/develop")

            # b merges; `land` in W1 answers for W1's own branch, and a has not landed
            self.move_trunk(b["commit"])
            code, out, err = run("-C", w1, "land")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(checked_out(w1), "feat/a")                  # HEAD did not move
            self.assertEqual(rev(fork, "refs/heads/develop"), base)
            self.assertEqual(rev(fork, "refs/heads/feat/b"), b["commit"])
            self.assertEqual(self.entries(fork), {"feat/a": a, "feat/b": b})

            # `land` in W2 lands b there
            code, out, err = run("-C", w2, "land")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(checked_out(w2), "develop")
            self.assertEqual(rev(fork, "refs/heads/develop"), b["commit"])
            self.assertEqual(rev(fork, "refs/heads/feat/b"), "")         # landed, deleted
            self.assertEqual(self.entries(fork), {"feat/a": a})
            self.assertEqual(checked_out(w1), "feat/a")

            # then a, rebased onto b: the trunk is now out in W2, where `land feat/a` lands it
            tip = self.human(("cherry-pick", a["commit"]))
            code, out, err = run("-C", w1, "land")
            self.assertEqual(code, 2, err + out)
            self.assertIn("run `forkflow land feat/a` there", err)
            code, out, err = run("-C", w2, "land", "feat/a")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), tip)
            self.assertEqual(self.entries(fork), {})
            self.assertEqual(rev(fork, "refs/heads/feat/a"), a["commit"])   # W1 has it out

        def test_ship_continue_keeps_the_other_worktrees_record(self):
            fork = make_fork(self.tmp)
            w1 = self.worktree(fork, "feat/a",
                               content=self.BASE_TF.replace("count = 1", "count = 2"))
            second_clone_commit(self.tmp, path="shared.tf",
                                content=self.BASE_TF.replace("count = 1", "count = 9"))
            code, out, err = run("-C", w1, "ship")
            self.assertEqual(code, 4, err + out)                         # stopped mid-rebase
            w2 = self.worktree(fork, "feat/b")
            self.ship_in(w2)
            b = self.entries(fork)["feat/b"]
            write(w1, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=w1)
            sh("git", "rebase", "--continue", cwd=w1)
            next_utc_second()
            code, out, err = run("-C", w1, "ship", "--continue")
            self.assertEqual(code, 0, err + out)
            entries = self.entries(fork)
            self.assertEqual(sorted(entries), ["feat/a", "feat/b"])
            self.assertEqual(entries["feat/b"], b)
            self.assertEqual(entries["feat/a"]["commit"], origin_sha(fork, "feat/a"))

        def test_merge_lands_its_own_record_while_another_worktree_ships(self):
            """W1 runs `ship --merge`; while the platform merges, W2 ships feat/b. W1 lands
            the record its own run wrote - not the one W2 wrote a moment ago - and leaves
            W2's in place."""
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-q", "-b", "scratch", "develop", cwd=fork)   # trunk free
            w1, w2 = self.worktree(fork, "feat/a"), self.worktree(fork, "feat/b")
            write(w1, CONFIG_FILE, 'merge = "self"\n')                   # untracked, as setup
            merging_tool(self.tmp, "glab")
            done, log = os.path.join(self.tmp, "w2-shipped"), os.path.join(self.tmp, "w2.log")
            fake_tool(os.path.join(self.tmp, "wrap"), "glab", (
                'if [ "$2" = merge ] && [ ! -e {done} ]; then\n'
                '  : > {done}; sleep 1.1\n'
                '  {py} {script} -C {w2} ship > {log} 2>&1\n'
                'fi\n'
                'exec {real} "$@"\n').format(
                    done=shlex.quote(done), py=shlex.quote(sys.executable),
                    script=shlex.quote(os.path.abspath(__file__)), w2=shlex.quote(w2),
                    log=shlex.quote(log),
                    real=shlex.quote(os.path.join(self.tmp, "bin", "glab"))))
            next_utc_second()
            with on_platform("gitlab"):
                code, out, err = run("-C", w1, "ship", "--merge")
            self.assertTrue(os.path.exists(done))                        # W2 did ship mid-way
            self.assertEqual(code, 0, err + out)
            shipped = self.value(self.tool_argv_for("gitlab", "merge"), "--sha")
            self.assertEqual(rev(fork, "refs/heads/develop"), shipped)
            self.assertEqual(checked_out(w1), "develop")
            self.assertEqual(rev(fork, "refs/heads/feat/a"), "")
            entries = self.entries(fork)
            self.assertEqual(sorted(entries), ["feat/b"])                # W2's, kept
            self.assertEqual(entries["feat/b"]["commit"], origin_sha(fork, "feat/b"))
            self.assertEqual(entries["feat/b"]["commit"], rev(fork, "refs/heads/feat/b"))

        def test_a_dry_run_with_several_pending_says_it_could_not_tell(self):
            """A dry run does not fetch, so with several records the "no" beside each was
            read off the refs on disk - and the sentence under them sent the user to merge
            two merge requests that the very next non-dry `land` lands without complaint.
            The caveat was written for the one-record path only; both paths say it now."""
            fork = make_fork(self.tmp)
            w1, w2, a, b = self.two_shipped(fork)
            base = rev(fork, "refs/heads/develop")
            self.human(("merge", "--no-ff", "-m", "m", a["commit"]),
                       ("merge", "--no-ff", "-m", "m2", b["commit"]))       # both merged on origin
            code, out, err = run("-C", fork, "land", "--dry-run")
            self.assertEqual(code, 2, err + out)
            self.assertIn("did not fetch", err)
            self.assertIn("without `--dry-run`", err)
            self.assertNotIn("merge, then run", err)             # what it used to say
            self.assertIn("`feat/a`", err)                       # each record is still named
            self.assertIn("`feat/b`", err)
            self.assertEqual(self.entries(fork), {"feat/a": a, "feat/b": b})
            self.assertEqual(rev(fork, "refs/heads/develop"), base)
            code, out, err = run("-C", fork, "land")              # and it lands them
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.entries(fork), {})

        def test_several_pending_and_none_merged_still_says_merge_them(self):
            """The refusal the dry run must not borrow: a real `land` has fetched, so
            "nothing pending is on the trunk yet - merge, then run it again" is true there."""
            fork = make_fork(self.tmp)
            w1, w2, a, b = self.two_shipped(fork)
            code, out, err = run("-C", fork, "land")
            self.assertEqual(code, 2, err + out)
            self.assertIn("nothing pending is on origin/develop yet", err)
            self.assertNotIn("did not fetch", err)

        def test_force_needs_the_record_named_when_several_are_pending(self):
            """`--force` judges nothing, so it must not pick among several on its own: off
            every recorded branch it is refused outright - not quietly turned into a plain
            `land` of whichever has landed - and named it acts on that record only."""
            fork = make_fork(self.tmp)
            w1, w2, a, b = self.two_shipped(fork)
            base = rev(fork, "refs/heads/develop")
            self.move_trunk(a["commit"])                               # a merged, b not
            code, out, err = run("-C", fork, "land", "--force")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(self.entries(fork), {"feat/a": a, "feat/b": b})
            self.assertEqual(rev(fork, "refs/heads/develop"), base)
            code, out, err = run("-C", fork, "land", "feat/nope")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(self.entries(fork), {"feat/a": a, "feat/b": b})
            self.assertEqual(rev(fork, "refs/heads/develop"), base)
            code, out, err = run("-C", fork, "land", "--force", "feat/b")   # b: closed, say
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.entries(fork), {"feat/a": a})
            self.assertEqual(rev(fork, "refs/heads/develop"), a["commit"])  # whatever origin has
            self.assertEqual(rev(fork, "refs/heads/feat/b"), b["commit"])   # kept: unverified

        def test_merge_lands_its_own_entry_even_when_the_record_cannot_be_written(self):
            """`save_state` swallows a write that fails - a restore point that cannot be
            recorded is still one - so `--merge` must not depend on reading its record back:
            it lands the entry it holds. Every write of the state file fails here while the
            file stays READABLE, because those are two different facts now: one that cannot
            be read is a memory that has been made to forget, and the test below is that."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            name = self.feature(fork)

            def dies(data, fh, **kw):
                raise OSError("disk full")

            with on_platform(self.platform("gitlab")), mock.patch.object(json, "dump", dies):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            shipped = self.value(self.tool_argv_for("gitlab", "merge"), "--sha")
            self.assertEqual(rev(fork, "refs/heads/develop"), shipped)
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(rev(fork, "refs/heads/" + name), "")
            self.assertEqual(self.entries(fork), {})            # nothing was recorded
            self.assertFalse(os.path.exists(git_path(fork, STATE_FILE)))

        def test_a_state_file_that_cannot_be_read_refuses_merge_before_any_push(self):
            """The other half. A state file that cannot be READ is where this clone's memory
            of the original project's configs went, and without it a version the project has
            withdrawn reads as one it never had - so `--merge` is refused, at the gate,
            before anything is pushed, naming the file. A plain `ship` is not refused: it
            does not depend on that memory."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            name = self.feature(fork)
            blocker = os.path.join(fork, ".git", STATE_FILE)        # the main worktree's
            write(fork, os.path.join(".git", STATE_FILE, "keep"), "")
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, EXIT_PRECONDITION, err + out)
            self.assertIn(blocker, err)
            self.assertIn("merged by hand", err)
            self.assertEqual(origin_sha(fork, name), "")            # nothing pushed
            self.assertEqual(self.tool_argv_for("gitlab", "merge"), [])
            self.assertTrue(os.path.isdir(blocker))                 # and left alone
            self.assertEqual(run("-C", fork, "status")[0], 0)       # the rest goes on
            self.ship_in(fork)                                      # and so does a plain ship
            self.assertNotEqual(origin_sha(fork, name), "")

        def b_landed_while_on_a(self) -> Tuple[str, dict, dict]:
            """One worktree: feat/b shipped, then feat/a shipped and still checked out, and
            only feat/b's merge request merged."""
            fork = make_fork(self.tmp)
            for name in ("feat/b", "feat/a"):          # a patch of its own each, or git cherry
                self.feature(fork, name, commits=0)    # calls one the other's landing
                commit_fork(fork, "ours/%s.txt" % name[-1], name + "\n", "ours: " + name)
                self.ship_in(fork)
            a, b = self.entries(fork)["feat/a"], self.entries(fork)["feat/b"]
            self.move_trunk(b["commit"])
            return fork, a, b

        def assert_b_landed_a_kept(self, fork: str, a: dict, b: dict) -> None:
            self.assertEqual(rev(fork, "refs/heads/develop"), b["commit"])
            self.assertEqual(rev(fork, "refs/heads/feat/b"), "")
            self.assertEqual(rev(fork, "refs/heads/feat/a"), a["commit"])
            self.assertEqual(self.entries(fork), {"feat/a": a})

        def test_the_landed_command_status_prints_works_from_a_branch_with_its_own_record(self):
            """`status` said "landed: run forkflow land" of feat/b; run from feat/a, that
            plain `land` answered for feat/a only - "not merged yet" - and nothing landed.
            The verdict names the branch, and run as printed it lands feat/b."""
            fork, a, b = self.b_landed_while_on_a()
            code, out, err = run("-C", fork, "status", "--fetch")
            self.assertEqual(code, 0, err + out)
            line = next(ln for ln in out.splitlines()
                        if ln.strip().startswith("pending") and " feat/b " in ln)
            cmd = line.split("landed: run ", 1)[1].strip()
            self.assertEqual(checked_out(fork), "feat/a")
            code, out, err = run("-C", fork, *shlex.split(cmd)[1:])
            self.assertEqual(code, 0, err + out)
            self.assert_b_landed_a_kept(fork, a, b)

        def test_land_on_an_unlanded_branch_names_the_records_that_landed(self):
            """Plain `land` on feat/a picks feat/a's record; not merged, it is exit 2 with
            nothing moved - and it names feat/b, which has landed, as the command that lands
            it. That command, run as printed, does."""
            fork, a, b = self.b_landed_while_on_a()
            develop = rev(fork, "refs/heads/develop")
            code, out, err = run("-C", fork, "land")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(rev(fork, "refs/heads/develop"), develop)
            self.assertEqual(checked_out(fork), "feat/a")
            self.assertEqual(self.entries(fork), {"feat/a": a, "feat/b": b})
            self.assertNotIn("forkflow land feat/a", err)     # only the ones that landed
            code, out, err2 = run_printed(err, "forkflow land feat/", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assert_b_landed_a_kept(fork, a, b)

    class TestShipAfterTheBranchLeftOrigin(MergeBase):
        """A fetch does not prune, so `origin/<branch>` outlives the branch on origin when
        the platform removes it after a merge (GitLab's `--remove-source-branch`). Offered as
        the lease, it made every later ship of that name exit 5 "(stale info)", each attempt
        leaving one more backup behind."""

        def gone_from_origin(self, name: str) -> None:
            sh("git", "--git-dir=" + os.path.join(self.tmp, "origin.git"),
               "update-ref", "-d", "refs/heads/" + name)

        def test_a_ship_after_a_merge_and_before_land_creates_the_branch_again(self):
            fork, name, entry = self.shipped()
            self.move_trunk(entry["commit"])                           # merged in the UI ...
            self.gone_from_origin(name)                                # ... source removed
            commit_fork(fork, "ours/more.txt", "more\n", "ours: more")
            tracking = "refs/remotes/origin/" + name
            self.assertEqual(rev(fork, tracking), entry["commit"])    # stale, still here
            next_utc_second()
            code, out, err = run("-C", fork, "ship", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, tracking), entry["commit"])    # a dry run drops nothing
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            self.assertEqual(rev(fork, tracking), rev(fork, "refs/heads/" + name))
            self.assertEqual(sh("git", "rev-list", "--count", entry["commit"] + ".." + name,
                                cwd=fork), "1")                       # the new work, squashed
            backup = self.backup_branch(fork)                        # rule 4 still honoured
            self.assertEqual(origin_sha(fork, backup), rev(fork, "refs/heads/" + backup))
            self.assertEqual(self.pending_of(fork, name)["commit"], origin_sha(fork, name))

        def test_land_force_drops_the_stale_ref_and_the_kept_branch_ships_again(self):
            """The request was merged in a way `land` cannot see, and the platform removed the
            branch: `land --force` keeps the local branch - and must not keep the stale
            `origin/<branch>` that would block its next ship."""
            fork, name, entry = self.shipped()
            self.gone_from_origin(name)
            code, out, err = run("-C", fork, "land", "--force")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(rev(fork, "refs/heads/" + name), entry["commit"])   # kept
            self.assertEqual(rev(fork, "refs/remotes/origin/" + name), "")      # not stale
            self.assertEqual(self.pending_of(fork), {})
            sh("git", "checkout", "-q", name, cwd=fork)
            commit_fork(fork, "ours/more.txt", "more\n", "ours: more")
            next_utc_second()
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))

        def test_a_branch_origin_still_has_keeps_its_lease(self):
            """Only origin's own "no such branch" drops the lease: a branch still there is
            replaced behind `--force-with-lease` as before."""
            fork, name, entry = self.shipped()
            commit_fork(fork, "ours/more.txt", "more\n", "ours: more")
            next_utc_second()
            code, out, err = run("-C", fork, "ship")
            self.assertEqual(code, 0, err + out)
            self.assertIn("--force-with-lease=%s:%s" % (name, entry["commit"]), out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))

        def test_the_stopped_rebase_hint_carries_merge(self):
            """`ship --merge` stops on a conflict; the second commit stops the rebase again,
            and `ship --continue --merge` run too early names the resume. Followed to the
            letter once the rebase is done, it merges and lands."""
            fork = self.self_fork()
            name = self.feature(fork, commits=0)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: two")
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                        "ours: three")
            second_clone_commit(self.tmp, path="shared.tf",
                                content=self.BASE_TF.replace("count = 1", "count = 9"))
            with on_platform(self.platform("gitlab")):
                self.assertEqual(run("-C", fork, "ship", "--merge")[0], 4)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            sh("git", "rebase", "--continue", cwd=fork, check=False)   # stops on the second
            self.assertTrue(rebase_in_progress(ctx_for(fork, strict_mirror=False)))
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--continue", "--merge")
            self.assertEqual(code, 2, err + out)
            hinted = re.search(r"and then `(forkflow ship[^`]*)`", err)
            self.assertTrue(hinted, err)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 5"))
            sh("git", "add", "shared.tf", cwd=fork)
            sh("git", "rebase", "--continue", cwd=fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, *hinted.group(1).split()[1:])
            self.assertEqual(code, 0, err + out)
            self.assertEqual(self.tool_argv_for("gitlab", "merge")[:3], ["mr", "merge", name])
            self.assertEqual(origin_sha(fork, "develop"), rev(fork, "refs/heads/develop"))
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(fork), {})

    class TestPrintedRerunsCarryMerge(MergeBase):
        """Every `forkflow sync` / `forkflow ship` a `--merge` run prints carries `--merge`.

        Followed to the letter, one without it pushes and opens the request, merges nothing,
        and exits 0 with a line that reads like success - found three rounds running at one
        site after another, so every such command is built by `rerun_cmd` / `continue_cmd`
        (`TestSourceInvariants` holds that). Each site here is reached for real, and its
        command run exactly as printed has to end merged and landed."""

        def run_it(self, text: str, start: str, fork: str) -> Tuple[int, str, str]:
            with on_platform("gitlab"):
                return run_printed(text, start, fork)

        def merged_and_landed(self, fork: str, branch: str) -> None:
            self.assertEqual(self.tool_argv_for("gitlab", "merge")[:3], ["mr", "merge", branch])
            self.assertEqual(origin_sha(fork, "develop"), rev(fork, "refs/heads/develop"))
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(fork, branch), {})

        def conflicted_sync(self) -> str:
            """A `merge = "self"` fork whose `sync --merge` stopped on a conflict."""
            fork = self.self_fork()
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                            "theirs: shared")
            with on_platform(self.platform("gitlab")):
                self.assertEqual(run("-C", fork, "sync", "--merge")[0], 4)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            return fork

        def test_a_sync_committed_by_hand_is_resumed_with_merge(self):
            """The conflict resolved and the merge committed by hand, the user reruns
            `sync --merge`: the branch exists, and the resume it names must merge."""
            fork = self.conflicted_sync()
            sh("git", "commit", "-q", "--no-edit", cwd=fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 2, err + out)
            code, out, err2 = self.run_it(err, "forkflow sync --continue", fork)
            self.assertEqual(code, 0, err2 + out)
            self.merged_and_landed(fork, sync_branch_name())

        def test_ship_on_a_sync_branch_names_the_sync_resume_with_merge(self):
            fork = self.conflicted_sync()
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 2, err + out)
            code, out, err2 = self.run_it(err, "forkflow sync --continue", fork)
            self.assertEqual(code, 0, err2 + out)
            self.merged_and_landed(fork, sync_branch_name())

        def test_a_published_sync_branch_is_redone_with_force_and_merge(self):
            """Today's sync branch is on origin and gone here: `--force` publishes `<name>-2`,
            and with `--merge` merges and lands it."""
            fork = self.self_fork()
            self.upstream_change()
            name = sync_branch_name()
            with on_platform(self.platform("gitlab")):
                self.assertEqual(run("-C", fork, "sync", "--mr")[0], 0)
            sh("git", "checkout", "-q", "develop", cwd=fork)
            sh("git", "branch", "-D", name, cwd=fork)
            next_utc_second()
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 2, err + out)
            code, out, err2 = self.run_it(err, "forkflow sync --force", fork)
            self.assertEqual(code, 0, err2 + out)
            self.merged_and_landed(fork, name + "-2")

        def test_a_sync_the_trunk_moved_under_is_redone_with_force_and_merge(self):
            """`check` fails on the tip because origin's trunk moved after the merge was made:
            the redo it names (`--force`) must merge. The gate asks for an untracked marker so
            the first stop is the gate's and the second the tip's."""
            fork = make_fork(self.tmp, config='merge = "self"\ngate = ["test -e .gate-ok"]\n')
            self.upstream_change()
            with on_platform(self.platform("gitlab")):
                self.assertEqual(run("-C", fork, "sync", "--merge")[0], 3)
            second_clone_commit(self.tmp)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            write(fork, ".gate-ok", "")
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--continue", "--merge")
            self.assertEqual(code, 3, err + out)
            next_utc_second()
            code, out, err2 = self.run_it(out, "forkflow sync --force", fork)
            self.assertEqual(code, 0, err2 + out)
            self.merged_and_landed(fork, sync_branch_name())

        def test_a_sync_branch_without_its_merge_is_redone_with_merge(self):
            """A branch under today's sync name carrying no merge: "nothing to continue".
            Plain `sync` would refuse the existing branch and send the user back to
            `--continue` - the rerun named is `--force`, and it merges."""
            fork = self.self_fork()
            self.upstream_change()
            sh("git", "checkout", "-q", "-b", sync_branch_name(), "develop", cwd=fork)
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--continue", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertIn("nothing to continue", err)
            code, out, err2 = self.run_it(err, "forkflow sync", fork)
            self.assertEqual(code, 0, err2 + out)
            self.merged_and_landed(fork, sync_branch_name())

        def test_no_ship_to_continue_names_ship_with_merge(self):
            fork = self.self_fork()
            name = self.feature(fork)
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--continue", "--merge")
            self.assertEqual(code, 2, err + out)
            code, out, err2 = self.run_it(err, "forkflow ship", fork)
            self.assertEqual(code, 0, err2 + out)
            self.merged_and_landed(fork, name)

        def test_an_aborted_rebase_names_ship_with_merge(self):
            """`git rebase --abort` after `ship --merge` stopped, then `ship --continue
            --merge`: "run `forkflow ship` again" lost the flag. Run as printed it stops on
            the same conflict, whose resume then merges and lands."""
            fork = self.self_fork()
            name = self.feature(fork, commits=0)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: two")
            second_clone_commit(self.tmp, path="shared.tf",
                                content=self.BASE_TF.replace("count = 1", "count = 9"))
            with on_platform(self.platform("gitlab")):
                self.assertEqual(run("-C", fork, "ship", "--merge")[0], 4)
            sh("git", "rebase", "--abort", cwd=fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--continue", "--merge")
            self.assertEqual(code, 2, err + out)
            next_utc_second()
            code, out, err = self.run_it(err, "forkflow ship", fork)
            self.assertEqual(code, 4, err + out)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            sh("git", "rebase", "--continue", cwd=fork)
            code, out, err2 = self.run_it(err, "forkflow ship --continue", fork)
            self.assertEqual(code, 0, err2 + out)
            self.merged_and_landed(fork, name)

    @needs_tomllib
    class TestForkMergeMode(ShipBase):
        """`fork_merge_mode` reads `merge` only where the original project cannot write it:
        the config committed on `origin/<trunk>` by this fork, or - while none is committed
        there - the fork's own untracked `.forkflow.toml`. Everything else is "manual"."""

        def mode(self, fork: str) -> str:
            return fork_merge_mode(ctx_for(fork))

        def advance_mirror(self, fork: str) -> None:
            """What a sync does to the mirror and nothing else: rule 6 keeps it a pristine
            copy of `upstream/main`, on origin and here."""
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "branch", "-f", "main", "upstream/main", cwd=fork)
            sh("git", "push", "-q", "origin", "main:refs/heads/main", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)

        def tips_lack(self, fork: str, text: str) -> None:
            for ref in ("upstream/main", "origin/main", "main"):
                held = sh("git", "show", ref + ":" + CONFIG_FILE, cwd=fork, check=False)
                self.assertNotEqual(config_fingerprint(held), config_fingerprint(text))

        def test_no_config_anywhere_is_manual(self):
            self.assertEqual(self.mode(make_fork(self.tmp)), "manual")

        def test_the_config_committed_on_the_remote_trunk_is_read(self):
            """And only that one: the checked-out branch committing, or the working tree
            holding, another value changes nothing."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            self.assertEqual(self.mode(fork), "self")
            sh("git", "checkout", "-q", "-b", "feat/x", cwd=fork)
            commit_fork(fork, CONFIG_FILE, 'merge = "manual"\n', "ours: manual on a branch")
            self.assertEqual(self.mode(fork), "self")
            sh("git", "checkout", "-q", "develop", cwd=fork)
            write(fork, CONFIG_FILE, 'merge = "manual"\n')         # an uncommitted edit
            self.assertEqual(self.mode(fork), "self")

        def test_the_checked_out_branch_is_not_read(self):
            """Nothing committed on the trunk: a branch whose tree says "self" - a sync branch
            carrying upstream's file, or a feature branch - is not this fork's config."""
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-q", "-b", "sync/upstream-x", cwd=fork)
            commit_fork(fork, CONFIG_FILE, 'merge = "self"\n', "a tree that says self")
            self.assertEqual(self.mode(fork), "manual")

        def test_an_untracked_file_is_read_while_nothing_is_committed(self):
            """Untracked only: staged, the file is on its way into a commit - as a stopped
            `git cherry-pick` of upstream's commit leaves it, with no MERGE_HEAD to tell."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "self"\n')
            self.assertEqual(self.mode(fork), "self")
            sh("git", "add", CONFIG_FILE, cwd=fork)
            self.assertEqual(self.mode(fork), "manual")

        def test_the_committed_config_wins_over_an_untracked_file(self):
            """A branch from before the config was committed, with an untracked file of its
            own: `origin/<trunk>`'s committed "manual" is what counts."""
            fork = make_fork(self.tmp, config='merge = "manual"\n')
            sh("git", "checkout", "-q", "-b", "old", "main", cwd=fork)
            write(fork, CONFIG_FILE, 'merge = "self"\n')
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            self.assertEqual(self.mode(fork), "manual")

        def test_a_file_head_or_a_merge_in_progress_holds_is_not_untracked(self):
            """Taken out of the index by hand, a file HEAD - or the MERGE_HEAD of a merge in
            progress - holds is still one a commit put there, and may be upstream's."""
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-q", "-b", "side", cwd=fork)
            commit_fork(fork, CONFIG_FILE, 'merge = "self"\n', "side: says self")
            sh("git", "rm", "-q", "--cached", CONFIG_FILE, cwd=fork)
            self.assertEqual(self.mode(fork), "manual")                   # HEAD holds it
            sh("git", "reset", "-q", "--hard", "HEAD", cwd=fork)
            sh("git", "checkout", "-q", "develop", cwd=fork)
            commit_fork(fork, "src/app.py", "ours\n", "ours: app")
            sh("git", "checkout", "-q", "side", cwd=fork)
            commit_fork(fork, "src/app.py", "side\n", "side: app")
            sh("git", "checkout", "-q", "develop", cwd=fork)
            sh("git", "merge", "side", cwd=fork, check=False)           # stops on src/app.py
            self.assertTrue(merge_in_progress(fork))
            sh("git", "rm", "-q", "--cached", CONFIG_FILE, cwd=fork)
            self.assertEqual(self.mode(fork), "manual")                   # MERGE_HEAD holds it

        def test_a_config_upstream_wrote_on_the_trunk_is_not_the_forks(self):
            """A trunk bootstrapped from an upstream that tracks the file: upstream's
            `merge = "self"` is on `origin/<trunk>` with nobody here having written it - and
            stays upstream's through a ship that does not touch it. A commit of this fork's
            that writes the file makes it the fork's."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, CONFIG_FILE, 'merge = "self"\n', "theirs: forkflow")
            push_upstream_into_origin(self.tmp, "develop")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(sh("git", "show", "origin/develop:" + CONFIG_FILE, cwd=fork),
                             'merge = "self"')
            self.assertEqual(self.mode(fork), "manual")
            second_clone_commit(self.tmp)                                # a ship, not the file
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(self.mode(fork), "manual")
            second_clone_commit(self.tmp, path=CONFIG_FILE,
                                content='merge = "self"\ngate = []\n')   # the fork writes it
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(self.mode(fork), "self")

        def test_upstreams_own_file_untracked_on_disk_is_not_this_forks(self):
            """A fork that never chose `merge`: an ordinary sync brings upstream's
            `.forkflow.toml` in and the user untracks it by hand (`git rm --cached`,
            committed on a sync branch that is then abandoned). That leaves upstream's
            bytes on disk in exactly the state `setup` leaves this fork's own template -
            absent from the index, from HEAD and from MERGE_HEAD - and upstream's
            `merge = "self"` opened the gate. Where git holds the file cannot tell the two
            apart; the bytes can, and one character of this fork's own settles it."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch theirs"]\n'
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            write(fork, CONFIG_FILE, theirs)
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            self.assertEqual(self.mode(fork), "manual")
            write(fork, CONFIG_FILE, theirs + "# ours\n")
            self.assertEqual(self.mode(fork), "self")

        def test_upstreams_bytes_are_upstreams_however_they_reached_the_trunk(self):
            """`git log -1 <rev> -- <path>` named this fork's commit whenever the file was
            re-added under another path - which is exactly what the case-variant `git mv`
            remedy this tool prints does to upstream's file - so upstream's `merge` became
            the fork's by a rename nobody read as a declaration. Built with plumbing, so it
            holds on any filesystem: upstream tracks `.ForkFlow.toml`, and a commit of this
            fork's adds `.forkflow.toml` holding upstream's bytes. The bytes did not
            change, so provenance does not either."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch theirs"]\n'
            commit_upstream(self.tmp, ".ForkFlow.toml", theirs, "theirs: variant")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            blob = sh("git", "hash-object", "-w", write(self.tmp, "blob.txt", theirs),
                      cwd=fork)
            sh("git", "update-index", "--add", "--cacheinfo",
               "100644,%s,%s" % (blob, CONFIG_FILE), cwd=fork)
            sh("git", "commit", "-q", "-m", "rename the config upstream brought in", cwd=fork)
            sh("git", "push", "-q", "origin", "develop", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(sh("git", "log", "-1", "--format=%s", "origin/develop", "--",
                                CONFIG_FILE, cwd=fork),
                             "rename the config upstream brought in")   # this fork's commit
            self.assertEqual(self.mode(fork), "manual")
            commit_fork(fork, CONFIG_FILE, theirs + "# ours\n", "ours: our own config")
            sh("git", "push", "-q", "origin", "develop", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(self.mode(fork), "self")

        def test_a_config_upstream_no_longer_has_at_its_tip_is_still_upstreams(self):
            """The scope `upstream_config_digests` reads is every version the history of
            `<upstream>/<branch>` and of the mirror has carried, not their tips. So a
            config the fork adopted whole at its last sync stays upstream's after upstream
            has edited its own since - here it is still the merge base of upstream and the
            trunk, which the tip-sampling scope read, and the two tests below are the same
            question where nothing but the walk can answer it."""
            fork = make_fork(self.tmp)
            adopted = 'merge = "self"\n'
            commit_upstream(self.tmp, CONFIG_FILE, adopted, "theirs: forkflow")
            push_upstream_into_origin(self.tmp, "develop")          # the trunk takes it
            commit_upstream(self.tmp, CONFIG_FILE, adopted + "# theirs, changed\n",
                            "theirs: forkflow again")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertNotEqual(sh("git", "show", "upstream/main:" + CONFIG_FILE, cwd=fork),
                                sh("git", "show", "origin/develop:" + CONFIG_FILE, cwd=fork))
            self.assertEqual(self.mode(fork), "manual")

        def test_a_version_upstream_has_retired_is_still_upstreams(self):
            """The sixth route into the gate, and the one the tip-sampling scope left open.
            Upstream's `.forkflow.toml`, brought in by a sync and untracked by hand, is
            refused while upstream still has those bytes somewhere - and upstream then
            edits its own config. The stale file in the working tree now matches no tip:
            not `upstream/main`, not the mirror on origin or here, and the trunk never took
            it (the sync MR was abandoned). Nothing this fork wrote, nobody's review - and
            the file used to become "the fork's own" the moment upstream moved on. Only the
            history answers it, and it does: every version upstream ever had is upstream's,
            and one character of this fork's own still opens the gate."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch theirs"]\n'
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            self.advance_mirror(fork)
            write(fork, CONFIG_FILE, theirs)     # a sync brought it in; untracked by hand
            self.assertEqual(self.mode(fork), "manual")
            commit_upstream(self.tmp, CONFIG_FILE, theirs + "# theirs, tidied\n",
                            "theirs: forkflow again")
            self.advance_mirror(fork)
            self.tips_lack(fork, theirs)         # no tip holds those bytes any more
            self.assertEqual(self.mode(fork), "manual")
            write(fork, CONFIG_FILE, theirs + "# ours\n")
            self.assertEqual(self.mode(fork), "self")

        def test_a_config_reached_through_a_symlink_is_not_judged_at_all(self):
            """The two sides of the comparison have to be the same kind of thing. Upstream
            keeps its settings in `theirs-real.toml` and a LINK at `.forkflow.toml`, so every
            blob upstream ever stored under the config's name is the string
            "theirs-real.toml" - while reading the path gives the settings the link points
            at. A sync brings both in, the link is left on disk, and upstream's own
            `merge = "self"` (with upstream's `gate` behind it) used to read as this fork's
            own file: scratchpad `f9/repro1.py` gets `untracked_own` and `self` out of the
            body before this one. forkflow does not reproduce git's rendering rules to
            compare like with like - it refuses, names the link, and prints the way out."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch theirs"]\n'
            write(fork, "theirs-real.toml", theirs)
            os.symlink("theirs-real.toml", os.path.join(fork, CONFIG_FILE))
            ctx = ctx_for(fork)
            self.assertEqual(fork_config_state(ctx)[0], "unprovable")
            self.assertEqual(self.mode(fork), "manual")
            why = fork_config_state(ctx)[2]
            self.assertIn("symbolic link", why)
            self.assertIn("theirs-real.toml", why)             # the link and its target
            self.assertIn("cp -p --", why)                     # copied aside before anything
            self.assertIn(git_path(fork, "forkflow-config-"), why)   # and the copy is named
            self.assertIn(without_stamp(why), without_stamp(fork_merge_refusal(ctx)))
            # a file of this fork's own, written here, is this fork's own again
            os.unlink(os.path.join(fork, CONFIG_FILE))
            write(fork, CONFIG_FILE, theirs + "# ours\n")
            self.assertEqual(self.mode(fork), "self")

        def theirs_as_a_link(self, fork: str, theirs: str) -> None:
            """The original project keeps its settings in `theirs-real.toml` and a LINK at
            `.forkflow.toml`. A sync brings both in and they are left on disk untracked -
            the state the symlink refusal is printed in."""
            commit_upstream(self.tmp, "theirs-real.toml", theirs, "theirs: settings")
            seed = os.path.join(self.tmp, "seed")
            os.symlink("theirs-real.toml", os.path.join(seed, CONFIG_FILE))
            sh("git", "add", "-A", cwd=seed)
            sh("git", "commit", "-m", "theirs: the config is a link", cwd=seed)
            sh("git", "push", "origin", "main", cwd=seed)
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "checkout", "-q", "upstream/main", "--", "theirs-real.toml",
               CONFIG_FILE, cwd=fork)
            sh("git", "rm", "-q", "--cached", "--", "theirs-real.toml", CONFIG_FILE, cwd=fork)

        def test_the_symlink_remedy_leaves_no_config_behind(self):
            """A REMEDY MUST NEVER PRODUCE CONFIG BYTES, and this one did. It ended
            `cp -p -- <the copy> .forkflow.toml`, so following it put the LINK TARGET's
            contents at the config's path - bytes no `.forkflow.toml` git stores has ever
            held, matching nothing in `upstream_config_digests`, reading as this fork's own.
            Run exactly as printed it turned the original project's `merge = "self"`, with
            the original project's `gate` behind it, into this fork's declaration
            (`f10/repro23.py`).

            What it prints now takes the link away, names where what it read went, and stops
            there. This test runs every command it prints, in the state it is printed in."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch pwned"]\n'
            self.theirs_as_a_link(fork, theirs)
            ctx = ctx_for(fork)
            self.assertEqual(fork_config_state(ctx)[0], "unprovable")
            why = fork_config_state(ctx)[2]

            ran = run_every_printed(why, fork)
            self.assertTrue(ran)
            self.assertEqual([rc for _, rc in ran], [0] * len(ran), ran)

            # nothing the tool produced is at the config's path - there is no file there
            self.assertFalse(os.path.lexists(os.path.join(fork, CONFIG_FILE)))
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "none")
            self.assertEqual(self.mode(fork), "manual")
            # and what was there is kept, at the one path the refusal named
            kept = kept_configs(fork)
            self.assertEqual(len(kept), 1)
            self.assertIn(kept[0], why)
            with open(kept[0]) as fh:
                self.assertEqual(fh.read(), theirs)
            self.assertTrue(os.path.exists(os.path.join(fork, "theirs-real.toml")))
            # the way forward is the user's own file, and it works
            write(fork, CONFIG_FILE, 'merge = "self"\n# ours\n')
            self.assertEqual(self.mode(fork), "self")

        def test_the_attribute_remedy_leaves_no_config_behind(self):
            """The other half of the same defect. Turning the attribute off in
            `info/attributes` fixes what git does NEXT time and leaves the already-converted
            file exactly where it is - and that file is the dangerous one: `ident` expanded
            `$Id$` into it, so its bytes are in no upstream digest set and it reads as this
            fork's own. Run as printed, the remedy opened `--merge` on the original
            project's settings (`f10/repro23.py`).

            It now ends by taking that file off disk, with what was there copied aside and
            named first, and says to write your own."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch pwned"]\n# $Id$\n'
            rendered = self.theirs_through_an_attribute(fork, "ident", theirs)
            self.assertIn(b"$Id: ", rendered)       # git really expanded it
            ctx = ctx_for(fork)
            self.assertEqual(fork_config_state(ctx)[0], "unprovable")
            why = fork_config_state(ctx)[2]

            ran = run_every_printed(why, fork)
            self.assertTrue(ran)
            self.assertEqual([rc for _, rc in ran], [0] * len(ran), ran)

            self.assertFalse(os.path.lexists(os.path.join(fork, CONFIG_FILE)))
            state = fork_config_state(ctx_for(fork))
            self.assertEqual(state[2], "")          # the attribute is off, nothing refused
            self.assertEqual(state[0], "none")      # and no config was produced
            self.assertEqual(self.mode(fork), "manual")
            kept = kept_configs(fork)
            self.assertEqual(len(kept), 1)
            self.assertIn(kept[0], why)
            with open(kept[0], "rb") as fh:
                self.assertEqual(fh.read(), rendered)
            # the way back here is NOT another untracked file: the original project's own
            # history sets `ident` on that path, so anything untracked at it can be
            # something git wrote (`config_history_render_unprovable`). The way the refusal
            # names is the trunk, where a config is read as a blob
            write(fork, CONFIG_FILE, 'merge = "self"\n# ours\n')
            self.assertEqual(self.mode(fork), "manual")
            self.assertIn("committed on `origin/develop`",
                          fork_config_state(ctx_for(fork))[2])
            self.ship_the_config(fork)
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "trunk_own")
            self.assertEqual(self.mode(fork), "self")

        def ship_the_config(self, fork: str) -> None:
            """The fork's own `.forkflow.toml` on `origin/<trunk>`, the way the refusals
            here name: committed on a branch off the trunk and merged onto it."""
            sh("git", "add", "--", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "-m", "ours: our own config", cwd=fork)
            sh("git", "push", "-q", "origin", "HEAD:refs/heads/develop", cwd=fork)
            sh("git", "fetch", "-q", "--prune", "origin", cwd=fork)

        def test_following_only_the_first_printed_command_is_still_refused(self):
            """THE ATTRIBUTE IS A FACT ABOUT NOW AND THE CONVERSION HAPPENED AT CHECKOUT.

            The refusal prints two commands and labels the first "First,". Running only that
            one - the likeliest way a person follows a two-part instruction - turned the
            attribute off in `info/attributes` and left the converted file exactly where it
            was: `check-attr` then said `unset`, NO refusal was produced at all, and the
            `ident`-expanded bytes of the original project's config read as `untracked_own`
            with `merge = "self"` in them. Scratchpad `q1/E4.sh` runs it end to end and
            upstream's `gate` runs as shell at the end of it.

            The round before answered the same defect by printing a SECOND command rather
            than by making the state safe, and its test ran every command printed, so the
            half-followed case was never exercised. This one runs the first and stops."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch pwned"]\n# $Id$\n'
            rendered = self.theirs_through_an_attribute(fork, "ident", theirs)
            ctx = ctx_for(fork)
            self.assertEqual(fork_config_state(ctx)[0], "unprovable")
            why = fork_config_state(ctx)[2]
            printed = [c for c in re.findall(r"`([^`]+)`", why)
                       if c.startswith(SHELL_VERBS)]
            self.assertGreater(len(printed), 1, printed)     # it really is a two-part remedy
            for cmd in printed[:-1]:                         # everything but the last step,
                self.assertEqual(subprocess.run(["sh", "-c", cmd], cwd=fork,   # which is the
                                                capture_output=True).returncode, 0, cmd)
                                                             # one that takes the file away

            # the attribute is off now, and the file git converted is untouched
            self.assertTrue(sh("git", "check-attr", "ident", "--", CONFIG_FILE,
                               cwd=fork).endswith("unset"))
            with open(os.path.join(fork, CONFIG_FILE), "rb") as fh:
                self.assertEqual(fh.read(), rendered)

            state = fork_config_state(ctx_for(fork))
            self.assertEqual(state[0], "unprovable")         # and it is STILL refused
            self.assertIn("already judged unprovable", state[2])
            self.assertIn(CONFIG_FILE, state[2])
            self.assertEqual(self.mode(fork), "manual")
            # the verdict is about these bytes, so it is written down where the original
            # project cannot reach it
            self.assertEqual(read_state(ctx, shared=True)[RENDERED_CONFIGS],
                             [config_digest(rendered.decode("utf-8"))])
            # and what this refusal prints, run as printed, ends with no config at all
            self.assertEqual([rc for _, rc in run_every_printed(state[2], fork)], [0])
            self.assertFalse(os.path.lexists(os.path.join(fork, CONFIG_FILE)))

        def test_an_attribute_the_project_has_since_deleted_still_refuses_what_it_wrote(self):
            """The other way the condition ends with the converted file still on disk, and
            this one needs no user action at all: the original project deletes the
            `.gitattributes` that set the attribute, and the fork syncs. The converted file
            is untracked, so the sync leaves it while taking the attribute away
            (`q1/render2.py` case B, `q1/E3.sh`).

            Nothing is written down here - this clone has never looked before - so the only
            thing that can answer is what the project's own history sets on that path
            (`config_history_render_unprovable`). An untracked config cannot answer for
            `merge` while that is so, and the way forward the refusal names is the one this
            tool recommends anyway: the config on the trunk, which is read as a blob."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch pwned"]\n# $Id$\n'
            rendered = self.theirs_through_an_attribute(fork, "ident", theirs)
            os.unlink(os.path.join(fork, ".gitattributes"))   # the project dropped it
            ctx = ctx_for(fork)
            self.assertEqual(git("check-attr", "ident", "--", CONFIG_FILE,
                                 cwd=fork).split(": ")[-1], "unspecified")
            self.assertEqual(read_state(ctx, shared=True).get(RENDERED_CONFIGS), None)
            with open(os.path.join(fork, CONFIG_FILE), "rb") as fh:
                self.assertEqual(fh.read(), rendered)        # still there, still converted

            state = fork_config_state(ctx)
            self.assertEqual(state[0], "unprovable")
            self.assertIn("ident", state[2])
            self.assertIn("does not have to be set NOW", state[2])
            self.assertEqual(self.mode(fork), "manual")
            # the way forward it names, taken: the fork's own config on the trunk
            os.unlink(os.path.join(fork, CONFIG_FILE))
            write(fork, CONFIG_FILE, 'merge = "self"\n# ours\n')
            self.ship_the_config(fork)
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "trunk_own")
            self.assertEqual(self.mode(fork), "self")

        @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                         "root reads a mode-000 file anyway")
        def test_a_gitattributes_the_history_lists_and_cannot_be_read_is_a_refusal(self):
            """FAIL CLOSED, here as everywhere else the reading can fail: a version of the
            project's `.gitattributes` this clone cannot open is not evidence that it never
            set anything. Skipped over, the one version that set `ident` on the config's
            path would be the one that is unreadable."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "self"\n')     # the fork's own, untracked
            self.assertEqual(self.mode(fork), "self")
            commit_upstream(self.tmp, ".gitattributes", "* text=auto\n", "theirs: attributes")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            blob = sh("git", "rev-parse", "upstream/main:.gitattributes", cwd=fork)
            loose = os.path.join(fork, ".git", "objects", blob[:2], blob[2:])
            self.assertTrue(os.path.exists(loose), loose)
            os.chmod(loose, 0o000)
            self.addCleanup(os.chmod, loose, 0o444)
            state = fork_config_state(ctx_for(fork))
            self.assertEqual(state[0], "unprovable")
            self.assertIn(short(blob), state[2])
            self.assertIn("cannot be read", state[2])
            self.assertEqual(self.mode(fork), "manual")

        def test_a_gitattributes_walk_git_refuses_is_a_refusal(self):
            """The other half: a walk that fails answers with the reason and never with an
            empty set. Asked of the walker directly - the same failure reaches the config
            walk first in any real clone, and a guard that only a broken repository could
            show is still the difference between "nothing set one" and "nothing was
            read"."""
            ctx = ctx_for(make_fork(self.tmp))
            found, why = attrs_render_the_config(ctx, ["refs/remotes/upstream/main",
                                                       "no-such-ref-at-all"])
            self.assertEqual(found, "")
            self.assertIn("cannot be walked", why)
            self.assertIn("merged by hand", why)

        def test_a_rendering_attribute_cannot_reach_a_config_on_the_trunk(self):
            """The rendering question asked where it cannot matter, which is where it used
            to be asked: before `fork_config_state` decided which file answers for `merge`.

            In the `trunk_*` states the config is read with `git show <origin/trunk>:<name>`
            - raw blob against raw blobs, the working tree never consulted - so nothing in
            the working tree can change the answer. A fork in the state this tool RECOMMENDS
            was nevertheless refused by any `filter`, `ident` or `working-tree-encoding` on
            the config's path, and told that "the file sitting there was written by git's
            conversion" and to `rm -- .forkflow.toml`: a fork following that deletes its own
            reviewed config for a condition that cannot change the decision."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            for attr in ("ident", "filter=mangle", "working-tree-encoding=UTF-16"):
                write(fork, ".gitattributes", CONFIG_FILE + " " + attr + "\n")
                state = fork_config_state(ctx_for(fork))
                self.assertEqual(state[0], "trunk_own", attr)
                self.assertEqual(state[2], "", attr)
                self.assertEqual(self.mode(fork), "self", attr)
                self.assertNotIn("rm -- ", fork_merge_refusal(ctx_for(fork)))
            # and the same for a symlink at the config's path: the trunk answers, not it
            os.unlink(os.path.join(fork, CONFIG_FILE))
            write(fork, "theirs-real.toml", 'merge = "self"\n')
            os.symlink("theirs-real.toml", os.path.join(fork, CONFIG_FILE))
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "trunk_own")
            self.assertEqual(self.mode(fork), "self")

        def test_the_attribute_remedy_with_no_file_on_disk_removes_nothing(self):
            """The attribute can be set on a path with no file at it - a tracked variant on
            a case-sensitive filesystem. There is then nothing converted to take away, and
            the remedy says so rather than printing an `rm` of a file that is not there.

            No config on the trunk here: with one there the trunk answers and the working
            tree is not read at all, which is what
            `test_a_rendering_attribute_cannot_reach_a_config_on_the_trunk` is about."""
            fork = make_fork(self.tmp)
            sh("git", "config", "core.ignorecase", "false", cwd=fork)
            write(fork, ".gitattributes", ".ForkFlow.toml ident\n")
            blob = sh("git", "hash-object", "-w", write(self.tmp, "v.txt", "theirs\n"),
                      cwd=fork)
            sh("git", "update-index", "--add", "--cacheinfo",
               "100644,%s,.ForkFlow.toml" % blob, cwd=fork)
            ctx = ctx_for(fork)
            why = fork_config_state(ctx)[2]
            self.assertIn("nothing to undo on disk", why)
            for cmd in re.findall(r"`([^`]+)`", why):
                self.assertFalse(cmd.startswith(("cp ", "mv ", "rm ", "test ")), cmd)
            self.assertEqual([rc for _, rc in run_every_printed(why, fork)], [0, 0])
            self.assertEqual(fork_config_state(ctx_for(fork))[2], "")

        def test_an_attribute_that_renders_the_config_is_not_judged_at_all(self):
            """`ident` expands `$Id$` into the blob's own hash on checkout, so the file in
            the working tree is not the bytes of any blob that could have produced it, and
            upstream's config - `.gitattributes` and all, brought in by a sync - read as this
            fork's own (`f9/repro1.py`). Every attribute that rewrites the file between the
            object store and the working tree is refused the same way - but only those. The
            attributes that do line endings (`text`, `eol`) and the one that does diff
            output (`diff`) are NOT here, and the test below says why."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\n# $Id$\n'
            for attr in ("ident", "filter=mangle", "working-tree-encoding=UTF-16"):
                write(fork, ".gitattributes", CONFIG_FILE + " " + attr + "\n")
                write(fork, CONFIG_FILE, theirs)
                ctx = ctx_for(fork)
                self.assertEqual(fork_config_state(ctx)[0], "unprovable", attr)
                self.assertEqual(self.mode(fork), "manual", attr)
                why = fork_config_state(ctx)[2]
                self.assertIn(attr.split("=")[0], why)          # the attribute is named
                self.assertIn(CONFIG_FILE, why)
                self.assertIn(git_path(fork, os.path.join("info", "attributes")), why)
                self.assertIn(without_stamp(why), without_stamp(fork_merge_refusal(ctx)))
            # the way out the message prints, run as printed: the attribute is turned off
            # for that one path in this clone's own attributes file (additive, nothing of
            # anybody's overwritten) AND the converted file is taken off disk. It does not
            # put a config back - `test_the_attribute_remedy_leaves_no_config_behind` is
            # about why not - so what is left is no config at all
            self.assertEqual([rc for _, rc in run_every_printed(why, fork)], [0, 0, 0])
            self.assertFalse(os.path.lexists(os.path.join(fork, CONFIG_FILE)))
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "none")
            write(fork, CONFIG_FILE, 'merge = "self"\n# ours\n')
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "untracked_own")
            self.assertEqual(self.mode(fork), "self")

        def theirs_through_an_attribute(self, fork: str, attr: str, theirs) -> bytes:
            """Upstream's `.gitattributes` and upstream's `.forkflow.toml`, checked out into
            this fork's working tree and untracked by hand - the shape a sync leaves behind,
            and the one the whole rendering question is about. Answers the raw bytes that
            landed on disk, so a test can say what git actually did to them."""
            commit_upstream(self.tmp, ".gitattributes", CONFIG_FILE + " " + attr + "\n",
                            "theirs: attributes")
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "checkout", "-q", "upstream/main", "--", ".gitattributes", cwd=fork)
            sh("git", "checkout", "-q", "upstream/main", "--", CONFIG_FILE, cwd=fork)
            sh("git", "rm", "-q", "--cached", "--ignore-unmatch", "--",
               CONFIG_FILE, ".gitattributes", cwd=fork)
            with open(os.path.join(fork, CONFIG_FILE), "rb") as fh:
                return fh.read()

        def test_line_endings_are_normalised_so_text_and_eol_are_safe_to_allow(self):
            """`text` and `eol` were refused with `filter` and `ident`, and they do not
            belong there: they change LINE ENDINGS and nothing else, and both sides of the
            provenance comparison already have their line endings taken off them
            (`working_config_text` reads in text mode, `config_fingerprint` normalises the
            blob). This is the claim the narrowing rests on, so it is measured rather than
            assumed - CRLF really reaches the working tree here, and upstream's config is
            STILL read as upstream's afterwards.

            Both sides of the comparison, one each way round: the blob stored with LF while
            `eol=crlf` puts CRLF on disk, and the blob stored with CRLF while the file on
            disk has LF."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch theirs"]\n'
            raw = self.theirs_through_an_attribute(fork, "eol=crlf", theirs)
            self.assertIn(b"\r\n", raw)                  # git really did convert it
            ctx = ctx_for(fork)
            self.assertEqual(working_config_text(ctx), theirs)    # and it reads back as LF
            self.assertEqual(fork_config_state(ctx)[0], "untracked_upstreams")
            self.assertEqual(self.mode(fork), "manual")           # still upstream's
            write(fork, CONFIG_FILE, theirs + "# ours\n")         # the way back, unchanged
            self.assertEqual(self.mode(fork), "self")

            # and the blob's own side: upstream STORED a version with CRLF in it (`-text`
            # keeps git's hands off the bytes on the way in), and this working tree holds
            # the same settings with LF. Same file, different line endings, still upstream's
            crlf = 'merge = "self"\r\ngate = ["touch crlf"]\r\n'
            commit_upstream(self.tmp, ".gitattributes", CONFIG_FILE + " -text\n",
                            "theirs: attributes, verbatim")
            commit_upstream(self.tmp, CONFIG_FILE, crlf, "theirs: forkflow with CRLF")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            stored = sh("git", "cat-file", "blob", "upstream/main:" + CONFIG_FILE, cwd=fork)
            self.assertIn("\r", stored)                  # CRLF really is what git holds
            write(fork, CONFIG_FILE, crlf.replace("\r\n", "\n"))
            ctx = ctx_for(fork)
            self.assertEqual(fork_config_state(ctx)[2], "")       # and nothing is refused
            self.assertEqual(fork_config_state(ctx)[0], "untracked_upstreams")
            self.assertEqual(self.mode(fork), "manual")
            write(fork, CONFIG_FILE, 'merge = "self"\n# ours\n')
            self.assertEqual(self.mode(fork), "self")

        def test_a_diff_attribute_never_reaches_the_working_tree(self):
            """`diff` names a diff driver, and a driver's `textconv` is run to produce DIFF
            OUTPUT - never as part of a checkout. It was in the refusing set anyway; the
            bytes say it does not belong there, and upstream's config stays upstream's."""
            theirs = 'merge = "self"\ngate = ["touch theirs"]\n'
            fork = make_fork(self.tmp)
            sh("git", "config", "diff.toml.textconv", "sed s/self/ZZZZ/", cwd=fork)
            raw = self.theirs_through_an_attribute(fork, "diff=toml", theirs)
            self.assertEqual(raw.decode(), theirs)      # the textconv changed nothing here
            ctx = ctx_for(fork)
            self.assertEqual(fork_config_state(ctx)[0], "untracked_upstreams")
            self.assertEqual(self.mode(fork), "manual")

        def test_an_ordinary_fork_is_never_refused_for_its_gitattributes(self):
            """The over-refusal this narrowing exists for. `* text=auto` is in an enormous
            number of repositories and says nothing at all about whose config this is; a
            fork carrying it used to be told `--merge` could not be proven here, with a
            remedy to write into its git directory. A plain clone with one `upstream`
            remote, its own `.forkflow.toml` on the trunk and that line in `.gitattributes`
            reaches `--merge` like any other fork."""
            fork = make_fork(self.tmp, config='merge = "self"\n# ours\n')
            for rules in ("* text=auto\n",
                          "* text=auto eol=lf\n",
                          "*.md text\n*.png binary\n" + CONFIG_FILE + " diff=toml\n",
                          CONFIG_FILE + " text eol=crlf diff\n"):
                write(fork, ".gitattributes", rules)
                ctx = ctx_for(fork)
                self.assertEqual(fork_config_state(ctx)[2], "", rules)   # nothing refused
                self.assertEqual(fork_config_state(ctx)[0], "trunk_own", rules)
                self.assertEqual(self.mode(fork), "self", rules)

        def test_an_attribute_on_a_case_variant_of_the_name_is_looked_at_too(self):
            """A case-insensitive filesystem makes `.ForkFlow.toml` and `.forkflow.toml` one
            file, so an attribute set on the variant reaches the file forkflow reads. The
            question is asked of every path that case-folds to the name - the one on disk,
            any variant beside it, and every variant git tracks - and not of the exact
            spelling alone.

            Built with plumbing and with `core.ignorecase` off, so it holds on any
            filesystem: where git has it on (the macOS and Windows default) git matches the
            attribute pattern itself case-insensitively and the refusal comes either way,
            which would make the test pass without asking about the variant at all."""
            fork = make_fork(self.tmp)
            sh("git", "config", "core.ignorecase", "false", cwd=fork)
            write(fork, ".gitattributes", ".ForkFlow.toml ident\n")
            write(fork, CONFIG_FILE, 'merge = "self"\n')
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "untracked_own")
            blob = sh("git", "hash-object", "-w", write(self.tmp, "v.txt", "theirs\n"),
                      cwd=fork)
            sh("git", "update-index", "--add", "--cacheinfo",
               "100644,%s,.ForkFlow.toml" % blob, cwd=fork)
            ctx = ctx_for(fork)
            self.assertEqual(fork_config_state(ctx)[0], "unprovable")
            self.assertIn(".ForkFlow.toml", fork_config_state(ctx)[2])

        def test_attributes_git_cannot_be_asked_about_are_a_refusal_too(self):
            """A `check-attr` that fails is not evidence that nothing is set on the file -
            a `.gitattributes` git refuses to parse is exactly where one would hide. It
            fails closed, like every other reading this answer depends on."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "self"\n')
            self.assertEqual(self.mode(fork), "self")
            real = git_rc

            def refuses(*argv, **kw):
                if argv[:1] == ("check-attr",):
                    return (128, "", "fatal: bad attributes line\n")
                return real(*argv, **kw)

            with mock.patch.object(sys.modules[__name__], "git_rc", refuses):
                ctx = ctx_for(fork)
                self.assertEqual(fork_config_state(ctx)[0], "unprovable")
                self.assertIn("bad attributes line", fork_config_state(ctx)[2])
                self.assertEqual(fork_merge_mode(ctx), "manual")

        def test_attributes_that_say_nothing_about_the_config_change_nothing(self):
            """The refusals above must never reach an ordinary fork. A `.gitattributes` that
            covers other paths, and one that turns the attributes OFF for this one, both
            leave the config exactly as readable as it was."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            self.assertEqual(self.mode(fork), "self")
            write(fork, ".gitattributes",
                  "*.md text\n*.png binary\nsrc/** diff=python\n" + CONFIG_FILE + " -text\n")
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "trunk_own")
            self.assertEqual(self.mode(fork), "self")

        def withdraw_and_replace(self, gone: str, instead: str) -> None:
            """Upstream rewrites its branch: the commit that carried `gone` is not in the
            history any more and `instead` is what the branch carries now. A force-push, a
            withdrawn branch and a pruned fetch all leave this behind - a history with every
            other version of upstream's in it and not that one."""
            seed = os.path.join(self.tmp, "seed")
            sh("git", "reset", "-q", "--hard", "HEAD~1", cwd=seed)
            write(seed, CONFIG_FILE, instead)
            sh("git", "add", "-A", cwd=seed)
            sh("git", "commit", "-q", "-m", "theirs: forkflow, rewritten", cwd=seed)
            sh("git", "push", "-q", "--force", "origin", "main", cwd=seed)
            self.assertNotEqual(gone, instead)

        def test_a_version_upstream_withdrew_from_every_ref_is_still_upstreams(self):
            """You cannot prove absence from a history you no longer hold. Upstream's
            `.forkflow.toml`, brought in by a sync and untracked by hand, is refused while
            upstream still carries those bytes - and upstream then FORCE-PUSHES that commit
            away and puts another version in its place. The walk now reads a history holding
            every version of upstream's but that one, so the answer is not empty, no
            fail-closed condition fires, and a file this fork never wrote read as the fork's
            own - with upstream's `merge = "self"` in it, and upstream's `gate` behind it.

            So the fork REMEMBERS, in the one place upstream cannot write: what was once the
            original project's stays the original project's, and the withdrawal changes
            nothing. The way back is the one every refusal here prints - edit the file, and
            edited bytes are new bytes that no memory holds."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = ["touch theirs"]\n'
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            self.advance_mirror(fork)
            write(fork, CONFIG_FILE, theirs)     # a sync brought it in; untracked by hand
            self.assertEqual(self.mode(fork), "manual")
            self.assertEqual(remembered_upstream_configs(ctx_for(fork)),
                             [config_digest(theirs)])       # written down while it was there

            self.withdraw_and_replace(theirs, theirs + "# theirs, rewritten\n")
            sh("git", "fetch", "-q", "--prune", "upstream", cwd=fork)
            sh("git", "branch", "-f", "main", "upstream/main", cwd=fork)
            sh("git", "push", "-q", "--force", "origin", "main:refs/heads/main", cwd=fork)
            sh("git", "fetch", "-q", "--prune", "origin", cwd=fork)
            ctx = ctx_for(fork)
            walked, why = config_versions_in(ctx, upstream_scope_refs(ctx))
            self.assertEqual(why, "")
            self.assertTrue(walked)                         # the baseline is NOT empty
            self.assertNotIn(config_digest(theirs), walked)  # and no history holds these

            self.assertEqual(self.mode(fork), "manual")     # remembered, so still upstream's

            # and the memory cannot be flushed into an open gate. Damage the file it lives
            # in - a truncated write, a full disk, a hand edit - and the answer used to be
            # `{}`: the record simply gone, in silence, and upstream's own file read as this
            # fork's own with `merge = "self"` in it (`f10/repro15.py`)
            shared = state_path(ctx, shared=True)
            with open(shared) as fh:
                whole = fh.read()
            with open(shared, "w") as fh:
                fh.write(whole[:len(whole) // 2])
            self.assertEqual(fork_config_state(ctx_for(fork))[0], "unprovable")
            self.assertEqual(self.mode(fork), "manual")
            self.assertIn(shared, fork_config_state(ctx_for(fork))[2])
            with open(shared, "w") as fh:                   # put it back as it was
                fh.write(whole)

            write(fork, CONFIG_FILE, theirs + "# ours\n")
            self.assertEqual(self.mode(fork), "self")       # an edit of the fork's own

        def test_what_is_written_down_comes_only_from_the_refs_upstream_cannot_choose(self):
            """The memory is permanent, so what goes into it is taken from the frame upstream
            cannot choose (`foreign_remote_refs`) and from nowhere else. The refs the CONFIG
            names widen the answer for the run that reads them and are left out on purpose:
            a config aiming the walk at this fork's own trunk would otherwise write the
            fork's own bytes in for good, and no later edit of the config could take them
            out again.

            Here the config on the trunk calls a branch of the FORK's the `mirror`, and that
            branch carries the fork's own config, so the walk reads the fork's own bytes as
            upstream's and refuses - which is the safe direction, and is what the superset
            scope is for. What must not happen is the refusal outliving the config that
            caused it: the fork edits the file, and the gate opens again."""
            config = 'merge = "self"\nmirror = "ours"\n'
            fork = make_fork(self.tmp, config=config)
            sh("git", "push", "-q", "origin", "develop:refs/heads/ours", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            ctx = ctx_for(fork)
            self.assertIn("refs/remotes/origin/ours", upstream_scope_refs(ctx))
            self.assertEqual(fork_config_state(ctx)[0], "trunk_upstreams")
            self.assertEqual(self.mode(fork), "manual")     # it aimed the walk at the fork
            self.assertNotIn(config_digest(config), remembered_upstream_configs(ctx))
            second_clone_commit(self.tmp, path=CONFIG_FILE, content='merge = "self"\n')
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(self.mode(fork), "self")       # nothing was written down about it

        def blank_upstream_branch(self, name: str = "blank") -> None:
            """A branch of the original project's own that never carried a config - the ref
            upstream points the walk at when it gets to choose the walk's scope."""
            seed = os.path.join(self.tmp, "seed")
            sh("git", "checkout", "-q", "--orphan", name, cwd=seed)
            sh("git", "rm", "-q", "-rf", ".", cwd=seed)
            write(seed, "BLANK.md", "nothing here\n")
            sh("git", "add", "-A", cwd=seed)
            sh("git", "commit", "-q", "-m", "theirs: a branch with no config in it", cwd=seed)
            sh("git", "push", "-q", "origin", name, cwd=seed)
            sh("git", "checkout", "-q", "main", cwd=seed)

        def test_upstream_does_not_choose_the_refs_the_walk_reads(self):
            """The seventh route into the gate: the provenance walk's own SCOPE was read
            off the `.forkflow.toml` whose provenance it decides. Upstream's file names an
            `upstream_branch` of upstream's that never carried a config and swaps `trunk`
            and `mirror`, so every ref the old walk read - `<upstream>/<branch>` and the
            mirror - held no config at all. The set came back empty, upstream's own bytes
            matched nothing, and the config sitting on `origin/main` (the MIRROR, a pristine
            copy of upstream) was read as this fork's reviewed declaration: `merge = "self"`,
            gate open, upstream's `gate` run here as shell.

            `upstream_scope_refs` takes the scope from every remote-tracking ref that is not
            `origin`'s instead - remotes are local git config, which upstream cannot write -
            so `upstream/main` is walked however the config is spelled."""
            fork = make_fork(self.tmp)
            self.blank_upstream_branch()
            theirs = ('merge = "self"\ngate = ["touch theirs"]\n'
                      'upstream_branch = "blank"\ntrunk = "main"\nmirror = "develop"\n')
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            self.advance_mirror(fork)
            write(fork, CONFIG_FILE, theirs)     # a sync brought it in; untracked by hand
            ctx = ctx_for(fork)
            self.assertEqual((ctx.upstream_branch, ctx.trunk, ctx.mirror),
                             ("blank", "main", "develop"))      # the config got its way here
            self.assertIn("refs/remotes/upstream/main", upstream_scope_refs(ctx))
            # the file read for `merge` is the one the renamed "trunk" carries - which is
            # the MIRROR, upstream's own pristine copy, and the walk says so
            self.assertEqual(fork_config_state(ctx)[0], "trunk_upstreams")
            self.assertEqual(self.mode(fork), "manual")

        def test_the_walk_reads_a_remote_the_config_never_names(self):
            """The scope is EVERY non-origin remote-tracking ref, not the one the config
            calls `upstream`: a second remote for the same project - a mirror of it, a
            colleague's fork of it - carries upstream's configs too, and a config pointing
            `upstream` at a remote with nothing fetched under it must not make them this
            fork's. `upstream/main` is deleted here and the mirror with it, so only the
            other remote's refs can answer - and they do."""
            fork = make_fork(self.tmp)
            theirs = ('merge = "self"\nupstream = "upstream"\n'
                      '# theirs, fetched under another remote\n')
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            sh("git", "remote", "add", "elsewhere", os.path.join(self.tmp, "upstream.git"),
               cwd=fork)
            sh("git", "fetch", "-q", "elsewhere", cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=fork)
            sh("git", "branch", "-q", "-D", "main", cwd=fork)    # the mirror is not advanced
            write(fork, CONFIG_FILE, theirs)
            ctx = ctx_for(fork)
            self.assertEqual(ctx.upstream, "upstream")           # the config named that one
            self.assertEqual([r for r in foreign_remote_refs(ctx) if r.endswith("/main")],
                             ["refs/remotes/elsewhere/main"])
            self.assertNotIn("theirs", sh("git", "show", "origin/main:" + CONFIG_FILE,
                                          cwd=fork, check=False))
            self.assertEqual(self.mode(fork), "manual")

        def test_a_version_upstream_only_ever_had_on_a_side_branch_is_upstreams(self):
            """`--full-history`, not the walk's default. A config upstream carried on a
            branch it merged keeping its own side (`-s ours`) is TREESAME to the first
            parent, so the simplified walk follows that parent alone and never sees the
            file at all. Upstream had those bytes and published them; a fork can hold them
            without having written a character of them."""
            fork = make_fork(self.tmp)
            seed = os.path.join(self.tmp, "seed")
            side = 'merge = "self"\n# theirs, on a branch\n'
            commit_upstream(self.tmp, "src/app.py", "theirs\n", "theirs: app")
            sh("git", "checkout", "-q", "-b", "theirs-side", cwd=seed)
            write(seed, CONFIG_FILE, side)
            sh("git", "add", "-A", cwd=seed)
            sh("git", "commit", "-q", "-m", "theirs: a config on a side branch", cwd=seed)
            sh("git", "checkout", "-q", "main", cwd=seed)
            sh("git", "merge", "-q", "-s", "ours", "--no-edit", "theirs-side", cwd=seed)
            sh("git", "push", "-q", "origin", "main", cwd=seed)
            self.advance_mirror(fork)
            self.tips_lack(fork, side)
            self.assertEqual(sh("git", "rev-list", "upstream/main", "--", CONFIG_FILE,
                                cwd=fork, check=False), "")      # the simplified walk
            write(fork, CONFIG_FILE, side)
            self.assertEqual(self.mode(fork), "manual")

    class TestMergeModeAskedAgainBeforeTheMerge(MergeBase):
        """The gate reads `origin/<trunk>` before the run's own fetch; `merge_mr` asks
        `fork_merge_mode` again right before the merge command. A teammate's commit that
        turned the fork "manual" is on origin but not fetched yet: the gate passes on the
        stale ref, and the run must end with the request open and nothing merged - exit 6,
        the record kept for `land`."""

        def stale_self_fork(self) -> Tuple[str, str]:
            fork = self.self_fork()
            turned = second_clone_commit(self.tmp, path=CONFIG_FILE,
                                         content='merge = "manual"\n')
            self.assertEqual(sh("git", "show", "origin/develop:" + CONFIG_FILE, cwd=fork),
                             'merge = "self"')                     # what the gate reads
            return fork, turned

        def not_merged(self, fork: str, branch: str, turned: str, code: int, out: str) -> None:
            self.assertEqual(code, EXIT_NOT_MERGED, out)
            self.assertEqual(self.tool_argv_for("gitlab", "merge"), [])
            self.assertEqual(self.tool_argv_for("gitlab", "create")[:2], ["mr", "create"])
            self.assertEqual(origin_sha(fork, "develop"), turned)
            self.assertEqual(origin_sha(fork, branch), rev(fork, "refs/heads/" + branch))
            self.assertEqual(self.pending_of(fork, branch)["commit"],
                             rev(fork, "refs/heads/" + branch))

        def test_a_sync(self):
            fork, turned = self.stale_self_fork()
            self.upstream_change()
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.not_merged(fork, sync_branch_name(), turned, code, err + out)

        def test_a_ship(self):
            fork, turned = self.stale_self_fork()
            name = self.feature(fork)
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.not_merged(fork, name, turned, code, err + out)

        def test_the_fetch_can_make_the_untracked_config_upstreams(self):
            """What upstream's configs ARE is read again too, not only what the fork's file
            says. The untracked `.forkflow.toml` here holds bytes no ref in this clone has,
            so the gate reads it as this fork's own - and the run's own fetch brings the
            mirror onto the upstream commit that carries exactly those bytes. Kept from the
            first read, the set would still lack them and the request would be merged on
            upstream's word; read again, the file is upstream's and nothing this fork wrote
            says `merge = "self"`."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\n# copied out of the project\n'
            write(fork, CONFIG_FILE, theirs)
            self.assertEqual(fork_merge_mode(ctx_for(fork)), MERGE_SELF)
            second_clone_commit(self.tmp, branch="main", path=CONFIG_FILE, content=theirs)
            base = origin_sha(fork, "develop")
            name = self.feature(fork, only_own=True)
            with on_platform(self.platform("gitlab")):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, EXIT_NOT_MERGED, err + out)
            self.assertEqual(self.tool_argv_for("gitlab", "merge"), [])
            self.assertEqual(self.tool_argv_for("gitlab", "create")[:2], ["mr", "create"])
            self.assertEqual(origin_sha(fork, "develop"), base)        # the trunk is untouched
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            self.assertEqual(self.pending_of(fork, name)["commit"],
                             rev(fork, "refs/heads/" + name))

    class TestMergeGate(ShipBase):
        """`--merge` is config AND flag, refused before the fetch, the backup and any push.

        A reviewed fork must never be merged by accident: the flag alone does nothing, and
        the refusal comes before anything the run would have to undo - `origin/develop`, the
        mirror, the backup branches, the sync branch, the feature branch on origin and the
        state file are all exactly as they were, and the platform tool never ran. Each
        refusal runs with the origin *named* (`on_platform`) and a merging tool on PATH, so
        only the condition under test can be what refuses it: without the config condition
        the run would push, open and merge."""

        REFUSED = "--merge needs"
        UNNAMED = "names no project"
        ARRIVED = "Resume without it"

        def feature(self, fork: str, name: str = "feat/x", commits: int = 1,
                    push: bool = False) -> str:
            """Own files only: an untracked `.forkflow.toml` must not be committed by the
            fixture of a test about the untracked config."""
            return ShipBase.feature(self, fork, name, commits, push, only_own=True)

        def work_state(self, fork: str) -> Optional[dict]:
            """The state file with the provenance memory taken out - what a run RECORDED,
            which is what a refused run has to leave exactly as it found it. None when there
            is no state file at all.

            `upstream_configs` is not a record of work: it is what this clone has written
            down of the original project's `.forkflow.toml`s (`remember_upstream_configs`),
            and a run that asks a provenance question writes down what it read whether it
            then goes on or refuses. That is the whole point of it - what was once
            upstream's stays upstream's, and a refusal is exactly when it matters."""
            path = git_path(fork, STATE_FILE)
            if not os.path.exists(path):
                return None
            with open(path) as fh:
                raw = fh.read()
            try:
                data = json.loads(raw)
            except ValueError:
                return {"unreadable": raw}
            if isinstance(data, dict):
                data.pop("upstream_configs", None)
                return data or None          # a file holding only the memory is no record
            return {"not an object": raw}

        def snapshot(self, fork: str) -> tuple:
            """Everything a run could have changed: every ref on origin and in the clone,
            and every record in the state file."""
            return (sh("git", "ls-remote", "origin", cwd=fork),
                    sh("git", "for-each-ref", "--format=%(refname) %(objectname)", cwd=fork),
                    self.work_state(fork))

        def refused(self, fork: str, *argv: str, why: str = REFUSED, named: bool = True) -> str:
            """Exit 2 naming `why`, nothing changed and no platform tool run; answers with
            the message, for the route it names."""
            merging_tool(self.tmp, "glab")
            before = self.snapshot(fork)
            if named:
                with on_platform("gitlab"):
                    code, out, err = run("-C", fork, *argv)
            else:
                code, out, err = run("-C", fork, *argv)
            self.assertEqual(code, 2, " ".join(argv) + ": " + err + out)
            self.assertIn(why, err)
            self.assertEqual(self.snapshot(fork), before)
            self.assertEqual(tool_argv(self.tmp, "glab", "create"), [])
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])
            return err

        def both_refused(self, fork: str, why: str = REFUSED, named: bool = True) -> None:
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            self.refused(fork, "sync", "--merge", why=why, named=named)
            self.assertEqual(sh("git", "ls-remote", "--heads", "origin",
                                "refs/heads/" + DEFAULT_BACKUP_PREFIX + "*", cwd=fork), "")
            self.assertFalse(self.work_state(fork))      # no record of work of any kind
            self.feature(fork)
            self.refused(fork, "ship", "--merge", why=why, named=named)
            self.assertEqual(origin_sha(fork, "feat/x"), "")
            self.assertFalse(self.work_state(fork))

        @needs_tomllib
        def test_refused_on_a_manual_fork_with_the_config_committed(self):
            self.both_refused(make_fork(self.tmp, config='merge = "manual"\n'))

        @needs_tomllib
        def test_refused_on_a_manual_fork_with_the_config_untracked(self):
            """`setup` leaves `.forkflow.toml` untracked, and `load_config` reads the
            working tree: the gate reads it there too - it stays untracked throughout (the
            feature commit stages its own file only). That the untracked file is read at
            all is `test_passes_with_merge_self_untracked_as_setup_leaves_it`."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "manual"\n')
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            self.both_refused(fork)
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))

        SELF = 'merge = "self"\n'

        def self_fork(self) -> str:
            """A fork whose own reviewed config says `merge = "self"`: everything but the
            condition under test would let `--merge` through."""
            fork = make_fork(self.tmp, config=self.SELF)
            self.assertEqual(fork_merge_mode(ctx_for(fork)), MERGE_SELF)
            self.feature(fork)            # `ship` refuses the trunk before the gate is read
            return fork

        @needs_tomllib
        def test_refused_on_a_shallow_clone(self):
            """Fail CLOSED: a version the walk cannot see is not a version upstream never
            had. A shallow clone simply does not hold the commits before its cut, so
            upstream's older `.forkflow.toml`s are missing from the comparison set and
            upstream's own file reads as this fork's. The refusal names the condition and
            the command that ends it."""
            fork = self.self_fork()
            sh("git", "fetch", "-q", "--depth=1", "upstream", "main", cwd=fork)
            self.assertEqual(sh("git", "rev-parse", "--is-shallow-repository", cwd=fork),
                             "true")
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("shallow", err)
            self.assertIn("git fetch --unshallow upstream", err)
            self.assertIn("merged by hand", err)

        @needs_tomllib
        def test_refused_when_history_is_rewritten_by_a_replace_ref(self):
            """`refs/replace/*` makes git answer with a history that is not the one the
            original project published - which is the one the set has to be taken from."""
            fork = self.self_fork()
            head = rev(fork, "refs/remotes/upstream/main")
            sh("git", "replace", "--graft", head, cwd=fork)       # the tip, with no parent
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("refs/replace/", err)
            self.assertIn("git replace -l", err)

        @needs_tomllib
        def test_refused_on_a_partial_clone(self):
            """A promisor remote means the blobs are fetched on demand: a `.forkflow.toml`
            upstream had can be absent here, and absence is what reads as "never had it"."""
            fork = self.self_fork()
            sh("git", "config", "remote.upstream.promisor", "true", cwd=fork)
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("partial", err)
            # the setting itself, so the user can see what the tool saw and unset it
            self.assertIn("remote.upstream.promisor=true", err)

        def upstream_ref_with_two_config_versions(self, fork: str) -> tuple:
            """A ref of the original project's carrying two versions of the config, one
            behind the other: (the tip, the older commit, the older blob). Built as real
            objects in this clone so that removing one of them is git's own answer about a
            missing object and not a mocked one."""
            sh("git", "checkout", "-q", "-b", "tmp/theirs", cwd=fork)
            write(fork, CONFIG_FILE, 'merge = "self"\n# the version they retired\n')
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "-m", "a version upstream had", cwd=fork)
            older = rev(fork, "HEAD")
            blob = sh("git", "rev-parse", older + ":" + CONFIG_FILE, cwd=fork)
            write(fork, CONFIG_FILE, 'merge = "self"\n# the version they have now\n')
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "-m", "and the one after it", cwd=fork)
            tip = rev(fork, "HEAD")
            sh("git", "update-ref", "refs/remotes/upstream/theirs", tip, cwd=fork)
            sh("git", "checkout", "-q", "feat/x", cwd=fork)
            sh("git", "branch", "-q", "-D", "tmp/theirs", cwd=fork)
            return (tip, older, blob)

        def remove_object(self, fork: str, sha: str) -> None:
            loose = os.path.join(fork, ".git", "objects", sha[:2], sha[2:])
            self.assertTrue(os.path.exists(loose), loose)
            os.remove(loose)

        @needs_tomllib
        def test_refused_when_a_config_at_a_tip_cannot_be_read(self):
            """The object is gone from this clone, and a ref names the tree that holds it:
            reading that as "upstream never had these bytes" is the open gate."""
            fork = self.self_fork()
            tip, _, _ = self.upstream_ref_with_two_config_versions(fork)
            self.remove_object(fork, sh("git", "rev-parse", tip + ":" + CONFIG_FILE,
                                        cwd=fork))
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("cannot be read", err)
            self.assertIn("merged by hand", err)

        @needs_tomllib
        @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                         "root reads a mode-000 file anyway")
        def test_refused_when_a_config_the_history_lists_cannot_be_read(self):
            """Not the tip - a version upstream has since replaced, which only the walk
            lists. The tips all read fine, `rev-list` names the blob from the tree that
            holds it without opening it, and `cat-file` then cannot produce it: the set
            would silently be one version short, and that version is exactly the one a fork
            ends up holding after a sync it never landed. Unreadable rather than gone,
            because a missing object is what `rev-list` itself refuses - this is the half
            of it that gets past the walk."""
            fork = self.self_fork()
            _, _, blob = self.upstream_ref_with_two_config_versions(fork)
            loose = os.path.join(fork, ".git", "objects", blob[:2], blob[2:])
            self.assertTrue(os.path.exists(loose), loose)
            os.chmod(loose, 0o000)
            self.addCleanup(os.chmod, loose, 0o444)
            err = self.refused(fork, "ship", "--merge")
            self.assertIn(short(blob), err)
            self.assertIn("cannot be read", err)

        @needs_tomllib
        def test_refused_when_the_history_cannot_be_walked_at_all(self):
            """A commit missing from the middle: the tips still read, and `rev-list` fails.
            An empty answer from a walk that failed is not "no config was ever here"."""
            fork = self.self_fork()
            tip, older, _ = self.upstream_ref_with_two_config_versions(fork)
            self.remove_object(fork, older)
            self.assertTrue(sh("git", "show", tip + ":" + CONFIG_FILE, cwd=fork))
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("cannot be walked", err)
            self.assertIn("merged by hand", err)

        @needs_tomllib
        def test_refused_when_this_clone_holds_nothing_of_upstreams(self):
            """Nothing outside `origin` is fetched here, so there is no history of the
            original project's to compare a config against - and an empty set says every
            file is this fork's own."""
            fork = self.self_fork()
            sh("git", "branch", "-q", "-D", "main", cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=fork)
            self.assertEqual(foreign_remote_refs(ctx_for(fork)), [])   # HEAD is broken now
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("no remote-tracking ref outside `origin`", err)
            self.assertIn("git fetch upstream", err)

        def test_refused_with_no_config_at_all(self):
            """No file means "manual": the default is the safe side."""
            self.both_refused(make_fork(self.tmp))

        def test_refused_on_a_real_conflicted_sync_continue(self):
            """`cmd_sync` dispatches `--continue` before any preflight, and a conflicted sync
            is exactly when `--continue` is used: the gate sits in front of it. A real
            conflicted sync, resolved, so nothing but the gate stops the resumed run."""
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                            "theirs: shared")
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            self.refused(fork, "sync", "--continue", "--merge")
            self.assertEqual(origin_sha(fork, sync_branch_name()), "")    # nothing pushed

        def test_refused_on_a_real_stopped_ship_continue(self):
            """A real ship stopped on a rebase conflict, resolved and continued by hand, so
            `ship --continue` has a ship of ours to resume and only the gate stops it."""
            fork, name = self.conflicting_ship()
            self.assertEqual(run("-C", fork, "ship")[0], 4)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            sh("git", "rebase", "--continue", cwd=fork)
            self.assertFalse(rebase_in_progress(ctx_for(fork, strict_mirror=False)))
            self.refused(fork, "ship", "--continue", "--merge")
            self.assertEqual(origin_sha(fork, name), "")                   # nothing pushed

        @needs_tomllib
        def test_refused_when_merge_self_arrived_with_the_sync(self):
            """The fork has no config ("manual"); upstream's `.forkflow.toml` says `merge =
            "self"` and comes in with a conflicted sync. On `--continue` the working tree is
            the merge, upstream's file included - that must not switch the gate off, before
            the merge is committed or after. `--continue --mr`, the route the refusal names,
            still finishes the sync, opens the request and merges nothing."""
            fork = make_fork(self.tmp)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            commit_upstream(self.tmp, CONFIG_FILE, 'merge = "self"\n', "theirs: forkflow")
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                            "theirs: shared")
            with on_platform("gitlab"):
                self.assertEqual(run("-C", fork, "sync")[0], 4)
            with open(os.path.join(fork, CONFIG_FILE)) as fh:
                self.assertIn('merge = "self"', fh.read())               # upstream's, in the tree
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            self.refused(fork, "sync", "--continue", "--merge", why=self.ARRIVED)
            sh("git", "commit", "-q", "--no-edit", cwd=fork)             # committed by hand
            err = self.refused(fork, "sync", "--continue", "--merge", why=self.ARRIVED)
            name = sync_branch_name()
            self.assertEqual(origin_sha(fork, name), "")
            with on_platform("gitlab"):
                code, out, err = run_printed(err, "forkflow sync --continue", fork)
            self.assertEqual(code, 0, err + out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])
            # still `--mr`: the request the reviewer merges by hand is opened
            self.assertEqual(tool_argv(self.tmp, "glab", "create")[:2], ["mr", "create"])

        @needs_tomllib
        def test_refused_on_a_sync_branch_left_carrying_upstreams_merge_self(self):
            """The fork has no config ("manual"); upstream's `.forkflow.toml` says `merge =
            "self"`. `sync --mr` leaves the user on the sync branch, whose tree holds
            upstream's file: `sync --merge` from there - and `sync --force --merge`, which the
            old refusal printed and which published `<name>-2` and merged it - are both
            refused before anything is pushed, and origin's trunk never gets upstream's
            config unreviewed."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, CONFIG_FILE, 'merge = "self"\n', "theirs: forkflow")
            merging_tool(self.tmp, "glab")
            trunk = origin_sha(fork, "develop")
            with on_platform("gitlab"):
                self.assertEqual(run("-C", fork, "sync", "--mr")[0], 0)
            name = sync_branch_name()
            self.assertEqual(checked_out(fork), name)
            with open(os.path.join(fork, CONFIG_FILE)) as fh:
                self.assertIn('merge = "self"', fh.read())               # upstream's, in the tree
            os.unlink(os.path.join(self.tmp, "glab-create-argv.txt"))   # that request is open
            next_utc_second()
            self.refused(fork, "sync", "--merge")
            self.refused(fork, "sync", "--force", "--merge")
            self.assertEqual(origin_sha(fork, name + "-2"), "")
            self.assertEqual(origin_sha(fork, "develop"), trunk)

        @needs_tomllib
        def test_refused_when_upstreams_config_sits_untracked_in_the_working_tree(self):
            """This fork chose no `merge`. A sync brought upstream's `.forkflow.toml` in and
            the user untracked it by hand, leaving upstream's bytes on disk in the state
            `setup` leaves this fork's template in: `ship --merge` merged and landed on
            upstream's `merge = "self"`, and the ship's `check` ran upstream's `gate` on the
            way, with no merge request merged by anyone. Refused on the bytes - and one
            character of this fork's own makes the file, and the gate, this fork's."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = []\n'
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            write(fork, CONFIG_FILE, theirs)
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            name = self.feature(fork)
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("byte for byte", err)
            self.assertEqual(origin_sha(fork, name), "")
            write(fork, CONFIG_FILE, theirs + "# ours\n")        # this fork's own, now
            merging_tool(self.tmp, "glab")
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(tool_argv(self.tmp, "glab", "merge")[:3], ["mr", "merge", name])

        @needs_tomllib
        def test_the_refusal_over_upstreams_config_says_whose_it_is_and_names_the_way_back(self):
            """A sync whose `.forkflow.toml` conflict a person resolved by taking upstream's
            text leaves upstream's bytes on the trunk. `--merge` is refused - they are not
            this fork's word - but the message said only that this fork's own config does
            not say `merge = "self"` about a file that plainly reads `merge = "self"`, and
            named nothing to do about it. It now says whose file it is, and the way back it
            prints - edit it in your fork and ship it - works."""
            ours = 'merge = "self"\ngate = []\n'
            theirs = 'merge = "self"\n# the project\'s own notes\n'
            fork = make_fork(self.tmp, config=ours)
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "merge", "upstream/main", cwd=fork, check=False)   # conflicts on it
            write(fork, CONFIG_FILE, theirs)                             # upstream's side
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "--no-edit", cwd=fork)
            sh("git", "push", "-q", "origin", "develop", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(sh("git", "show", "origin/develop:" + CONFIG_FILE, cwd=fork),
                             theirs.strip())                     # what the user can read
            self.feature(fork)
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("byte for byte", err)
            self.assertIn("`%s`" % rerun_cmd("ship", argparse.Namespace(mr=True)), err)
            # the way back, followed: an edit of this fork's own, on a branch, merged
            sh("git", "checkout", "-q", "-b", "cfg", "origin/develop", cwd=fork)
            commit_fork(fork, CONFIG_FILE, theirs + "# ours\n", "ours: our own config")
            sh("git", "push", "-q", "origin", "cfg:refs/heads/develop", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            self.assertEqual(fork_merge_mode(ctx_for(fork)), "self")

        @needs_tomllib
        def test_refused_when_the_origin_names_no_project(self):
            """Knowable at gate time, so not a push followed by a failed merge: the fixture's
            local-path origin is on no platform and cannot be given to `--repo`."""
            self.both_refused(make_fork(self.tmp, config='merge = "self"\n'), why=self.UNNAMED,
                              named=False)

        @needs_tomllib
        def test_refused_for_a_branch_that_reads_as_a_request_number(self):
            """The merge is addressed by branch name, and `glab mr merge 123` reads `123`
            as merge request !123 - refused before anything is pushed."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            self.feature(fork, name="123")
            self.refused(fork, "ship", "--merge", why="reads as a merge request number")
            self.assertEqual(origin_sha(fork, "123"), "")

        @needs_tomllib
        def test_the_ship_that_first_commits_the_config_is_told_which_flag_to_use(self):
            """`setup` leaves `.forkflow.toml` untracked and `--merge` reads it there. The
            one ship that first COMMITS it is refused - on a branch the file is neither
            untracked nor on `origin/<trunk>`, so nothing says "self" until a person has
            merged it - and that refusal named no command at all: no `forkflow ship --mr`,
            no `then:` line, and the user had to work the flag out alone. The `in_the_way`
            path names it; this one does too, and what it names, run as printed, works."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "self"\n')
            sh("git", "checkout", "-q", "-b", "cfg", "develop", cwd=fork)
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "-m", "ours: track our config", cwd=fork)
            err = self.refused(fork, "ship", "--merge")
            self.assertIn("`%s`" % rerun_cmd("ship", argparse.Namespace(mr=True)), err)
            self.assertIn("then: forkflow land", err)
            with on_platform("gitlab"):
                code, out, err2 = run_printed(err, "forkflow ship", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assertEqual(tool_argv(self.tmp, "glab", "create")[:2], ["mr", "create"])
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])   # merged by hand

        @needs_tomllib
        def test_passes_on_a_self_fork_whose_origin_is_named(self):
            """Both conditions met: the run goes on (a dry run here, which pushes nothing
            and reaches the merge-request step)."""
            fork = make_fork(self.tmp, config='merge = "self"\n')
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            sh("git", "fetch", "-q", "upstream", cwd=fork)   # the dry run does not fetch, and
            with on_platform("gitlab"):                   # will not conclude from a stale ref
                code, out, err = run("-C", fork, "sync", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn(self.REFUSED, err)
            self.feature(fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--merge", "--dry-run")
            self.assertEqual(code, 0, err + out)
            self.assertNotIn(self.REFUSED, err)
            self.assertIn("not run (dry run)", out)              # `--merge` implied `--mr`

        @needs_tomllib
        def test_passes_with_merge_self_untracked_as_setup_leaves_it(self):
            """The ordinary solo fork after `setup`: `merge = "self"` in an UNTRACKED
            `.forkflow.toml`. The gate passes on the file as it is - not only on a committed
            one - and the run merges and lands, the file still untracked afterwards."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, 'merge = "self"\n')
            name = self.feature(fork)
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            merging_tool(self.tmp, "glab")
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "ship", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(tool_argv(self.tmp, "glab", "merge")[:3], ["mr", "merge", name])
            self.assertEqual(origin_sha(fork, "develop"), rev(fork, "refs/heads/develop"))
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(self.pending_of(fork), {})                    # landed, forgotten
            self.assertIn("?? " + CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            # and the sync: upstream tracks no config, so the untracked file is not in the way
            commit_upstream(self.tmp, "docs/theirs.md", "theirs\n", "theirs: docs")
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertEqual(tool_argv(self.tmp, "glab", "merge")[:3],
                             ["mr", "merge", sync_branch_name()])
            self.assertEqual(origin_sha(fork, "develop"), rev(fork, "refs/heads/develop"))
            self.assertEqual(self.pending_of(fork), {})

    def case_insensitive(path: str) -> bool:
        """Whether the filesystem under `path` opens a file under another case of its name."""
        probe = os.path.join(path, "forkflow-case-probe")
        with open(probe, "w"):
            pass
        try:
            return os.path.exists(os.path.join(path, "FORKFLOW-CASE-PROBE"))
        finally:
            os.unlink(probe)

    class TestConfigNameCase(ShipBase):
        """`.forkflow.toml` under another case of its name.

        On a case-insensitive filesystem (the macOS and Windows default) an upstream that
        commits `.ForkFlow.toml` puts a file in the tree that `open(".forkflow.toml")` reads
        and git's case-exact `<rev>:.forkflow.toml` and `ls-files` do not see: every check of
        what the sync merge brought in read "no config on either side", and upstream's `gate`
        ran with `sh -c` and its `merge = "self"` merged the sync MR. The first two cases hold
        on any filesystem; the end-to-end ones need a case-insensitive one and skip
        elsewhere."""

        VARIANT = ".ForkFlow.toml"

        def upstream_variant(self, flag: str) -> None:
            commit_upstream(self.tmp, self.VARIANT,
                            'merge = "self"\ngate = ["touch %s"]\n' % flag,
                            "theirs: forkflow, spelled differently")

        def test_check_refuses_a_variant_in_the_tree_and_runs_no_gate(self):
            fork = make_fork(self.tmp)
            flag = os.path.join(self.tmp, "variant-gate-ran")
            write(fork, self.VARIANT, 'gate = ["touch %s"]\n' % flag)
            self.assertNotIn(CONFIG_FILE, os.listdir(fork))
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 2, err + out)
            self.assertIn("`%s`" % self.VARIANT, err)
            self.assertFalse(os.path.exists(flag))

        def test_a_committed_variant_counts_as_the_config(self):
            """What "is the config tracked" and "what did this revision carry" answer for a
            variant - built with plumbing, so no working-tree file is involved and it holds on
            any filesystem. The exact name wins where a tree holds both."""
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork)
            self.assertFalse(tracked_config_names(fork))
            self.assertIsNone(config_text(ctx, "HEAD"))

            def blob(text: str) -> str:
                path = write(self.tmp, "blob.txt", text)
                return sh("git", "hash-object", "-w", path, cwd=fork)

            sh("git", "update-index", "--add", "--cacheinfo",
               "100644,%s,%s" % (blob("theirs\n"), self.VARIANT), cwd=fork)
            self.assertTrue(tracked_config_names(fork))
            one = sh("git", "commit-tree", sh("git", "write-tree", cwd=fork), "-p", "HEAD",
                     "-m", "variant only", cwd=fork)
            self.assertEqual(config_text(ctx, one), "theirs\n")
            sh("git", "update-index", "--add", "--cacheinfo",
               "100644,%s,%s" % (blob("ours\n"), CONFIG_FILE), cwd=fork)
            both = sh("git", "commit-tree", sh("git", "write-tree", cwd=fork), "-p", "HEAD",
                      "-m", "both", cwd=fork)
            self.assertEqual(config_text(ctx, both), "ours\n")
            self.assertIsNone(config_text(ctx, "HEAD"))                   # still none there

        @needs_tomllib
        def test_a_clean_sync_bringing_a_variant_runs_no_gate(self):
            """A plain `sync` on a fork with no config of its own: the merge is clean, and
            before the fix `check` ran upstream's gate from the variant."""
            fork = make_fork(self.tmp)
            if not case_insensitive(fork):
                self.skipTest("the filesystem is case-sensitive")
            flag = os.path.join(self.tmp, "upstream-gate-ran")
            self.upstream_variant(flag)
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, err + out)
            self.assertIn("`%s`" % self.VARIANT, err)
            self.assertFalse(os.path.exists(flag))
            name = sync_branch_name()
            self.assertEqual(checked_out(fork), name)                    # the merge is made
            self.assertEqual(origin_sha(fork, name), "")                   # nothing pushed
            # the route the refusal names, run as printed: the fork has no config of its own,
            # so the variant takes the exact name, content kept (and a copy kept first),
            # committed on the sync branch
            with open(os.path.join(fork, self.VARIANT)) as fh:
                theirs = fh.read()
            cmd, kept = remedy_of(err)
            sh("sh", "-c", cmd, cwd=fork)
            self.assertIn(CONFIG_FILE, os.listdir(fork))
            with open(os.path.join(fork, CONFIG_FILE)) as fh:
                self.assertEqual(fh.read(), theirs)
            with open(kept) as fh:
                self.assertEqual(fh.read(), theirs)
            sh("git", "commit", "-q", "-m", "upstream's config under the name forkflow reads",
               cwd=fork)
            code, out, err2 = run_printed(err, "forkflow sync --continue", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))
            self.assertFalse(os.path.exists(flag))       # still upstream's gate: shown, not run

        @needs_tomllib
        def test_a_conflicted_sync_bringing_a_variant_is_not_merged_on_continue(self):
            fork = make_fork(self.tmp)
            if not case_insensitive(fork):
                self.skipTest("the filesystem is case-sensitive")
            flag = os.path.join(self.tmp, "upstream-gate-ran")
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            self.upstream_variant(flag)
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                            "theirs: shared")
            merging_tool(self.tmp, "glab")
            trunk = origin_sha(fork, "develop")
            with on_platform("gitlab"):
                self.assertEqual(run("-C", fork, "sync")[0], 4)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            name = sync_branch_name()
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--continue", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertIn("`%s`" % self.VARIANT, err)
            self.assertFalse(os.path.exists(flag))
            self.assertEqual(origin_sha(fork, name), "")
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])
            self.assertEqual(origin_sha(fork, "develop"), trunk)
            # renamed to the exact name, it is upstream's config arriving with the merge:
            # `--merge` is refused (the fork's own config says nothing), and the gate is
            # shown rather than run
            sh("git", "mv", self.VARIANT, CONFIG_FILE, cwd=fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--continue", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertIn("--merge needs", err)
            with on_platform("gitlab"):
                code, out, err = run_printed(err, "forkflow sync --continue", fork)
            self.assertEqual(code, 0, err + out)
            self.assertIn("NOT RUN", out)
            self.assertFalse(os.path.exists(flag))
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])
            self.assertEqual(origin_sha(fork, "develop"), trunk)

        # -- the remedies: on a case-insensitive filesystem the variant and `.forkflow.toml`
        # -- are ONE file, so a remedy that deletes or renames "the variant" takes the fork's
        # -- own config with it. Each is run here exactly as printed, and each must leave the
        # -- fork's config in place and the file as it was in the copy the message names.
        # -- On the trunk, the mirror or a detached HEAD nothing is printed to run at all.

        OURS = 'trunk = "develop"\ngate = ["true"]\n'

        def refusal(self, fork: str) -> str:
            with self.assertRaises(Fail) as cm:
                load_config(fork)
            self.assertEqual(cm.exception.code, 2)
            return str(cm.exception)

        def variant_beside_ours(self, variant_text: str, branch: Optional[str] = "feat/cfg",
                                on_disk: Optional[str] = None) -> str:
            """The state a sync leaves on a case-insensitive filesystem, built on any: the
            fork's `.forkflow.toml` committed, a variant staged beside it, and on disk only
            the variant's name, holding `on_disk` (`variant_text`, unless the user has edited
            it since). On `branch` - None stays on the trunk. The index is refreshed (`git
            status`), as any status the user runs does."""
            fork = make_fork(self.tmp, config=self.OURS)
            if branch:
                sh("git", "checkout", "-q", "-b", branch, cwd=fork)
            blob = sh("git", "hash-object", "-w", write(self.tmp, "blob.txt", variant_text),
                      cwd=fork)
            sh("git", "update-index", "--add", "--cacheinfo",
               "100644,%s,%s" % (blob, self.VARIANT), cwd=fork)
            os.unlink(os.path.join(fork, CONFIG_FILE))
            write(fork, self.VARIANT, variant_text if on_disk is None else on_disk)
            self.assertNotIn(CONFIG_FILE, os.listdir(fork))
            sh("git", "status", "--porcelain", cwd=fork)
            return fork

        def assert_ours_back(self, fork: str) -> None:
            """The fork's own config back under its exact name, content and index alike, and
            the variant out of the index."""
            self.assertIn(CONFIG_FILE, os.listdir(fork))
            with open(os.path.join(fork, CONFIG_FILE)) as fh:
                self.assertEqual(fh.read(), self.OURS)
            self.assertEqual(load_config(fork), {"trunk": "develop", "gate": ["true"]})
            self.assertEqual(sh("git", "show", ":" + CONFIG_FILE, cwd=fork), self.OURS.strip())
            self.assertNotIn(self.VARIANT, sh("git", "ls-files", cwd=fork).splitlines())

        def run_remedy(self, fork: str, was: str) -> None:
            """The printed remedy, run as printed: it succeeds, and the copy it names holds
            `was` - what the working file held before it ran."""
            cmd, kept = remedy_of(self.refusal(fork))
            sh("sh", "-c", cmd, cwd=fork)
            self.assertEqual(os.path.dirname(kept), os.path.dirname(git_path(fork, "x")))
            with open(kept) as fh:
                self.assertEqual(fh.read(), was)

        @needs_tomllib
        def test_the_index_only_remedy_brings_the_forks_config_back(self):
            fork = self.variant_beside_ours('gate = ["touch theirs"]\n')
            self.run_remedy(fork, 'gate = ["touch theirs"]\n')
            self.assert_ours_back(fork)

        @needs_tomllib
        def test_the_index_only_remedy_holds_when_the_two_hold_the_same_bytes(self):
            """`git rm --cached <variant> && git checkout -- .forkflow.toml` leaves the file
            under the variant's name when the bytes match and the index is fresh: checkout
            sees an up-to-date entry and writes nothing, and the refusal comes straight back.
            Both names leave the index, so the checkout from HEAD writes the file anew."""
            fork = self.variant_beside_ours(self.OURS)
            self.run_remedy(fork, self.OURS)
            self.assert_ours_back(fork)

        @needs_tomllib
        def test_an_edit_made_before_the_remedy_is_in_the_copy(self):
            """The refusal used to invite an edit of the file, and the checkout from HEAD
            then overwrote it with no copy anywhere: the copy is made first."""
            fork = self.variant_beside_ours(self.OURS, on_disk=self.OURS + "# mine\n")
            self.run_remedy(fork, self.OURS + "# mine\n")
            self.assert_ours_back(fork)

        @needs_tomllib
        def test_the_remedy_goes_through_where_git_rm_cached_refuses(self):
            """The staged variant differs from both the file and HEAD - the middle of a merge
            with the file edited since: `git rm --cached` refuses and suggests `-f`, which
            the user would follow. The remedy asks neither."""
            fork = self.variant_beside_ours('gate = ["touch theirs"]\n', on_disk="# mine\n")
            p = subprocess.run(["git", "rm", "--cached", "-q", "--", self.VARIANT], cwd=fork,
                               capture_output=True)
            self.assertNotEqual(p.returncode, 0)                           # the refusing state
            self.run_remedy(fork, "# mine\n")
            self.assert_ours_back(fork)

        def test_the_rename_remedy_warns_that_a_config_a_sync_brought_stays_upstreams(self):
            """`git mv` gives upstream's file the exact name and nothing more: the sync
            still treats it as upstream's - its `gate` shown rather than run - and `--merge`
            is refused while the bytes are upstream's, until this fork edits the file
            itself. The clause naming `--merge` was dropped once, and the remedy then read
            as "rename it and it is yours"."""
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-q", "-b", "feat/cfg", cwd=fork)
            write(fork, self.VARIANT, 'trunk = "develop"\n')
            sh("git", "add", self.VARIANT, cwd=fork)
            sh("git", "commit", "-q", "-m", "a config, spelled differently", cwd=fork)
            err = self.refusal(fork)
            self.assertIn("git mv", err)
            self.assertIn("`gate` is shown rather than run", err)
            self.assertIn("`--merge` is refused", err)

        @needs_tomllib
        def test_a_tracked_variant_alone_is_renamed_with_its_content(self):
            """No `.forkflow.toml` is tracked: the variant is the only config there is, and
            `git mv` gives it the exact name - nothing deleted."""
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-q", "-b", "feat/cfg", cwd=fork)
            write(fork, self.VARIANT, 'trunk = "develop"\n')
            sh("git", "add", self.VARIANT, cwd=fork)
            sh("git", "commit", "-q", "-m", "a config, spelled differently", cwd=fork)
            self.run_remedy(fork, 'trunk = "develop"\n')
            self.assertIn(CONFIG_FILE, os.listdir(fork))
            self.assertNotIn(self.VARIANT, os.listdir(fork))
            self.assertEqual(load_config(fork), {"trunk": "develop"})
            self.assertEqual(sh("git", "ls-files", CONFIG_FILE, cwd=fork), CONFIG_FILE)

        @needs_tomllib
        def test_on_the_trunk_nothing_is_run_and_the_fix_goes_on_a_branch(self):
            """The fork's config committed on its trunk under a variant name - which worked
            before variants were refused. On `develop`, and on a detached HEAD, the refusal
            names nothing to run: a commit there never reaches origin and every later `land`
            refuses the trunk. On a branch off `origin/develop` it names the rename, which run
            as printed and committed leaves the trunk, local and on origin, as it was."""
            fork = make_fork(self.tmp)
            write(fork, self.VARIANT, 'trunk = "develop"\n')
            sh("git", "add", self.VARIANT, cwd=fork)
            sh("git", "commit", "-q", "-m", "fork: config, spelled differently", cwd=fork)
            sh("git", "push", "-q", "origin", "develop", cwd=fork)
            sh("git", "fetch", "-q", "origin", cwd=fork)
            trunk = rev(fork, "refs/heads/develop")
            no_remedy(self.refusal(fork))
            sh("git", "checkout", "-q", "--detach", cwd=fork)
            no_remedy(self.refusal(fork))
            sh("git", "checkout", "-q", "-b", "fix/config", "origin/develop", cwd=fork)
            self.run_remedy(fork, 'trunk = "develop"\n')
            sh("git", "commit", "-q", "-m", "the config under its exact name", cwd=fork)
            self.assertEqual(load_config(fork), {"trunk": "develop"})
            self.assertEqual(rev(fork, "refs/heads/develop"), trunk)
            self.assertEqual(origin_sha(fork, "develop"), trunk)
            self.assertEqual(sh("git", "ls-files", "--", CONFIG_FILE, cwd=fork), CONFIG_FILE)

        @needs_tomllib
        def test_the_index_state_on_the_trunk_names_nothing_to_run(self):
            fork = self.variant_beside_ours('gate = ["touch theirs"]\n', branch=None)
            no_remedy(self.refusal(fork))

        def test_a_trunk_of_another_name_is_known_by_origins_default_branch(self):
            """The config naming the trunk is the file that cannot be read: `origin/HEAD`
            (which `setup` sets, and whose platform default its report insists on) says it."""
            fork = make_fork(self.tmp, trunk="trunk")
            self.assertEqual(checked_out(fork), "trunk")
            write(fork, self.VARIANT, 'trunk = "trunk"\n')
            sh("git", "add", self.VARIANT, cwd=fork)
            sh("git", "commit", "-q", "-m", "fork: config, spelled differently", cwd=fork)
            no_remedy(self.refusal(fork))

        def test_the_trunk_and_the_mirror_are_the_only_branches_that_refuse_a_commit(self):
            """`no_commit_here` encodes rules 2 and 6 and nothing besides. The mirror is
            known by its name, the config that would name it being the file that cannot be
            read: the default, and any name the original project's remote has a branch of
            (`main` beside `upstream/main`, which is what `setup` bootstraps). A branch of
            the fork's own refuses nothing, however little of its own it carries - it used
            to refuse whenever it held only upstream's commits, which on a fresh fork is
            every branch there is."""
            fork = make_fork(self.tmp)
            sh("git", "push", "-q", "origin", "main:refs/heads/release",
               cwd=os.path.join(self.tmp, "seed"))              # another upstream branch
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "branch", "release", "develop", cwd=fork)
            sh("git", "branch", "feat/x", "develop", cwd=fork)
            for branch, why in (("develop", "origin's default branch"),
                                ("main", "is the mirror"), ("release", "is the mirror"),
                                ("feat/x", "")):
                sh("git", "checkout", "-q", branch, cwd=fork)
                answer = no_commit_here(fork, "develop")
                if why:
                    self.assertIn(why, answer, branch)
                else:
                    self.assertEqual(answer, "", branch)
            sh("git", "checkout", "-q", "--detach", cwd=fork)
            self.assertEqual(no_commit_here(fork, "develop"), "HEAD is detached")

        @needs_tomllib
        def test_a_fresh_fork_of_a_project_that_tracks_a_variant_is_not_a_dead_end(self):
            """The project itself tracks `.ForkFlow.toml`, and the fork is new: `develop`,
            `main` and every branch off `origin/develop` are all still at upstream's tip.
            Each of them answered "carries nothing but upstream's commits" and named no
            command, so `forkflow setup` - the first command a new fork runs - and the very
            branch its advice sends you to were both dead ends, with only an unrelated
            commit leading out. The trunk and the mirror still name no command; the branch
            they send you to names the rename, which run as printed leaves the config
            readable."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, self.VARIANT, 'gate = ["true"]\n', "theirs: variant")
            push_upstream_into_origin(self.tmp, "develop")
            push_upstream_into_origin(self.tmp, "main")
            sh("git", "fetch", "-q", "--prune", "origin", cwd=fork)
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "merge", "-q", "--ff-only", "origin/develop", cwd=fork)
            self.assertIn(self.VARIANT, os.listdir(fork))
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 2, err + out)
            no_remedy(err)                                   # rule 2: nothing to run here
            self.assertIn("branch off `origin/develop`", err)
            sh("git", "checkout", "-q", "main", cwd=fork)
            sh("git", "merge", "-q", "--ff-only", "origin/main", cwd=fork)
            no_remedy(self.refusal(fork))                    # rule 6: nor here
            sh("git", "checkout", "-q", "-b", "fix/config", "origin/develop", cwd=fork)
            self.run_remedy(fork, 'gate = ["true"]\n')
            sh("git", "commit", "-q", "-m", "the config under its exact name", cwd=fork)
            self.assertEqual(load_config(fork), {"gate": ["true"]})

        def test_on_the_mirror_nothing_is_run(self):
            """Upstream tracks a variant and the mirror is checked out: the file is upstream's,
            and a commit on the mirror breaks every later sync and setup - nothing to run."""
            fork = make_fork(self.tmp)
            commit_upstream(self.tmp, self.VARIANT, 'gate = ["true"]\n', "theirs: variant")
            sh("git", "fetch", "-q", "upstream", cwd=fork)
            sh("git", "checkout", "-q", "main", cwd=fork)
            sh("git", "merge", "-q", "--ff-only", "upstream/main", cwd=fork)
            mirror = rev(fork, "refs/heads/main")
            no_remedy(self.refusal(fork))
            self.assertEqual(rev(fork, "refs/heads/main"), mirror)

        def test_a_merge_whose_copy_left_the_index_names_nothing_to_run(self):
            """Mid-merge, upstream's variant taken out of the index by hand: the file on disk
            is upstream's, untracked. Renamed to the exact name it would be read as this
            fork's config - its `gate` run - so nothing is named."""
            fork = make_fork(self.tmp)
            sh("git", "checkout", "-q", "-b", "side", cwd=fork)
            write(fork, self.VARIANT, 'gate = ["touch theirs"]\n')
            commit_fork(fork, "src/app.py", "side\n", "side: variant and app")
            sh("git", "checkout", "-q", "-b", "sync/upstream-x", "develop", cwd=fork)
            commit_fork(fork, "src/app.py", "ours\n", "ours: app")
            sh("git", "merge", "side", cwd=fork, check=False)
            sh("git", "rm", "-q", "--cached", self.VARIANT, cwd=fork)
            self.assertIn(self.VARIANT, os.listdir(fork))
            no_remedy(self.refusal(fork))

        def test_a_variant_upstream_adds_is_in_the_way_where_the_filesystem_folds_case(self):
            """The collision check before the backup, on any filesystem: an upstream
            `.ForkFlow.toml` is in the way of this fork's untracked `.forkflow.toml` exactly
            when the two names open one file - asked of the filesystem, and answered here by a
            stand-in for each kind. The name reported is the one on disk."""
            fork = make_fork(self.tmp)
            write(fork, CONFIG_FILE, "# ours, untracked\n")
            target = commit_upstream(self.tmp, self.VARIANT, "gate = []\n", "theirs: forkflow")
            sh("git", "fetch", "upstream", cwd=fork)
            ctx = ctx_for(fork)

            def one_file(a: str, b: str) -> bool:
                return a.casefold() == b.casefold()

            with mock.patch.object(os.path, "samefile", side_effect=one_file):
                self.assertEqual(untracked_in_the_way(ctx, target), [CONFIG_FILE])
            with mock.patch.object(os.path, "samefile", return_value=False):
                self.assertEqual(untracked_in_the_way(ctx, target), [])
            # git-ignored, the file is one git would write over without a word: still in the way
            with open(os.path.join(fork, ".git", "info", "exclude"), "a") as fh:
                fh.write(CONFIG_FILE + "\n")
            self.assertNotIn(CONFIG_FILE, sh("git", "status", "--porcelain", cwd=fork))
            with mock.patch.object(os.path, "samefile", side_effect=one_file):
                self.assertEqual(untracked_in_the_way(ctx, target), [CONFIG_FILE])

        @needs_tomllib
        def test_the_in_the_way_advice_says_when_the_untracked_config_is_upstreams(self):
            """`setup`'s untracked template and upstream's own file, left untracked after a
            sync, are the same shape on disk. The advice called both "this fork's own
            config" and sent the user to commit and ship it - which for upstream's bytes
            puts upstream's word on the trunk and leaves `--merge` refused there, with
            nothing in the message saying so. The bytes decide what the sentence says."""
            fork = make_fork(self.tmp)
            theirs = 'merge = "self"\ngate = []\n'
            commit_upstream(self.tmp, CONFIG_FILE, theirs, "theirs: forkflow")
            write(fork, CONFIG_FILE, theirs)              # upstream's file, untracked here
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, err + out)
            self.assertIn("the original project's own file", err)
            self.assertIn("`--merge` stays refused", err)
            write(fork, CONFIG_FILE, theirs + "# ours\n")          # this fork's own, now
            code, out, err = run("-C", fork, "sync")
            self.assertEqual(code, 2, err + out)
            self.assertIn("is this fork's own config, untracked", err)
            self.assertNotIn("the original project's own file", err)

        @needs_tomllib
        def test_an_ignored_untracked_config_is_in_the_way_before_the_backup(self):
            """setup's `.forkflow.toml`, kept out of `git status` with `info/exclude`, and
            upstream starts tracking one: git writes over an ignored file without a word, and
            `sync --merge` merged and landed upstream's config - this fork's `merge`, `gate`
            and settings gone. Refused before the backup, the file untouched."""
            fork = make_fork(self.tmp)
            ours = 'merge = "self"\ngate = ["true"]\n# my settings\n'
            path = write(fork, CONFIG_FILE, ours)
            with open(os.path.join(fork, ".git", "info", "exclude"), "a") as fh:
                fh.write(CONFIG_FILE + "\n")
            self.assertEqual(sh("git", "status", "--porcelain", cwd=fork), "")
            commit_upstream(self.tmp, CONFIG_FILE, 'gate = ["true"]\n', "theirs: forkflow")
            merging_tool(self.tmp, "glab")
            trunk = origin_sha(fork, "develop")
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(sh("git", "ls-remote", "origin", "refs/heads/backup/*", cwd=fork),
                             "")
            self.assertNotIn(sync_branch_name(), local_branches(fork))
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(origin_sha(fork, "develop"), trunk)
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])
            with open(path) as fh:
                self.assertEqual(fh.read(), ours)

        @needs_tomllib
        def test_a_config_gone_from_the_tree_mid_sync_is_put_back_before_it_goes_on(self):
            """A conflict on upstream's `.ForkFlow.toml` resolved with `git rm` deletes the one
            file a case-insensitive filesystem holds for both names: the fork's
            `.forkflow.toml` is still in the index and gone from the tree, and `sync
            --continue` went on with no config - "gate none configured". Built on any
            filesystem by removing the file; the command printed, run as printed, puts it back
            and the resumed sync runs the fork's gate."""
            flag = os.path.join(self.tmp, "fork-gate-ran")
            config = 'gate = ["touch %s"]\n' % flag
            fork = make_fork(self.tmp, config=config)
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                            "theirs: shared")
            self.assertEqual(run("-C", fork, "sync")[0], 4)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            os.unlink(os.path.join(fork, CONFIG_FILE))
            code, out, err = run("-C", fork, "sync", "--continue")
            self.assertEqual(code, 2, err + out)
            name = sync_branch_name()
            self.assertEqual(origin_sha(fork, name), "")
            self.assertFalse(os.path.exists(flag))
            sh("sh", "-c", printed_cmd(err, "git checkout"), cwd=fork)
            with open(os.path.join(fork, CONFIG_FILE)) as fh:
                self.assertEqual(fh.read(), config)
            code, out, err = run_printed(err, "forkflow sync --continue", fork)
            self.assertEqual(code, 0, err + out)
            self.assertTrue(os.path.exists(flag))
            self.assertEqual(origin_sha(fork, name), rev(fork, "refs/heads/" + name))

        @needs_tomllib
        def test_an_untracked_config_meets_upstreams_variant_before_the_backup(self):
            """setup's untracked `.forkflow.toml` and upstream's `.ForkFlow.toml`: one file
            here. The case-exact check let `sync --merge` push a backup and create the sync
            branch before git refused the merge, and its "remove them" deleted this fork's
            config for good. Now it is refused before the backup - and every command printed
            on the way, run as printed, ends with the sync merged, landed, and this fork's
            config and gate the ones in force."""
            fork = make_fork(self.tmp)
            if not case_insensitive(fork):
                self.skipTest("the filesystem is case-sensitive")
            ours_flag = os.path.join(self.tmp, "fork-gate-ran")
            theirs_flag = os.path.join(self.tmp, "upstream-gate-ran")
            ours = 'merge = "self"\ngate = ["touch %s"]\n' % ours_flag
            path = write(fork, CONFIG_FILE, ours)
            self.upstream_variant(theirs_flag)
            merging_tool(self.tmp, "glab")
            trunk = origin_sha(fork, "develop")

            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(sh("git", "ls-remote", "origin", "refs/heads/backup/*", cwd=fork),
                             "")
            self.assertNotIn("backup/", local_branches(fork))
            self.assertNotIn(sync_branch_name(), local_branches(fork))
            self.assertEqual(origin_sha(fork, sync_branch_name()), "")
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(origin_sha(fork, "develop"), trunk)
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])
            self.assertIn(CONFIG_FILE, os.listdir(fork))
            with open(path) as fh:
                self.assertEqual(fh.read(), ours)

            # commit it on a branch off the trunk and ship it - as printed, `--mr`: committed
            # on a branch it is neither untracked nor on origin/develop, so nothing says
            # "self" until a person has merged it - then land it
            sh("git", "checkout", "-q", "-b", "cfg", "origin/develop", cwd=fork)
            sh("git", "add", CONFIG_FILE, cwd=fork)
            sh("git", "commit", "-q", "-m", "ours: forkflow config", cwd=fork)
            with on_platform("gitlab"):
                code, out, err2 = run_printed(err, "forkflow ship", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assertEqual(tool_argv(self.tmp, "glab", "create")[:2], ["mr", "create"])
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])
            self.move_trunk(origin_sha(fork, "cfg"))                     # merged by a person
            code, out, err2 = run("-C", fork, "land")
            self.assertEqual(code, 0, err2 + out)
            self.assertEqual(sh("git", "show", "origin/develop:" + CONFIG_FILE, cwd=fork),
                             ours.strip())
            # then the sync again: now the merge is made, and the variant beside the fork's
            # own config is refused - with the index-only way back
            os.unlink(ours_flag)
            next_utc_second()
            with on_platform("gitlab"):
                code, out, err = run_printed(err, "forkflow sync", fork)
            self.assertEqual(code, 2, err + out)
            self.assertFalse(os.path.exists(theirs_flag))
            self.assertEqual(tool_argv(self.tmp, "glab", "merge"), [])    # nothing merged yet
            with open(path) as fh:
                theirs = fh.read()                                 # what the merge put there
            cmd, kept = remedy_of(err)
            sh("sh", "-c", cmd, cwd=fork)
            with open(kept) as fh:
                self.assertEqual(fh.read(), theirs)
            with open(path) as fh:
                self.assertEqual(fh.read(), ours)
            self.assertIn(CONFIG_FILE, os.listdir(fork))
            sh("git", "commit", "-q", "-m", "keep this fork's config", cwd=fork)
            with on_platform("gitlab"):
                code, out, err2 = run_printed(err, "forkflow sync --continue", fork)
            self.assertEqual(code, 0, err2 + out)
            self.assertTrue(os.path.exists(ours_flag))                   # the fork's gate ran
            self.assertFalse(os.path.exists(theirs_flag))
            self.assertEqual(tool_argv(self.tmp, "glab", "merge")[:3],
                             ["mr", "merge", sync_branch_name()])
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(origin_sha(fork, "develop"), rev(fork, "refs/heads/develop"))
            tree = sh("git", "ls-tree", "--name-only", "origin/develop", cwd=fork).splitlines()
            self.assertIn(CONFIG_FILE, tree)
            self.assertNotIn(self.VARIANT, tree)
            with open(path) as fh:
                self.assertEqual(fh.read(), ours)
            self.assertEqual(self.pending_of(fork), {})

        @needs_tomllib
        def test_a_conflicted_sync_beside_the_forks_tracked_config_resumes_intact(self):
            """The fork tracks `.forkflow.toml`; the sync conflicts elsewhere and brings
            upstream's variant, and the user edits the file in the middle of it. `sync
            --continue --merge` is refused with the index-only way back; run as printed -
            where `git rm --cached` refused and suggested `-f` - committed, and resumed, this
            fork's gate runs and its config is what lands, the edit in the copy named.
            `git rm .ForkFlow.toml` - the old advice - took the fork's working
            `.forkflow.toml` with it."""
            flag = os.path.join(self.tmp, "fork-gate-ran")
            ours = 'merge = "self"\ngate = ["touch %s"]\n' % flag
            fork = make_fork(self.tmp, config=ours)
            if not case_insensitive(fork):
                self.skipTest("the filesystem is case-sensitive")
            theirs_flag = os.path.join(self.tmp, "upstream-gate-ran")
            commit_fork(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 2"),
                        "ours: shared", push=True)
            self.upstream_variant(theirs_flag)
            commit_upstream(self.tmp, "shared.tf", self.BASE_TF.replace("count = 1", "count = 3"),
                            "theirs: shared")
            merging_tool(self.tmp, "glab")
            with on_platform("gitlab"):
                self.assertEqual(run("-C", fork, "sync", "--merge")[0], 4)
            write(fork, "shared.tf", self.BASE_TF.replace("count = 1", "count = 4"))
            sh("git", "add", "shared.tf", cwd=fork)
            with open(os.path.join(fork, CONFIG_FILE), "a") as fh:
                fh.write("# my edit\n")                                    # the one file on disk
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--continue", "--merge")
            self.assertEqual(code, 2, err + out)
            self.assertEqual(origin_sha(fork, sync_branch_name()), "")
            with open(os.path.join(fork, CONFIG_FILE)) as fh:
                edited = fh.read()
            self.assertIn("# my edit", edited)
            cmd, kept = remedy_of(err)
            sh("sh", "-c", cmd, cwd=fork)
            with open(kept) as fh:
                self.assertEqual(fh.read(), edited)
            path = os.path.join(fork, CONFIG_FILE)
            self.assertIn(CONFIG_FILE, os.listdir(fork))
            with open(path) as fh:
                self.assertEqual(fh.read(), ours)
            sh("git", "commit", "-q", "--no-edit", cwd=fork)
            with on_platform("gitlab"):
                code, out, err = run("-C", fork, "sync", "--continue", "--merge")
            self.assertEqual(code, 0, err + out)
            self.assertTrue(os.path.exists(flag))
            self.assertFalse(os.path.exists(theirs_flag))
            self.assertEqual(checked_out(fork), "develop")
            self.assertEqual(sh("git", "show", "origin/develop:" + CONFIG_FILE, cwd=fork),
                             ours.strip())
            self.assertNotIn(self.VARIANT, sh("git", "ls-tree", "--name-only", "origin/develop",
                                              cwd=fork).splitlines())

    class TestParseArgs(unittest.TestCase):
        def test_common_flags_before_or_after_the_subcommand(self):
            for argv in (["--dry-run", "sync"], ["sync", "--dry-run"]):
                self.assertTrue(parse_args(argv).dry_run, argv)
            for argv in (["-C", "/x", "status"], ["status", "-C", "/x"]):
                self.assertEqual(parse_args(argv).dir, "/x", argv)
            for argv in (["--force", "setup"], ["setup", "--force"]):
                self.assertTrue(parse_args(argv).force, argv)

        def test_the_dry_run_help_does_not_promise_a_fetch(self):
            """A flag's own help is the first place anybody reads what it does, and this one
            still said "(it does fetch, and simulates the merge)" long after the no-write
            contract stopped it fetching - a fetch moves `FETCH_HEAD` and `refs/remotes/*`
            and brings objects in, so `fetch_preview` asks `git ls-remote` instead. Read out
            of the parser, so the sentence a user is shown is the one under test."""
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                parse_args(["--help"])
            text = " ".join(out.getvalue().split())
            self.assertIn("--dry-run", text)
            self.assertIn("fetch nothing", text)
            self.assertNotIn("does fetch", text)

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

        def test_land_takes_the_common_flags_and_one_optional_branch(self):
            self.assertIs(COMMANDS["land"], cmd_land)
            for argv in (["land", "--force"], ["--force", "land"]):
                args = parse_args(argv)
                self.assertEqual((args.cmd, args.force, args.dry_run), ("land", True, False), argv)
                self.assertIsNone(args.branch)
            args = parse_args(["land", "--dry-run", "-C", "/x"])
            self.assertEqual((args.force, args.dry_run, args.dir), (False, True, "/x"))
            for argv in (["land", "feat/a", "--force"], ["--force", "land", "feat/a"]):
                args = parse_args(argv)
                self.assertEqual((args.branch, args.force), ("feat/a", True), argv)
            with self.assertRaises(SystemExit):        # `--merge` belongs to sync and ship
                capture(parse_args, ["land", "--merge"])
            with self.assertRaises(SystemExit):        # one branch, not a list
                capture(parse_args, ["land", "feat/a", "feat/b"])

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
            cls.tree = tree
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

        def test_a_dry_run_reaches_no_command_that_writes(self):
            """`--dry-run` promises to write nothing, and two git commands were writing
            under it: `git fetch` rewrites `FETCH_HEAD`, moves the remote-tracking refs and
            brings objects in, and `git merge-tree --write-tree` writes the merged tree and
            a blob per conflict. So `fetch` is spelled in one place, which answers a dry run
            with `ls-remote` instead (`fetch_preview` - one round trip, nothing written),
            and `merge-tree` is spelled in one place, which sends the objects it writes to a
            scratch directory. A caller added later cannot spell either for itself."""
            self.assertEqual(self.owners('"merge-tree"'), {"merge_tree"})
            self.assertEqual(self.owners('["fetch"]'), {"fetch", "fetch_preview"})
            self.assertEqual(self.owners("fetch_preview("), {"fetch_preview", "fetch"})

        def test_the_only_rebase_is_of_a_feature_branch(self):
            self.assertEqual(self.owners('"rebase"'), {"rebase_onto"})

        def test_git_and_the_platform_tools_are_the_only_subprocesses(self):
            """`run_tool` took over the platform-tool call from `open_mr` when the merge
            step (`merge_mr`) came to share it: a rename of the one owner, not a widening
            of the set."""
            self.assertEqual(self.owners("subprocess"),      # `<module>` is the import
                             {"git", "git_ok", "git_rc", "shell", "run_tool", "api_get",
                              "<module>"})
            # `run_tool` runs whatever it is handed, so who hands it something is pinned too:
            # the merge request's two steps and nothing else
            self.assertEqual(self.owners("run_tool("), {"run_tool", "open_mr", "merge_mr"})

        def test_every_printed_sync_and_ship_command_comes_from_rerun_cmd(self):
            """A printed `forkflow sync ...` / `forkflow ship ...` spelled out by hand drops
            the `--merge` (or `--mr`) the run was given, and a hand-built `forkflow land`
            drops the `--force` or the branch - found at one site after another, for both.
            So no string in the code spells one except the builder's own: `rerun_cmd`
            (behind `continue_cmd`) writes every `forkflow sync` and `forkflow ship`,
            `land_cmd` every `forkflow land`, and `header`'s title line is the one other
            `forkflow {...}`. Every
            string literal and f-string is read, adjacent pieces joined as Python joins them;
            docstrings are prose and are skipped (comments never reach the tree). A string
            that ends in the word `forkflow` is the head of one assembled with `+` or
            `str.join` - `argparse`'s program name is the one there is."""
            import ast
            prose = {id(n.value) for n in ast.walk(self.tree)
                     if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
            parts = {id(v) for n in ast.walk(self.tree) if isinstance(n, ast.JoinedStr)
                     for v in n.values}
            spelled, built, headed = set(), set(), set()
            for n in ast.walk(self.tree):
                if getattr(n, "lineno", self.limit) >= self.limit:
                    continue
                if isinstance(n, ast.JoinedStr):
                    text = "".join(v.value if isinstance(v, ast.Constant) else "{}"
                                   for v in n.values)
                elif (isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and id(n) not in prose and id(n) not in parts):
                    text = n.value
                else:
                    continue
                owner = self.owner.get(n.lineno, "<module>")
                if re.search(r"forkflow\s+(sync|ship|land)\b", text):
                    spelled.add(owner)
                if re.search(r"forkflow\s+(\{|%)", text):
                    built.add(owner)
                if re.search(r"(^|\s)forkflow\s*$", text):
                    headed.add(owner)
            self.assertEqual(spelled, {"land_cmd"})
            self.assertEqual(built, {"rerun_cmd", "header"})
            self.assertEqual(headed, {"parse_args"})

        def test_every_merge_decision_goes_through_fork_merge_mode(self):
            """The `merge` a `--merge` run trusts was read from a place upstream can write -
            the working tree on `--continue`, a case variant, a sync branch left checked out -
            once per route, each closed alone. So the key has one reader, `fork_merge_mode`;
            the gate refuses on it before anything is pushed; and the merge command is run in
            `merge_mr` alone, only after a refusal on it - so every path to a merge, fresh or
            resumed, passes it, and the gate sits before every road into `merge_mr`."""
            import ast
            funcs = {n.name: n for n in ast.walk(self.tree)
                     if isinstance(n, ast.FunctionDef) and n.lineno < self.limit}

            def calls(func: str, name: str) -> list:
                return sorted(c.lineno for c in ast.walk(funcs[func]) if isinstance(c, ast.Call)
                              and isinstance(c.func, ast.Name) and c.func.id == name)

            def refuses_on(func: str, name: str) -> bool:
                """An `if` whose test asks `name` and whose body raises."""
                return any(isinstance(n, ast.If) and any(
                    isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                    and c.func.id == name for c in ast.walk(n.test))
                    and any(isinstance(b, ast.Raise) for b in ast.walk(n))
                    for n in ast.walk(funcs[func]))

            self.assertEqual(self.owners('get("merge")'),
                             {"parse_config", "merge_mode_in", "fork_merge_mode"})
            self.assertEqual(self.owners('["merge"]'), set())
            self.assertTrue(refuses_on("merge_gate", "fork_merge_mode"))
            self.assertTrue(refuses_on("merge_mr", "fork_merge_mode"))
            self.assertEqual(self.owners("merge_command("), {"merge_command", "merge_mr"})
            self.assertLess(calls("merge_mr", "fork_merge_mode")[0], calls("merge_mr", "run_tool")[0])
            # the roads into `merge_mr`, and the gate before each
            self.assertEqual(self.owners("merge_mr("), {"merge_mr", "publish"})
            self.assertEqual(self.owners("publish("), {"publish", "finish_sync", "finish_ship"})
            self.assertEqual(self.owners("finish_sync("),
                             {"finish_sync", "cmd_sync", "cmd_sync_continue"})
            self.assertEqual(self.owners("cmd_sync_continue("), {"cmd_sync_continue", "cmd_sync"})
            self.assertEqual(self.owners("finish_ship("), {"finish_ship", "cmd_ship"})
            gate = calls("cmd_sync", "merge_gate")[0]
            self.assertLess(gate, min(calls("cmd_sync", "cmd_sync_continue")
                                      + calls("cmd_sync", "finish_sync")))
            self.assertLess(calls("cmd_ship", "merge_gate")[0], calls("cmd_ship", "finish_ship")[0])

        def test_whose_config_it_is_is_decided_by_its_bytes_in_one_place(self):
            """Provenance - is this `.forkflow.toml` this fork's own, or the original
            project's? - was decided by the PATH's history in one reader and by INDEX
            MEMBERSHIP in another, and each answer was wrong in both directions
            (`config_is_upstreams`). There is one question now, asked of the content, and a
            fourth check added later cannot answer it its own way: what upstream's configs
            are is computed in one place, every provenance answer is a call to
            `config_is_upstreams`, and the two answers `fork_config_state` reads ask nothing
            else at all - no `git log`, no index. `fork_merge_mode` and `fork_merge_refusal`
            both switch on the state it returns rather than deriving one of their own, so a
            state added there cannot reach a message written for another."""
            import ast
            funcs = {n.name: n for n in ast.walk(self.tree)
                     if isinstance(n, ast.FunctionDef) and n.lineno < self.limit}

            def calls_in(func: str) -> set:
                return {c.func.id for c in ast.walk(funcs[func]) if isinstance(c, ast.Call)
                        and isinstance(c.func, ast.Name)}

            # `fork_config_state` reads the set once and hands it to the two questions it
            # asks; every other answer computes it for itself, and nothing else may
            # decide what upstream's configs are
            self.assertEqual(self.owners("upstream_config_digests("),
                             {"upstream_config_digests", "config_is_upstreams",
                              "fork_config_state"})
            # and the parts of it that decide what may be read, whether the answer can be
            # trusted at all, and what this clone has written down of upstream's, are its
            # own - so none of them can be re-derived, or skipped, elsewhere
            self.assertEqual(self.owners("upstream_scope_refs("),
                             {"upstream_scope_refs", "upstream_config_digests",
                              "config_history_render_unprovable"})
            self.assertEqual(self.owners("history_unprovable("),
                             {"history_unprovable", "upstream_config_digests"})
            # the rendering questions belong to the states that read the WORKING TREE, so
            # they are called from `fork_config_state` - below `trunk_*` - and nowhere else.
            # In `upstream_config_digests` the first of them refused a fork in the
            # recommended state, where the config is a blob off `origin/<trunk>`
            self.assertEqual(self.owners("config_render_unprovable("),
                             {"config_render_unprovable", "fork_config_state"})
            self.assertEqual(self.owners("config_rendered_before("),
                             {"config_rendered_before", "fork_config_state"})
            self.assertEqual(self.owners("config_history_render_unprovable("),
                             {"config_history_render_unprovable", "fork_config_state"})
            self.assertEqual(self.owners("remember_rendered_config("),
                             {"remember_rendered_config", "fork_config_state"})
            # what this clone judged rendered is read where it is judged and where it is
            # remembered, and the scratch repository that asks git what a historical
            # `.gitattributes` sets is built in one place
            self.assertEqual(self.owners("remembered_rendered_configs("),
                             {"remembered_rendered_configs", "remember_rendered_config",
                              "config_rendered_before"})
            self.assertEqual(self.owners("attrs_render_the_config("),
                             {"attrs_render_the_config", "config_history_render_unprovable"})
            self.assertEqual(self.owners('"init"'), {"attrs_render_the_config"})
            self.assertEqual(self.owners('"check-attr"'),
                             {"config_render_unprovable", "attrs_render_the_config"})
            self.assertEqual(self.owners("config_memory_unprovable("),
                             {"config_memory_unprovable", "upstream_config_digests"})
            self.assertEqual(self.owners("config_versions_in("),
                             {"config_versions_in", "upstream_config_digests"})
            self.assertEqual(self.owners("remember_upstream_configs("),
                             {"remember_upstream_configs", "upstream_config_digests"})
            self.assertEqual(self.owners("foreign_remote_refs("),
                             {"foreign_remote_refs", "upstream_scope_refs",
                              "history_unprovable", "upstream_config_digests"})
            # what is written down comes from the refs upstream cannot choose, and from
            # nowhere else: `upstream_scope_refs` widens the answer for the run that reads
            # it and never reaches the memory
            self.assertEqual(calls_in("upstream_config_digests"),
                             {"history_unprovable",
                              "config_memory_unprovable", "foreign_remote_refs", "set",
                              "config_versions_in", "remember_upstream_configs",
                              "upstream_scope_refs"})
            # and the bytes are turned into what is compared and written down in one place
            self.assertEqual(self.owners("config_digest("),
                             {"config_digest", "config_versions_in", "config_is_upstreams",
                              "remember_rendered_config", "config_rendered_before"})
            self.assertEqual(calls_in("upstream_scope_refs"),
                             {"foreign_remote_refs", "has_ref", "set",
                              "merge_in_progress", "add"})
            self.assertEqual(self.owners("config_is_upstreams("),
                             {"config_is_upstreams", "written_by_upstream",
                              "own_untracked_config", "fork_config_state",
                              "in_the_way_advice"})
            self.assertEqual(calls_in("written_by_upstream"), {"config_is_upstreams"})
            self.assertEqual(calls_in("own_untracked_config"),
                             {"working_config_text", "tracked_config_names",
                              "merge_in_progress", "config_name_at", "config_is_upstreams",
                              "any"})
            self.assertEqual(self.owners("written_by_upstream("),
                             {"written_by_upstream", "fork_config_state"})
            self.assertEqual(self.owners("own_untracked_config("),
                             {"own_untracked_config", "fork_config_state"})
            # and the state both `--merge` readers switch on is decided in that one place
            self.assertEqual(self.owners("fork_config_state("),
                             {"fork_config_state", "fork_merge_mode", "fork_merge_refusal"})

        def test_every_copy_a_remedy_prints_is_built_here(self):
            """A REMEDY MUST NEVER PRODUCE CONFIG BYTES. Two rounds running, a remedy has
            ended by putting something back at `.forkflow.toml` - the contents a symlink
            read as, and a file git's own conversion had rewritten - and each time the bytes
            that landed there had never been any `.forkflow.toml` blob, so they matched
            nothing in `upstream_config_digests`, read as this fork's own declaration, and
            opened `--merge` on the original project's settings. Both were found by an
            external reviewer, in the fixes for the round before.

            So the shape is pinned in two places rather than trusted to review.

            One: every copy this script prints for a user to run is built by `keep_aside`,
            whose destination is inside the git directory by construction. Nothing else in
            the script may spell a `cp` for a user at all.

            Two: the copy `keep_aside` names is a DESTINATION and never a source. That is
            exactly the shape the symlink remedy had - `cp -p -- <the copy> <the config>` -
            and the one thing that turns a copy-aside into a way of producing config bytes.
            `{sh_arg(keep)}` therefore appears in a printed command only at its end, and the
            name `keep` is reserved for that destination - `cmd_ship`'s branch of somebody
            else's commits is `saved` so this can be asked by name."""
            self.assertEqual(self.owners("cp -p --"), {"keep_aside"})
            self.assertEqual(self.owners("sh_arg(keep)"), {"keep_aside"})
            self.assertEqual(self.owners("keep_aside("),
                             {"keep_aside", "variant_remedy", "config_render_unprovable",
                              "config_rendered_before"})
            # the same rule again on the printed text itself, so a copy spelled some other
            # way is caught too. Adjacent f-string pieces are glued back together first:
            # every remedy here is written across several lines
            glued = re.sub(r'"\s*\n\s*f?"', "", "\n".join(self.lines[:self.limit]))
            for cmd in re.findall(r"`([^`\n]+)`", glued):
                words = cmd.split()
                for i, word in enumerate(words):
                    if "keep" in word and i != len(words) - 1:
                        self.fail("a printed command reads back the copy it made, which is "
                                  "how a remedy comes to produce config bytes: " + cmd)

        def test_no_printed_command_needs_a_git_newer_than_the_floor(self):
            """The README states the floor: Python 3.9+ and git 2.20+. Every command this
            script prints is pasted by a user exactly as printed, so a spelling their git
            does not have is a dead end in the middle of a recovery - and the spellings a
            hand reaches for without thinking are all newer than the floor: `switch` and
            `restore` are 2.23, `branch --show-current` is 2.22, `fetch --refetch` is 2.36,
            `ls-remote --symref` is 2.8, `for-each-ref --exclude` is 2.38.

            `merge-tree --write-tree` is the one thing here above the floor, and it is
            never reached on an older git: every call to `merge_tree` is behind
            `git_version() < MERGE_TREE_GIT`, and the README says the simulation wants
            2.38+ and is skipped with a note. `%(worktreepath)` (2.23) has its own
            fallback, tested in `test_without_worktreepath_only_this_worktree_is_seen`."""
            import ast
            body = "\n".join(self.lines[:self.limit])
            for spelling in ("git switch", "git restore", "branch --show-current",
                             "fetch --refetch", "ls-remote --symref",
                             "for-each-ref --exclude", "rev-list --exclude-hidden"):
                self.assertNotIn(spelling, body,
                                 spelling + " is newer than the git floor the README states")
            funcs = {n.name: n for n in ast.walk(self.tree)
                     if isinstance(n, ast.FunctionDef) and n.lineno < self.limit}
            callers = self.owners("merge_tree(") - {"merge_tree", "<module>"}
            self.assertEqual(callers, {"simulate_merge", "still_carries"})
            for name in callers:
                where = funcs[name]
                self.assertIn("MERGE_TREE_GIT",
                              "\n".join(self.lines[where.lineno - 1:where.end_lineno]), name)

        def test_the_state_file_is_written_in_one_place_and_under_its_lock(self):
            """Every record in the state file is a read-modify-write, and `pending` lives in
            the one file every worktree of the clone shares. `record_pending`,
            `forget_pending` and `write_state` each read it, changed one key and wrote it
            back on their own, so two worktrees recording at the same time each wrote the map
            the other had just changed away. The lock belongs to `change_state` and the write
            happens nowhere else, so a writer added later cannot go around it."""
            self.assertEqual(self.owners("save_state("), {"save_state", "change_state"})
            self.assertEqual(self.owners("take_state_lock("),
                             {"take_state_lock", "change_state"})
            # and the state file is PARSED in one place too, so "this file says nothing" and
            # "this file cannot be read" stay two different answers. A second parser is how
            # an unreadable file came to read as {} and forget the provenance memory
            self.assertEqual(self.owners("json.load("), {"load_state"})
            self.assertEqual(self.owners("load_state("),
                             {"load_state", "read_state", "state_unreadable", "change_state"})
            # a write over a file that cannot be read makes the damage permanent, so the
            # decision not to lives with the one writer
            self.assertEqual(self.owners("state_unreadable("),
                             {"state_unreadable", "config_memory_unprovable"})
            # nothing here may ever delete the lock file: the lock is the kernel's, on the
            # open descriptor, and a run that unlinks the path releases whatever a third
            # run holds by then. So the pathname is used in exactly one place - the open -
            # and the lock calls themselves are behind one helper
            self.assertEqual(self.owners("state_lock_path("),
                             {"state_lock_path", "take_state_lock"})
            self.assertEqual(self.owners("lock_exclusive("),
                             {"lock_exclusive", "take_state_lock"})
            self.assertEqual(self.owners("fcntl.flock("), {"lock_exclusive"})
            self.assertEqual(self.owners("msvcrt.locking("), {"lock_exclusive"})
            self.assertEqual(self.owners("drop_state_lock("),
                             {"drop_state_lock", "change_state"})
            self.assertEqual(self.owners("change_state("),
                             {"change_state", "write_state", "record_published",
                              "record_pending", "forget_pending",
                              "remember_upstream_configs", "remember_rendered_config"})

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
