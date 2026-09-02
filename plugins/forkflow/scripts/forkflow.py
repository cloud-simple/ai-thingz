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
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

CONFIG_FILE = ".forkflow.toml"
HOOK_MARK = "# forkflow pre-push hook"
DEFAULT_TRUNK = "develop"
DEFAULT_SYNC_PREFIX = "sync/"
DEFAULT_BACKUP_PREFIX = "backup/"
README_POINTER = "See README, *Adopting forkflow in an existing fork*"


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
# subcommands
# --------------------------------------------------------------------------- #

def cmd_status(args: argparse.Namespace) -> int:
    ctx = resolve_ctx(args.dir, args, need_upstream=True, need_trunk=False, strict_mirror=False)
    header(ctx, "status")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    raise Fail("`check` is not implemented yet")


def cmd_sync(args: argparse.Namespace) -> int:
    raise Fail("`sync` is not implemented yet")


def cmd_ship(args: argparse.Namespace) -> int:
    raise Fail("`ship` is not implemented yet")


def cmd_setup(args: argparse.Namespace) -> int:
    raise Fail("`setup` is not implemented yet")


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

    def write(root: str, path: str, text: str) -> str:
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
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
                 TestBootstrapTrunk, TestParseArgs, TestMainWiring):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
