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
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

CONFIG_FILE = ".forkflow.toml"
HOOK_MARK = "# forkflow pre-push hook"
DEFAULT_TRUNK = "develop"
DEFAULT_SYNC_PREFIX = "sync/"
DEFAULT_BACKUP_PREFIX = "backup/"
README_POINTER = "See README, *Adopting forkflow in an existing fork*"
GATE_TAIL = 12                       # lines of a failing gate command's output that are shown
MERGE_TREE_GIT = (2, 38)             # `git merge-tree --write-tree` - older git skips the simulation
BOTH_SIDES_SNIFF = 8192              # bytes of a blob looked at for a NUL before it is "binary"
BOTH_SIDES_WIDTH = 48                # column the both-sides table pads paths to

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


def short(sha: str) -> str:
    return sha[:8] if sha else "-"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def load_config(root: str) -> dict:
    """{} when absent. A config that exists but cannot be read is a hard failure:
    it carries the branch names every safety check depends on."""
    path = os.path.join(root, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        raise Fail(f"{CONFIG_FILE} needs Python 3.11+ (tomllib) to be read; "
                   f"this is Python {sys.version_info[0]}.{sys.version_info[1]}. "
                   f"Remove the file or run forkflow with a newer Python.")
    with open(path, "rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise Fail(f"{CONFIG_FILE}: {exc}")


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
    mirror_diverged: bool = False
    platform: str = "unknown"
    dry_run: bool = False
    sync_prefix: str = DEFAULT_SYNC_PREFIX
    backup_prefix: str = DEFAULT_BACKUP_PREFIX

    def up(self) -> str:
        return f"{self.upstream}/{self.upstream_branch}"

    def origin_ref(self, branch: str) -> str:
        return f"{self.origin}/{branch}"


def resolve_ctx(cwd: str, args: object = None, need_upstream: bool = True,
                need_trunk: bool = True, strict_mirror: bool = True) -> Ctx:
    """Build the Ctx and enforce the preconditions each subcommand needs."""
    cwd = os.path.abspath(cwd)
    if not git_ok("rev-parse", "--is-inside-work-tree", cwd=cwd):
        raise Fail(f"{cwd} is not inside a git repository")
    root = git("rev-parse", "--show-toplevel", cwd=cwd)
    cfg = load_config(root)
    remotes = git("remote", cwd=root).split()
    if "origin" not in remotes:
        raise Fail("no `origin` remote: forkflow expects the fork to be `origin`")

    name = getattr(args, "upstream", None) or cfg.get("upstream")
    others = [r for r in remotes if r != "origin"]
    if not name:
        name = others[0] if len(others) == 1 else None
    if not name or name not in remotes:
        if need_upstream:
            what = (f"remote `{name}` does not exist" if name else
                    ("several non-origin remotes (" + ", ".join(others) + ")" if others
                     else "no remote for the original project"))
            raise Fail(f"{what}; run `forkflow setup --upstream-url <URL>` "
                       f"(or `forkflow setup --upstream <NAME>` for an existing remote)")
        upstream = name or "upstream"
        upstream_url = ""
    else:
        upstream = name
        upstream_url = git("remote", "get-url", upstream, cwd=root, check=False)

    ub = getattr(args, "upstream_branch", None) or cfg.get("upstream_branch")
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
    if not ub:
        ub = "main"

    mirror = getattr(args, "mirror", None) or cfg.get("mirror") or ub
    trunk = getattr(args, "trunk", None) or cfg.get("trunk") or DEFAULT_TRUNK
    if trunk == mirror:
        raise Fail(f"trunk and mirror are both `{trunk}`: the trunk carries our work, "
                   f"the mirror is a pure copy of upstream - they cannot be the same branch")

    ctx = Ctx(root=root, cfg=cfg,
              origin_url=git("remote", "get-url", "origin", cwd=root, check=False),
              upstream=upstream, upstream_url=upstream_url, upstream_branch=ub,
              trunk=trunk, mirror=mirror,
              dry_run=bool(getattr(args, "dry_run", False)),
              sync_prefix=cfg.get("sync_prefix") or DEFAULT_SYNC_PREFIX,
              backup_prefix=cfg.get("backup_prefix") or DEFAULT_BACKUP_PREFIX)
    ctx.platform = detect_platform(ctx.origin_url)

    local_mirror = has_ref(root, f"refs/heads/{mirror}")
    remote_mirror = has_ref(root, f"refs/remotes/origin/{mirror}")
    if strict_mirror and not (local_mirror or remote_mirror):
        raise Fail(f"mirror `{mirror}` exists neither locally nor on origin - run `forkflow setup`")
    for ref in ([mirror] if local_mirror else []) + ([f"origin/{mirror}"] if remote_mirror else []):
        rc, _, _ = git_rc("merge-base", "--is-ancestor", ref, ctx.up(), cwd=root)
        if rc == 0:
            continue
        if rc == 1:
            ctx.mirror_diverged = True
            if strict_mirror:
                raise Fail(f"`{ref}` has commits that are not in `{ctx.up()}`: "
                           f"it cannot be the mirror. {README_POINTER}")
        else:
            if strict_mirror:
                raise Fail(f"`{ctx.up()}` is not fetched yet: run `git fetch {upstream}`")
            break

    if need_trunk and not has_ref(root, f"refs/remotes/origin/{trunk}"):
        raise Fail(f"trunk `{trunk}` is not on origin - run `forkflow setup`")
    return ctx


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def step(label: str, cmd: str, result: str, dry: bool = False) -> None:
    prefix = "  would: " if dry else "  "
    print(f"{prefix}{label}  $ {cmd}  -> {result}")


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
    """The subset of files that exists in upstream's branch - editing those costs merges."""
    out = []
    for f in files:
        if git_ok("cat-file", "-e", f"{ctx.up()}:{f}", cwd=ctx.root):
            out.append(f)
    return out


def divergence(ctx: Ctx) -> Tuple[Optional[int], Optional[int]]:
    """(files changed on origin/<trunk> since it left upstream, how many are upstream-tracked)."""
    trunk_ref = f"origin/{ctx.trunk}"
    if not rev(ctx.root, trunk_ref) or not rev(ctx.root, ctx.up()):
        return (None, None)
    mb = git("merge-base", ctx.up(), trunk_ref, cwd=ctx.root, check=False)
    if not mb:
        return (None, None)
    files = [f for f in git("diff", "--name-only", mb, trunk_ref, cwd=ctx.root).splitlines() if f]
    return (len(files), len(upstream_tracked(ctx, files)))


def branch_files(ctx: Ctx, ref: str = "HEAD") -> list:
    """Files the current branch changed since it left the trunk (upstream when there is no trunk)."""
    if not rev(ctx.root, ref):
        return []
    for base in (f"refs/remotes/{ctx.origin}/{ctx.trunk}", ctx.up()):
        if not rev(ctx.root, base):
            continue
        mb = git("merge-base", base, ref, cwd=ctx.root, check=False)
        if not mb:
            continue
        out = git("diff", "--name-only", mb, ref, cwd=ctx.root, check=False)
        return [f for f in out.splitlines() if f]
    return []


def current_branch(ctx: Ctx) -> str:
    """Short name of the checked-out branch, "" when HEAD is detached."""
    return git("symbolic-ref", "-q", "--short", "HEAD", cwd=ctx.root, check=False)


def warn_upstream_tracked(ctx: Ctx) -> list:
    """Print (and return) the upstream-tracked files this branch touches. Never a failure:
    editing them is a permanent merge cost that is sometimes the right call."""
    touched = upstream_tracked(ctx, branch_files(ctx))
    if touched:
        print(f"  touches upstream-tracked files (WARNING, {len(touched)}):")
        for f in touched:
            print(f"    {f}")
    return touched


def hooks_path(root: str) -> str:
    """Absolute hooks directory of this worktree (follows core.hooksPath)."""
    p = git("rev-parse", "--git-path", "hooks", cwd=root, check=False)
    if not p:
        return ""
    return p if os.path.isabs(p) else os.path.join(root, p)


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
    path = git("rev-parse", "--git-path", "FETCH_HEAD", cwd=ctx.root, check=False)
    if path and not os.path.isabs(path):
        path = os.path.join(ctx.root, path)
    if not path or not os.path.exists(path):
        return "never"
    return _rel_age(max(0.0, time.time() - os.path.getmtime(path)))


def header(ctx: Ctx, sub: str) -> None:
    print(f"forkflow {sub}  origin={ctx.origin_url or '-'}  "
          f"upstream={ctx.upstream_url or '-'}  platform={ctx.platform}")
    print(f"  as of last fetch: {last_fetch(ctx)}")

    up_sha = rev(ctx.root, ctx.up())
    local_m = rev(ctx.root, f"refs/heads/{ctx.mirror}")
    origin_m = rev(ctx.root, f"refs/remotes/origin/{ctx.mirror}")
    ours = f"refs/heads/{ctx.mirror}" if local_m else f"refs/remotes/origin/{ctx.mirror}"
    if not up_sha:
        vs_up = "unfetched"
    elif not (local_m or origin_m):
        vs_up = "no mirror"
    else:
        ahead, behind = ahead_behind(ctx, ours, ctx.up())
        if ahead is None:
            vs_up = "?"
        elif ahead and behind:
            vs_up = "DIVERGED"
        elif ahead:
            vs_up = "DIVERGED"
        elif behind:
            vs_up = f"mirror behind by {behind}"
        else:
            vs_up = "="
    if not origin_m:
        vs_origin = "-"
    elif local_m and origin_m != local_m:
        ahead, behind = ahead_behind(ctx, f"refs/heads/{ctx.mirror}", f"refs/remotes/origin/{ctx.mirror}")
        vs_origin = f"unpushed {ahead}" if ahead else f"behind {behind}"
    else:
        vs_origin = "="
    print(f"  mirror  {ctx.mirror} {short(local_m)}   "
          f"origin/{ctx.mirror} {short(origin_m)} ({vs_origin})   "
          f"{ctx.up()} {short(up_sha) if up_sha else 'unfetched'} ({vs_up})")

    local_t = rev(ctx.root, f"refs/heads/{ctx.trunk}")
    origin_t = rev(ctx.root, f"refs/remotes/origin/{ctx.trunk}")
    if not origin_t:
        vs_origin_t = "missing"
    elif not local_t:
        vs_origin_t = "-"
    elif local_t == origin_t:
        vs_origin_t = "="
    else:
        ahead, behind = ahead_behind(ctx, f"refs/heads/{ctx.trunk}", f"refs/remotes/origin/{ctx.trunk}")
        vs_origin_t = f"+{ahead}/-{behind}"
    if origin_t and up_sha:
        ahead, behind = ahead_behind(ctx, ctx.up(), f"refs/remotes/origin/{ctx.trunk}")
        vs_up_t = f"+{ahead}/-{behind} vs origin/{ctx.trunk}"
    else:
        vs_up_t = f"vs origin/{ctx.trunk}: unknown"
    print(f"  trunk   {ctx.trunk} {short(local_t)}   "
          f"origin/{ctx.trunk} {short(origin_t) if origin_t else 'missing'} ({vs_origin_t})   "
          f"{ctx.up()} ({vs_up_t})")

    n_files, n_tracked = divergence(ctx)
    if n_files is None:
        print("  divergence: unknown (fetch upstream and create the trunk first)")
    else:
        print(f"  divergence: {n_files} files, {n_tracked} upstream-tracked")


# --------------------------------------------------------------------------- #
# the three push helpers - nothing else in this script pushes
# --------------------------------------------------------------------------- #

def push(ctx: Ctx, branch: str, lease: Optional[str] = None) -> str:
    """Push a feature/sync/backup branch. Never the trunk, never the mirror, never --force."""
    if branch == ctx.trunk:
        raise Fail(f"refusing to push the trunk `{branch}`: it is only ever reached "
                   f"through a merge request")
    if branch == ctx.mirror:
        raise Fail(f"refusing to push the mirror `{branch}` from here: only `sync` moves it, "
                   f"and only forward to {ctx.up()}")
    args = ["push"]
    if lease:
        args.append(f"--force-with-lease={branch}:{lease}")
    if not has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{branch}"):
        args.append("-u")
    args += [ctx.origin, f"{branch}:refs/heads/{branch}"]
    cmd = "git " + " ".join(args)
    if ctx.dry_run:
        step("push", cmd, "not run (dry run)", dry=True)
        return cmd
    rc, _, err = git_rc(*args, cwd=ctx.root)
    if rc != 0:
        step("push", cmd, "REJECTED")
        raise Fail(f"push of `{branch}` was rejected by {ctx.origin}:\n{err.strip()}", 5)
    step("push", cmd, "pushed")
    return cmd


def push_mirror(ctx: Ctx) -> str:
    """The only way the mirror reaches origin: never forced, only a pure copy of upstream."""
    m = ctx.mirror
    if not has_ref(ctx.root, f"refs/heads/{m}"):
        raise Fail(f"no local `{m}` to push")
    rc, _, _ = git_rc("merge-base", "--is-ancestor", m, ctx.up(), cwd=ctx.root)
    if rc == 1:
        raise Fail(f"`{m}` is not a pure copy of `{ctx.up()}`: refusing to push the mirror. "
                   f"{README_POINTER}")
    if rc != 0:
        raise Fail(f"cannot verify `{m}` against `{ctx.up()}`: run `git fetch {ctx.upstream}`")
    if has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{m}"):
        rc, _, _ = git_rc("merge-base", "--is-ancestor", f"{ctx.origin}/{m}", m, cwd=ctx.root)
        if rc != 0:
            raise Fail(f"`{ctx.origin}/{m}` is not an ancestor of the local `{m}`: "
                       f"fetch and fast-forward first - the mirror is never forced")
    args = ["push", ctx.origin, f"{m}:refs/heads/{m}"]
    cmd = "git " + " ".join(args)
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
            raise Fail(f"cannot compare `{m}` with {short(target)}: run `git fetch {ctx.upstream}`")
    wt = mirror_worktree(ctx)
    here = wt and os.path.realpath(wt) == os.path.realpath(ctx.root)
    if wt and not here:
        raise Fail(f"mirror `{m}` is checked out in {wt}: advance it there, "
                   f"or remove that worktree")
    if old == target:
        step("mirror", f"git rev-parse {m}", f"up to date at {short(target)}")
        return (old, old)
    if not old:
        cmd = f"git branch --no-track {m} {short(target)}"
    elif here:
        cmd = f"git merge --ff-only {short(target)}"
    else:
        cmd = f"git update-ref refs/heads/{m} {short(target)}"
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
    cmd = (f"git branch --no-track {t} {short(target)} && "
           f"git push {ctx.origin} {t}:refs/heads/{t}")
    if ctx.dry_run:
        step("trunk", cmd, f"would create {t} at {short(target)} and push it", dry=True)
        return
    git("branch", "--no-track", t, target, cwd=ctx.root)
    rc, _, err = git_rc("push", ctx.origin, f"{t}:refs/heads/{t}", cwd=ctx.root)
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
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{ctx.backup_prefix}{stamp}-{reason}"


def backup(ctx: Ctx, reason: str, from_ref: str) -> str:
    """Create a restore point and prove it reached origin before anything is rewritten.

    Not confirmed by `ls-remote` means not a backup: the local branch is removed again and
    the caller stops with exit 5 rather than rewriting on the strength of a failed push."""
    name = backup_name(ctx, reason)
    src = rev(ctx.root, from_ref)
    if not src:
        raise Fail(f"cannot back up `{from_ref}`: it does not resolve to a commit")
    create = f"git branch {name} {from_ref}"
    purpose = BACKUP_PURPOSE.get(reason, "")

    if ctx.dry_run:
        step("backup", create, f"would keep {short(src)} as {name}", dry=True)
        print(f"    rollback: git reset --hard {ctx.origin}/{name}")
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

    confirm = f"git ls-remote --heads {ctx.origin} refs/heads/{name}"
    rc, out, err = git_rc("ls-remote", "--heads", ctx.origin, f"refs/heads/{name}", cwd=ctx.root)
    if rc != 0 or f"refs/heads/{name}" not in out:
        step("backup", confirm, "NOT CONFIRMED")
        git("branch", "-D", name, cwd=ctx.root, check=False)
        raise Fail(f"backup `{name}` is not on {ctx.origin} after the push"
                   f"{': ' + err.strip() if err.strip() else ''}: refusing to go on "
                   f"without a confirmed restore point", 5)
    step("backup", confirm, "confirmed on origin")
    print(f"    rollback: git reset --hard {ctx.origin}/{name}")
    if purpose:
        print(f"    {purpose}")
    return name


def simulate_merge(ctx: Ctx, target: str) -> Optional[Tuple[bool, list]]:
    """Predict the sync merge without touching the worktree: (clean, conflicting paths).

    None when git is too old to simulate - the merge itself is then the first answer."""
    cmd = f"git merge-tree --write-tree --name-only {ctx.origin}/{ctx.trunk} {short(target)}"
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
        step("simulate", cmd, f"{len(conflicts)} conflicting file(s)")
        for f in conflicts:
            print(f"    {f}")
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
        for line in tail_lines(p.stderr.decode("utf-8", "replace"), GATE_TAIL):
            print(f"      {line}")
        print(f"    description: {path}")
        return shown
    step("mr", shown, "created")
    for line in out.splitlines():
        print(f"    {line}")
    return shown


# --------------------------------------------------------------------------- #
# subcommands
# --------------------------------------------------------------------------- #

def cmd_status(args: argparse.Namespace) -> int:
    """Read-only. Without --fetch it makes no network call and writes nothing."""
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=False, strict_mirror=False)
    fetch_result = None
    if getattr(args, "fetch", False):
        rc, _, err = git_rc("fetch", "--multiple", ctx.origin, ctx.upstream, cwd=ctx.root)
        tail = err.strip().splitlines()[-1] if err.strip() else "see git output"
        fetch_result = ("remote-tracking refs refreshed" if rc == 0
                        else f"FAILED, reporting the refs on disk: {tail}")

    header(ctx, "status")
    if fetch_result:
        step("fetch", f"git fetch --multiple {ctx.origin} {ctx.upstream}", fetch_result)

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
    print(f"  setup    upstream push: {upstream_push(ctx)}   pre-push hook: {hook}   "
          f"ff-only: {ctx.trunk} {'yes' if ff_only(ctx, ctx.trunk) else 'no'}, "
          f"{ctx.mirror} {'yes' if ff_only(ctx, ctx.mirror) else 'no'}")

    todo = []
    if not has_ref(ctx.root, f"refs/remotes/{ctx.origin}/{ctx.trunk}"):
        todo.append(f"trunk `{ctx.trunk}` is not on origin")
    if hook != "installed":
        todo.append(f"pre-push hook {hook}")
    if not upstream_push(ctx).startswith("DISABLED"):
        todo.append(f"`{ctx.upstream}` still has a live push URL")
    if not (ff_only(ctx, ctx.trunk) and ff_only(ctx, ctx.mirror)):
        todo.append("ff-only merge config not set")
    if todo:
        print("  hint     run `forkflow setup`: " + "; ".join(todo))
    if ctx.mirror_diverged:
        print(f"  hint     mirror `{ctx.mirror}` has commits that are not in `{ctx.up()}`. "
              f"{README_POINTER}")
    return 0


def gate_commands(ctx: Ctx) -> list:
    """`gate = [...]` from the config; a bare string is accepted as a single command."""
    gate = ctx.cfg.get("gate") or []
    if isinstance(gate, str):
        gate = [gate]
    if not isinstance(gate, list) or any(not isinstance(c, str) for c in gate):
        raise Fail(f"{CONFIG_FILE}: `gate` must be a list of shell commands")
    return [c for c in gate if c.strip()]


def run_check(ctx: Ctx) -> int:
    """The preflight `sync` and `ship` run, and what `status` surfaces for humans.

    Read-only. 0 when every invariant holds, 3 when one does not; the upstream-tracked
    warning is advisory and never changes the code."""
    failures = []
    warn_upstream_tracked(ctx)

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
        for line in tail_lines(out, GATE_TAIL):
            print(f"      {line}")
        failures.append(f"gate `{cmd}` exited {rc}")
        break                                 # the first failure is the one to fix

    trunk_ref = f"{ctx.origin}/{ctx.trunk}"
    tip_cmd = f"git merge-base --is-ancestor {trunk_ref} HEAD"
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
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    return f"{ctx.sync_prefix}{ctx.upstream}-{stamp}"


def fetch_both(ctx: Ctx) -> None:
    """Refresh both remotes and report what moved; every later step reads these refs."""
    refs = [ctx.up(), f"{ctx.origin}/{ctx.trunk}", f"{ctx.origin}/{ctx.mirror}"]
    before = dict((r, rev(ctx.root, r)) for r in refs)
    cmd = f"git fetch --multiple {ctx.origin} {ctx.upstream}"
    rc, _, err = git_rc("fetch", "--multiple", ctx.origin, ctx.upstream, cwd=ctx.root)
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    moved = []
    for r in refs:
        now = rev(ctx.root, r)
        moved.append(f"{r} unchanged" if now == before[r]
                     else f"{r} {short(before[r])}..{short(now)}")
    step("fetch", cmd, ", ".join(moved))


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
    """(added, removed) non-blank stripped lines of `git diff <mb> <side> -- <path>`."""
    rc, out, _ = git_rc("diff", "--no-color", "--no-ext-diff", "--no-renames",
                        mb, side, "--", path, cwd=ctx.root)
    added, removed = [], []
    for line in out.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            bucket = added
        elif line.startswith("-"):
            bucket = removed
        else:
            continue
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
        out = git("diff", "--name-only", "--no-renames", mb, side, cwd=ctx.root, check=False)
        changed.append(set(f for f in out.splitlines() if f))
        status = git("diff", "--name-status", "--find-renames", mb, side,
                     cwd=ctx.root, check=False)
        for line in status.splitlines():
            fields = line.split("\t")
            if fields and fields[0].startswith("R"):
                renamed.update(f for f in fields[1:] if f)
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


def make_sync_branch(ctx: Ctx, name: str, force: bool) -> None:
    """Create the sync branch off `origin/<trunk>` and switch to it. Never off the local trunk:
    the MR has to apply to what is published."""
    base = f"{ctx.origin}/{ctx.trunk}"
    cmd = f"git checkout --no-track -b {name} {base}"
    exists = has_ref(ctx.root, f"refs/heads/{name}")
    if exists and not force:
        raise Fail(f"branch `{name}` already exists: resume it with `forkflow sync --continue`, "
                   f"or recreate it from {base} with `forkflow sync --force`")
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
    unmerged = [f for f in git("diff", "--name-only", "--diff-filter=U",
                               cwd=ctx.root, check=False).splitlines() if f]
    if not unmerged:
        raise Fail(f"merge of {short(target)} failed:\n{(err or out).strip()}")
    step("merge", cmd, f"{len(unmerged)} conflicting file(s)")
    for f in unmerged:
        print(f"    {f}")
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
                  f"Rollback: `git reset --hard {ctx.origin}/{backup_ref}`"]
    lines += ["", f"Merge button: {merge_button(ctx, 'sync')}."]
    return "\n".join(lines)


def merge_in_progress(ctx: Ctx) -> bool:
    """True while a merge is resolved but not committed (`MERGE_HEAD` still there)."""
    path = git("rev-parse", "--git-path", "MERGE_HEAD", cwd=ctx.root, check=False)
    if path and not os.path.isabs(path):
        path = os.path.join(ctx.root, path)
    return bool(path) and os.path.exists(path)


def unmerged_paths(ctx: Ctx) -> list:
    """Paths still carrying conflict markers."""
    out = git("diff", "--name-only", "--diff-filter=U", cwd=ctx.root, check=False)
    return [f for f in out.splitlines() if f]


def last_backup(ctx: Ctx, reason: str) -> str:
    """Newest local backup branch of this kind - what `--continue` reports without state."""
    out = git("for-each-ref", "--format=%(refname:short)",
              f"refs/heads/{ctx.backup_prefix}*-{reason}", cwd=ctx.root, check=False)
    names = sorted([ln for ln in out.splitlines() if ln], reverse=True)
    return names[0] if names else ""


def finish_sync(ctx: Ctx, args: argparse.Namespace, name: str, commits: Sequence[str],
                rows: Sequence[Tuple[str, str]], mirror_move: Tuple[str, str],
                backup_ref: str) -> int:
    """check -> push -> merge request: the tail both `sync` and `sync --continue` run."""
    hint = "  fix that, then: forkflow sync --continue"
    if ctx.dry_run:
        step("check", "forkflow check", "not run (dry run)", dry=True)
    elif run_check(ctx) == 3:
        print(hint)
        return 3

    try:
        push(ctx, name)
    except Fail as exc:
        if exc.code == 5:
            print(hint)
        raise

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    title = getattr(args, "title", None) or f"sync: {ctx.up()} {stamp} ({len(commits)} commits)"
    open_mr(ctx, name, title, sync_body(ctx, commits, rows, mirror_move, backup_ref),
            bool(getattr(args, "mr", False)))
    print(f"  after the MR is merged: git fetch {ctx.origin} && git switch {ctx.trunk} "
          f"&& git merge --ff-only {ctx.origin}/{ctx.trunk}")
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
        step("continue", "git diff --name-only --diff-filter=U",
             f"{len(unmerged)} file(s) still unmerged")
        for f in unmerged:
            print(f"    {f}")
        raise Fail("resolve the conflicts and `git add` them, "
                   "then run `forkflow sync --continue` again")

    merging = merge_in_progress(ctx)
    parents = git("rev-list", "--parents", "-n", "1", "HEAD", cwd=ctx.root, check=False).split()
    committed = len(parents) >= 3
    if not merging and not committed:
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
            committed = True
            step("continue", cmd, f"merge commit {short(rev(ctx.root, 'HEAD'))}")
    else:
        step("continue", "git rev-parse MERGE_HEAD", "the merge is already committed")

    if committed:
        log = git("log", "--oneline", "--no-decorate", "HEAD^1..HEAD^2",
                  cwd=ctx.root, check=False)
        commits = [ln for ln in log.splitlines() if ln]
        rows = both_sides_survived(ctx, "HEAD^1", "HEAD^2")
    else:                                   # dry run over an uncommitted merge
        step("verify", "git diff HEAD^1 / HEAD^2",
             "not run (dry run: the merge is not committed)", dry=True)
        commits, rows = [], []

    mirror_sha = rev(ctx.root, f"refs/heads/{ctx.mirror}")
    return finish_sync(ctx, args, name, commits, rows, (mirror_sha, mirror_sha),
                       last_backup(ctx, "pre-sync"))


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
    print(f"  leaving `{branch}`, switching to `{name}` "
          f"(you stay on it when this finishes; the trunk is never touched)")

    fetch_both(ctx)
    target = rev(ctx.root, ctx.up())
    if not target:
        raise Fail(f"`{ctx.up()}` does not resolve after the fetch: is `{ctx.upstream}` right?")
    step("target", f"git rev-parse {ctx.up()}", short(target))

    # the mirror first: it rewrites nothing and is useful on its own, and a failure here
    # leaves the trunk untouched. Everything after this reads `target`, not the mirror branch,
    # so a dry run previews the pending merge even though it moves nothing.
    mirror_move = advance_mirror(ctx, target)
    if rev(ctx.root, f"refs/remotes/{ctx.origin}/{ctx.mirror}") == target:
        step("mirror push", f"git push {ctx.origin} {ctx.mirror}", "up to date")
    else:
        push_mirror(ctx)

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
    step("commits", f"git log --oneline {ctx.origin}/{ctx.trunk}..{short(target)}",
         f"{len(commits)} upstream commit(s) to take")
    for line in commits:
        print(f"    {line}")

    backup_ref = backup(ctx, "pre-sync", f"{ctx.origin}/{ctx.trunk}")
    make_sync_branch(ctx, name, force=bool(getattr(args, "force", False)))
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
        path = git("rev-parse", "--git-path", name, cwd=ctx.root, check=False)
        if path and not os.path.isabs(path):
            path = os.path.join(ctx.root, path)
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
    if not clean_tree(ctx):
        raise Fail("the working tree has uncommitted changes: commit or stash them first")
    return branch


def rebase_onto(ctx: Ctx, branch: str, trunk_ref: str) -> None:
    """Rebase locally so the trunk can fast-forward: `rebase locally, merge globally`."""
    cmd = f"git rebase {trunk_ref}"
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
    step("rebase", cmd, f"{len(unmerged)} conflicting file(s)")
    for f in unmerged:
        print(f"    {f}")
    raise Fail(f"resolve the conflicts, `git add` them, run `git rebase --continue`, "
               f"then `forkflow ship --continue`", 4)


def commit_records(ctx: Ctx, base: str, ref: str = "HEAD") -> list:
    """(sha, subject, body) of `base..ref`, oldest first - what the squash message is built of.

    Read through git_rc: the separators are ASCII whitespace to Python, so `git()`'s strip()
    would eat the last record's field separator and lose that commit."""
    rc, out, _ = git_rc("log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e",
                        f"{base}..{ref}", cwd=ctx.root)
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


def squash_message(ctx: Ctx, records: Sequence[Tuple[str, str, str]],
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
        return git("log", "-1", "--format=%B", "HEAD", cwd=ctx.root) + "\n"
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
    mb = git("merge-base", trunk_ref, "HEAD", cwd=ctx.root)
    records = commit_records(ctx, mb)
    step("commits", f"git log --oneline {trunk_ref}..HEAD",
         f"{len(records)} commit(s) to squash into one")
    for sha, subject, _ in records:
        print(f"    {short(sha)} {subject}")

    message = squash_message(ctx, records, getattr(args, "message_file", None))
    squash(ctx, mb, message)

    rollback = f"  rollback: git reset --hard {ctx.origin}/{backup_ref}" if backup_ref else ""
    touched = upstream_tracked(ctx, branch_files(ctx))
    if ctx.dry_run:
        step("check", "forkflow check", "not run (dry run)", dry=True)
    elif run_check(ctx) == 3:
        if rollback:
            print(rollback)
        return 3

    try:
        push(ctx, branch, lease=lease or None)
    except Fail as exc:
        if exc.code == 5 and rollback:
            print(rollback)
        raise

    title = (getattr(args, "title", None)
             or (message.strip().splitlines() or [f"ship {branch}"])[0])
    open_mr(ctx, branch, title, ship_body(ctx, message, touched),
            bool(getattr(args, "mr", False)))
    print(f"  after the MR is merged: git fetch {ctx.origin} && git switch {ctx.trunk} "
          f"&& git merge --ff-only {ctx.origin}/{ctx.trunk}")
    if ctx.platform == "github":
        print(f"    then delete the local `{branch}`: "
              f'"Rebase and merge" rewrites the commit, so the local branch is a stale copy')
    return 0


def cmd_ship(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=True, strict_mirror=True)
    header(ctx, "ship")
    branch = ship_preflight(ctx)
    trunk_ref = f"{ctx.origin}/{ctx.trunk}"

    if getattr(args, "cont", False):
        cmd = f"git merge-base --is-ancestor {trunk_ref} HEAD"
        rc, _, _ = git_rc("merge-base", "--is-ancestor", trunk_ref, "HEAD", cwd=ctx.root)
        if rc != 0:
            step("continue", cmd, f"`{branch}` is not on {trunk_ref}'s tip")
            raise Fail("the rebase did not complete; run `forkflow ship` again")
        step("continue", cmd, "the rebase completed - resuming at the squash")
        return finish_ship(ctx, args, branch, last_backup(ctx, "pre-ship"),
                           rev(ctx.root, f"refs/remotes/{ctx.origin}/{branch}"))

    cmd = f"git fetch {ctx.origin}"
    before = rev(ctx.root, trunk_ref)
    rc, _, err = git_rc("fetch", ctx.origin, cwd=ctx.root)
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed: {err.strip()}")
    now = rev(ctx.root, trunk_ref)
    step("fetch", cmd, f"{trunk_ref} unchanged" if now == before
         else f"{trunk_ref} {short(before)}..{short(now)}")

    rc, _, _ = git_rc("merge-base", "--is-ancestor", "HEAD", trunk_ref, cwd=ctx.root)
    if rc == 0:
        print(f"  nothing to ship: `{branch}` has no commits beyond {trunk_ref}")
        return 0

    # the lease is what the fetch just saw; the rebase and the squash come after it
    lease = rev(ctx.root, f"refs/remotes/{ctx.origin}/{branch}")
    backup_ref = backup(ctx, "pre-ship", "HEAD")
    rebase_onto(ctx, branch, trunk_ref)
    if not ctx.dry_run and rev(ctx.root, "HEAD") == now:
        print(f"  nothing to ship: every commit of `{branch}` is already on {trunk_ref} "
              f"(the backup `{backup_ref}` still holds the branch as it was)")
        return 0
    return finish_ship(ctx, args, branch, backup_ref, lease)


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #

# key = value lines of `.forkflow.toml`, commented or not, with whatever trails them
CONFIG_KEY_LINE = re.compile(r'^\s*#?\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
                             r'(?P<val>"[^"]*"|\[[^\]]*\]|\S*)(?P<rest>.*)$')


def template_text(ctx: Ctx) -> str:
    """The commented `.forkflow.toml` `setup` drops in: every key optional, defaults shown."""
    return "\n".join([
        "# forkflow - every key is optional; the values below are what this clone resolves to.",
        "# Reading this file needs Python 3.11+ (tomllib); a file that cannot be read is fatal.",
        "",
        f'# upstream = "{ctx.upstream}"                # remote name of the original project',
        f'# upstream_branch = "{ctx.upstream_branch}"  # its branch we track (default: its HEAD)',
        f'# mirror = "{ctx.mirror}"                    # our fast-forward-only copy of it',
        f'# trunk = "{ctx.trunk}"                      # protected, MR-only branch with our work',
        '# gate = []                                  # e.g. ["make test", "terraform fmt"]',
        f'# sync_prefix = "{ctx.sync_prefix}"',
        f'# backup_prefix = "{ctx.backup_prefix}"',
        "",
    ]) + "\n"


def config_with_keys(text: str, pairs: Sequence[Tuple[str, str]]) -> str:
    """`text` with each key set: an existing line (commented or not) is rewritten in place,
    keeping the comment that trails it; every other line is left exactly as it was."""
    lines = text.splitlines()
    for key, value in pairs:
        entry = f'{key} = "{value}"'
        for i, line in enumerate(lines):
            m = CONFIG_KEY_LINE.match(line)
            if m and m.group("key") == key:
                lines[i] = entry + m.group("rest")
                break
        else:
            lines.append(entry)
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
        step("remote", f"git remote get-url {name}", f"{name} -> {current or '-'}")
        if url and url != current:
            print(f"    note: `{name}` already points at {current}; --upstream-url is ignored - "
                  f"change it with `git remote set-url {name} {url}` if that is what you want")
        if name != "upstream":
            print(f"    note: the original project is the remote `{name}`, not `upstream`; "
                  f"forkflow uses it as it is - `git remote rename {name} upstream` if you "
                  f"prefer the usual name")
        return name

    if not url:
        what = (f"remote `{name}` does not exist" if name else
                ("several non-origin remotes (" + ", ".join(others) + ")" if others
                 else "no remote for the original project"))
        raise Fail(f"{what}; run `forkflow setup --upstream-url <URL>` "
                   f"(or `forkflow setup --upstream <NAME>` for an existing remote)")
    name = name or "upstream"
    cmd = f"git remote add {name} {url}"
    if ctx.dry_run:
        step("remote", cmd, "would add the remote of the original project", dry=True)
        return None
    git("remote", "add", name, url, cwd=ctx.root)
    step("remote", cmd, "added")
    return name


def setup_fetch(ctx: Ctx, upstream: str) -> None:
    """Both remotes, then their HEADs. A failure here has changed nothing yet."""
    cmd = f"git fetch --multiple {ctx.origin} {upstream}"
    rc, _, err = git_rc("fetch", "--multiple", ctx.origin, upstream, cwd=ctx.root)
    if rc != 0:
        step("fetch", cmd, "FAILED")
        raise Fail(f"fetch failed - nothing has been changed yet:\n{err.strip()}")
    step("fetch", cmd, "remote-tracking refs refreshed")
    for remote in (ctx.origin, upstream):
        sub = f"git remote set-head {remote} -a"
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
        cmd = f"git branch --no-track {m} {short(target)}"
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
            raise Fail(f"cannot compare `{ref}` with `{ctx.up()}`: run `git fetch {ctx.upstream}`")
    step("mirror", f"git merge-base --is-ancestor {m} {ctx.up()}",
         f"`{m}` is a pure copy of `{ctx.up()}` (behind is fine - `forkflow sync` advances it)")


def setup_trunk(ctx: Ctx, target: str) -> None:
    """Create the trunk on a fresh fork. Runs before the hook, which refuses every push
    of the trunk - creation included."""
    t = ctx.trunk
    origin_t = rev(ctx.root, f"refs/remotes/{ctx.origin}/{t}")
    if origin_t:
        step("trunk", f"git rev-parse {ctx.origin}/{t}",
             f"already on {ctx.origin} at {short(origin_t)}")
        return
    if has_ref(ctx.root, f"refs/heads/{t}"):
        raise Fail(f"trunk `{t}` exists locally but not on {ctx.origin}: forkflow never pushes "
                   f"the trunk - push it yourself once it is what you want "
                   f"(`git push -u {ctx.origin} {t}`), or delete it and rerun `forkflow setup`")
    bootstrap_trunk(ctx, target)


def setup_push_url(ctx: Ctx) -> None:
    """`DISABLED` makes `git push <upstream>` fail before any hook can even run."""
    cmd = f"git remote set-url --push {ctx.upstream} DISABLED"
    if git("remote", "get-url", "--push", ctx.upstream, cwd=ctx.root, check=False) == "DISABLED":
        step("push url", cmd, "already DISABLED")
        return
    if ctx.dry_run:
        step("push url", cmd, f"would stop every push to `{ctx.upstream}`", dry=True)
        return
    git("remote", "set-url", "--push", ctx.upstream, "DISABLED", cwd=ctx.root)
    step("push url", cmd, f"pushes to `{ctx.upstream}` now fail")


def setup_git_config(ctx: Ctx) -> None:
    """ff-only for the two long-lived branches is rule 3 and rule 6 in git's own hands."""
    settings = [
        (f"branch.{ctx.trunk}.mergeOptions", "--ff-only", "rule: the trunk only fast-forwards"),
        (f"branch.{ctx.mirror}.mergeOptions", "--ff-only", "rule: the mirror only fast-forwards"),
        ("pull.ff", "only", "rule: a pull never creates a merge commit"),
        ("rerere.enabled", "true", "convenience, not a rule: remembers conflict resolutions"),
    ]
    for key, value, why in settings:
        cmd = f"git config {key} {value}"
        if git("config", "--get", key, cwd=ctx.root, check=False) == value:
            step("config", cmd, f"already set ({why})")
            continue
        if ctx.dry_run:
            step("config", cmd, f"would set it ({why})", dry=True)
            continue
        git("config", key, value, cwd=ctx.root)
        step("config", cmd, why)


def setup_template(ctx: Ctx) -> None:
    """A commented `.forkflow.toml`, left untracked: the branch names are the fork's decision."""
    path = os.path.join(ctx.root, CONFIG_FILE)
    cmd = f"write {CONFIG_FILE}"
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
    args.upstream = name
    setup_fetch(ctx, name)
    # the upstream branch (and with it the default mirror name) is only known after the fetch
    ctx = resolve_ctx(ctx.root, args, need_upstream=True, need_trunk=False, strict_mirror=False)
    step("names", "-", f"upstream={ctx.up()}  mirror={ctx.mirror}  trunk={ctx.trunk}")

    pairs = [(key, getattr(ctx, key)) for key in ("trunk", "mirror")
             if getattr(args, key, None)]
    if pairs:
        write_config_keys(ctx, pairs)

    target = rev(ctx.root, ctx.up())
    if not target:
        raise Fail(f"`{ctx.up()}` does not resolve after the fetch: is `{name}` the right remote?")
    step("target", f"git rev-parse {ctx.up()}", short(target))

    setup_mirror(ctx, target)
    setup_trunk(ctx, target)
    setup_push_url(ctx)
    setup_git_config(ctx)
    setup_template(ctx)
    return 0


COMMANDS = {
    "status": cmd_status,
    "check": cmd_check,
    "sync": cmd_sync,
    "ship": cmd_ship,
    "setup": cmd_setup,
}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    # SUPPRESS keeps a subparser from clobbering a flag given before the subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", dest="dir", default=argparse.SUPPRESS, metavar="DIR",
                        help="repository directory (default: cwd)")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="report what would happen; change nothing")
    common.add_argument("--force", action="store_true", default=argparse.SUPPRESS,
                        help="recreate an existing branch / replace a foreign hook")

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
    if "--test" in argv:
        run_tests()                      # exits
    try:
        args = parse_args(argv)
        return COMMANDS[args.cmd](args)
    except Fail as exc:
        print(f"forkflow: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------- #
# embedded tests
# --------------------------------------------------------------------------- #

def run_tests() -> None:
    """Run the embedded unit tests (`forkflow.py --test`) and exit."""
    import contextlib
    import io
    import tempfile
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
        os.environ.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": gitconfig,
            "HOME": tmp,
            "GIT_AUTHOR_NAME": "forkflow tests",
            "GIT_AUTHOR_EMAIL": "tests@example.invalid",
            "GIT_COMMITTER_NAME": "forkflow tests",
            "GIT_COMMITTER_EMAIL": "tests@example.invalid",
            "GIT_EDITOR": "true",
        })
        try:
            yield
        finally:
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

    def sync_branch_name(remote: str = "upstream") -> str:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
        return "%s%s-%s" % (DEFAULT_SYNC_PREFIX, remote, stamp)

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
            self.assertFalse(ctx.mirror_diverged)

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
            self.assertTrue(ctx.mirror_diverged)

        def test_unfetched_upstream(self):
            fork = make_fork(self.tmp)
            sh("git", "symbolic-ref", "-d", "refs/remotes/upstream/HEAD", cwd=fork)
            sh("git", "update-ref", "-d", "refs/remotes/upstream/main", cwd=fork)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("not fetched", str(cm.exception))
            self.assertFalse(ctx_for(fork, strict_mirror=False).mirror_diverged)

        def test_trunk_missing_on_origin(self):
            fork = make_fresh_fork(self.tmp)
            with self.assertRaises(Fail) as cm:
                ctx_for(fork)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("forkflow setup", str(cm.exception))
            ctx = ctx_for(fork, need_trunk=False)
            self.assertEqual(ctx.trunk, "develop")

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
            _, out, _ = capture(push, ctx, "feat/x", head)
            self.assertIn("--force-with-lease=feat/x:%s" % head, out)
            self.assertNotIn(" -u ", out)          # already on origin: no upstream to set

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
            _, out, _ = run("-C", fork, "status")
            self.assertIn("backups  0 (refs/remotes/origin/backup/*)", out)

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
            fork = make_fork(self.tmp, config="gate = 3\n")
            code, out, err = run("-C", fork, "check")
            self.assertEqual(code, 2)
            self.assertIn("list of shell commands", err)

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

        def test_refs_moving_between_the_runs_does_not_change_the_table(self):
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

        def test_a_non_standard_remote_name_is_used_as_it_is(self):
            fork = make_fork(self.tmp)
            sh("git", "remote", "rename", "upstream", "original", cwd=fork)
            code, out, err = run("-C", fork, "setup")
            self.assertEqual(code, 0, err + out)
            self.assertIn("git remote rename original upstream", out)
            self.assertEqual(self.push_url(fork, "original"), "DISABLED")
            self.assertIn("upstream=original/main", out)

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

        def test_dry_run_writes_no_body_file(self):
            fork = make_fork(self.tmp)
            ctx = ctx_for(fork, dry_run=True)
            ctx.platform = "github"
            shown, out, _ = capture(open_mr, ctx, "feat/x", "t", "body\n", True)
            self.assertIn("gh pr create", shown)
            self.assertIn("<description file>", shown)
            self.assertIn("not run (dry run)", out)

    class TestMrEndToEnd(Base):
        def record(self, name: str) -> str:
            """A fake glab/gh that records its argv, one argument per line, and prints a URL."""
            log = os.path.join(self.tmp, name + "-argv.txt")
            fake_tool(os.path.join(self.tmp, "bin"), name,
                      'for a in "$@"; do echo "$a"; done > %s\n'
                      'echo "https://example.invalid/merge_requests/1"\n' % shlex.quote(log))
            return log

        def argv(self, log: str) -> list:
            with open(log) as fh:
                return [ln.rstrip("\n") for ln in fh]

        def value(self, argv: Sequence[str], flag: str) -> str:
            self.assertIn(flag, argv)
            return argv[list(argv).index(flag) + 1]

        def body_of(self, argv: Sequence[str], flag: str) -> str:
            with open(self.value(argv, flag)) as fh:
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
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
            self.assertEqual(self.value(argv, "--title"),
                             "sync: upstream/main %s (1 commits)" % stamp)

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

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (TestDetectPlatform, TestGitVersion, TestLoadConfig, TestResolveCtx,
                 TestCleanTree, TestPush, TestPushMirror, TestAdvanceMirror,
                 TestBootstrapTrunk, TestBackup, TestSimulateMerge, TestSimulateMergeOldGit,
                 TestStatus, TestCheck, TestSync, TestSyncConflicts,
                 TestShip, TestShipErrors, TestSetup,
                 TestMrCommand, TestOpenMr, TestMrEndToEnd,
                 TestParseArgs, TestMainWiring):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
