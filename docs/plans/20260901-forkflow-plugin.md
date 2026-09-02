# forkflow plugin: fork <-> upstream workflow (status / sync / ship / setup)

## Overview

`forkflow` is a Claude Code plugin for developing features in a fork (remote `origin`) of a project
that keeps moving (remote `upstream`), without ever committing on the mirror of upstream or pushing
the fork's trunk directly. It packages a workflow that was arrived at the hard way on a real fork:
a feature merged and pushed straight to a protected branch, undone only by force-push, which needed
a temporary protection change through the hosting platform's API.

The branch layout is fixed - two remotes, two long-lived branches:

```
upstream/main ────●───────●───────●          theirs; read-only (push URL disabled)
                   \
main (mirror) ──────●───────●───────●        pristine copy of upstream/main, fast-forward only, pushed to origin
                                     \
develop (trunk) ──────────────────────●──●──●   upstream + our work; protected, MR-only; the team's real trunk
```

- **mirror** (`main` by default): never committed on. `sync` fast-forwards it to `upstream/main` and
  pushes it; the pre-push hook refuses any push of the mirror that is not a pure ancestor of the
  last-fetched `upstream/main`. A pushed mirror lets teammates and CI see "theirs vs ours"
  (`git diff main..develop`) without configuring `upstream`.
- **trunk** (`develop` by default): carries every feature and every upstream sync, reached only
  through MRs that fast-forward it. Features are squashed single commits; syncs are single merge
  commits of the mirror, so `git log --merges develop` is the "when did we take upstream" record.

What it delivers:

- **status** - one screen: where mirror, trunk, `origin/*` and `upstream/main` stand, how far the
  fork has diverged and on how many upstream-tracked files, what the current branch touches.
- **sync** - advance and push the mirror, then bring it into the trunk through
  `sync/upstream-<date>` + one `--no-ff` merge + an MR; conflicts are resolved in the branch, and a
  "both sides survived" table proves a clean merge is also a correct one.
- **ship** - take a feature branch to the trunk as ONE squashed commit (tree-hash verified) on top
  of a fresh `origin/develop`, through an MR.
- **setup** - make the rules mechanical: `upstream` push URL disabled, pre-push hook, ff-only git
  config for mirror and trunk, trunk bootstrapped on a fresh fork, a report of the platform's
  default branch / merge method / protection with the exact command to fix each mismatch.

Problem solved: the trunk is protected and published; every history rewrite on it is a force-push
with a protection change. The plugin routes **every push through three helpers** - `push()` refuses
the trunk and the mirror, `push_mirror()` can only fast-forward the mirror to upstream,
`bootstrap_trunk()` runs only when the trunk does not exist yet - and has no rebase of the trunk
anywhere, so the mistake becomes impossible instead of merely discouraged.

Hard rules, stated once here and encoded in the script and in every SKILL.md:

1. never push to `upstream` (push URL `DISABLED` + pre-push hook matching the remote name **and** its URL)
2. never push the trunk (`push()` refuses it; hook refuses it; only MRs reach `origin/develop`)
3. never rebase the trunk; upstream comes in by merge only
4. force-push only a feature branch, only `--force-with-lease`, only after a backup branch is
   confirmed on `origin`
5. sync MRs are merged as merges (GitLab: fast-forward of the merge-commit-tipped sync branch;
   GitHub: "Create a merge commit"); never squash/rebase a sync MR - that rewrites upstream's SHAs
   out of the trunk's ancestry and every later sync re-conflicts on the same hunks. Ship MRs are
   merged fast-forward (GitLab `merge_method=ff`; GitHub "Rebase and merge" - GitHub rewrites the
   SHA, so delete the local feature branch afterwards instead of reusing it)
6. never commit on the mirror; it is only ever fast-forwarded to `upstream/main` and pushed by
   `sync`, never with force - the hook rejects a mirror push that is not an ancestor of the
   last-fetched `upstream/main`, and rejects it when that ref is missing

Sequence rule: **sync first, then ship; rebase locally, merge globally.**

The plugin is general: nothing in its script, skills or README refers to any particular fork.
Adopting it in a fork whose `main` already carries work is a documented generic recipe (README);
applying that recipe to a specific project is that project's own document, kept with that project.

## Context (from discovery)

- Repo `ai-thingz` is a Claude Code plugin marketplace (`.claude-plugin/marketplace.json`). One
  plugin so far: `plugins/nocomment` - `SKILL.md` + `scripts/nocomment.py` (stdlib-only, argparse,
  `Fail(msg, code)` exception, `git()`/`git_ok()` helpers, `load_config()` via `tomllib` on 3.11+
  with a warning otherwise, per-file report table, exit codes 0/2/3, 130 on Ctrl-C). The README's
  documented layout is `plugins/<plugin>/skills/<skill>/scripts/`; forkflow adds a plugin-level
  `plugins/<plugin>/scripts/` line to it.
- Layout to mirror for a multi-skill plugin: `umputun/cc-thingz` `plugins/planning` -
  plugin-level `scripts/` shared by several `skills/<name>/SKILL.md`, referenced as
  `${CLAUDE_PLUGIN_ROOT}/scripts/<script>`; local testing via `claude --plugin-dir plugins/<name>`
  (documented in that repo's CLAUDE.md).
- Embedded-test pattern to copy: `cc-thingz/plugins/review/skills/git-review/scripts/git-review.py` -
  `--test` handled by inspecting `argv` before argparse -> `run_tests()` defines `unittest.TestCase`
  classes inline, builds a suite, `TextTestRunner(verbosity=2)`, `sys.exit(0/1)`; module docstring
  usage block lists `--test`. No separate `tests/` file for this plugin.
- Commit style: `feat(forkflow) ...`; work branch `feat/forkflow-skill`.
- Evidence from a real fork session the design rests on: fork diverged by 38 commits / 138 files,
  of which 22 upstream-tracked (the whole conflict surface); `git merge-tree --write-tree` predicted
  the merge correctly; a file merged **clean** yet both sides had touched it and needed a both-sides
  check; squash verified by tree hash; backup convention `backup/<YYYYMMDD-HHMMSS>-<reason>` pushed
  to origin (the user asked for one before the sync merge); sync convention `sync/<remote>-<YYYYMMDD>`;
  a direct push to the protected trunk cost an hour and a protection change to undo.
- The mirror + trunk layout follows the classic fork recommendation (pristine `main`, work on
  `develop`, sync by advancing the mirror and merging it into `develop`); its "push develop directly
  after the merge" step is replaced by the MR rule.
- Verified git behaviours the plan depends on (git 2.55): `git fetch origin upstream` fetches the
  *ref* `upstream` from `origin` (use `--multiple`); `git merge` exits 1 on conflict; `push
  --force-with-lease` exits 1 on stale info; `git branch -D` of the checked-out branch fails;
  `git init` without `-b` creates `master` unless `init.defaultBranch` is set; `merge-tree
  --write-tree` exits 1 both for conflicts and for a bad ref (stdout discriminates); `git rev-parse
  --git-path hooks` follows a global `core.hooksPath`; `git update-ref` moves a branch that is
  checked out in a **linked worktree** without complaint, leaving that worktree's index wrong
  (`git branch -f` refuses) - `for-each-ref --format=%(worktreepath)` tells; `git merge-base
  --is-ancestor A B` exits 1 when not an ancestor and **128** when either ref is missing; a pre-push
  hook receives the remote **name or URL** as `$1` and the URL as `$2`, and stdin lines
  `<local ref> <local sha> <remote ref> <remote sha>` - zero lines when everything is up to date;
  with the push URL set to `DISABLED`, `git push upstream` fails (128) **before** any hook runs;
  `git merge --ff-only` exits 1 when an untracked file would be overwritten even though
  `status --porcelain --untracked-files=no` is empty; `git branch X origin/Y` sets tracking
  (`--no-track` avoids it); GitHub answers 403 for `branches/*/protection` without admin rights;
  GitLab answers 409 for `POST protected_branches` on an already protected branch.

## Development Approach

- **testing approach**: Regular (code first, then tests) - each task ends by adding its test
  cases to `run_tests()` in `forkflow.py` and running `python3 plugins/forkflow/scripts/forkflow.py --test`
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional - they are a required part of the checklist
  - tests live inside `forkflow.py` (`run_tests()`), never in `tests/`
  - tests cover both success and error scenarios (exit codes are part of the contract)
- **CRITICAL: all tests must pass before starting next task** - no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- stdlib only, Python 3.9+ syntax (no `match`, no `X | Y` types). **`.forkflow.toml` needs Python
  3.11+ (`tomllib`)**: a config file that is present but cannot be read is exit 2 for every
  subcommand - the config carries the safety-critical branch names and must never be silently
  defaulted. Without a config file, 3.9+ works. Config-dependent tests are
  `unittest.skipUnless(sys.version_info >= (3, 11), ...)`
- git 2.20+ for everything except the merge simulation (`git merge-tree --write-tree`, 2.38+,
  skipped with a note on older git)
- nothing in the script may push or rebase the trunk, or move the mirror anywhere but forward to
  upstream; the three push helpers enforce it, and a shared test helper `origin_sha(fork, branch)`
  asserts `origin/<trunk>` is unchanged after every `sync` and `ship` run, including failure paths

## Testing Strategy

- **unit tests**: required for every task, embedded in `forkflow.py`, run with `--test`
- **throwaway repos**: `make_fork(tmp, trunk="develop", mirror="main", config=None)` builds a bare
  `upstream.git` (seeded through a work clone), a bare `origin.git` cloned from it, and a work clone
  `fork/` with remotes `origin` and `upstream`; the trunk is created from the mirror and pushed,
  `origin/HEAD` points at the trunk, `upstream/HEAD` at `main`; `config`, when given, is written to
  `.forkflow.toml` verbatim and committed on the trunk. `make_fresh_fork(tmp)` is the same with only
  `main` on origin (the just-forked case `setup` must bootstrap). Helpers push divergent commits to
  either side (clean or conflicting). Every subcommand is exercised end to end; no network
- **git environment isolation** (the suite must pass on any machine): every `git init` uses
  `-b main`; the harness patches `os.environ` for the duration of each test (and restores it) with
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=<tmp>/gitconfig` (empty file), `HOME=<tmp>`,
  `GIT_AUTHOR_NAME/EMAIL`, `GIT_COMMITTER_NAME/EMAIL`, `GIT_EDITOR=true`; each repo gets local
  `user.name`/`user.email`, `commit.gpgsign=false`; `core.hooksPath` is never inherited
- **in-process invocation**: tests call `main([...])` with `-C <fork>` and capture stdout **and
  stderr** (`contextlib.redirect_stdout`/`redirect_stderr` - `Fail` messages go to stderr); exit
  codes come from the return value
- **external CLIs**: `glab`/`gh` are never required by tests - a fake executable written into a
  temp `bin/` prepended to `PATH` records its argv and returns canned JSON/HTTP-status behaviour, so
  MR creation and the platform report are tested without the real tools
- **failure injection**: push rejections are produced by a `pre-receive` hook in the bare `origin`
  (deterministic, root-proof), never by `chmod`; a stale lease is produced by narrowing the fork's
  fetch refspec to `+refs/heads/develop:refs/remotes/origin/develop` so `git fetch origin` does not
  refresh `origin/<feature>` while a second clone pushes a competing commit to it
- **the installed hook is tested as a hook**: after `setup`, real `git push` invocations from the
  fork prove what it refuses and what it allows. Because the `DISABLED` push URL stops `git push
  upstream` before any hook runs, the hook's upstream check is exercised through a **URL push**
  (`git push <upstream fetch URL> main`) and through a second remote that carries the upstream URL
  under another name; the "mirror push allowed" case first advances upstream so that the push
  carries a real ref line (an up-to-date push feeds the hook nothing)
- no e2e/UI tests apply

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

One deterministic script, four thin skills:

```
.claude-plugin/marketplace.json                 + plugin "forkflow" (name, source, description)
plugins/forkflow/.claude-plugin/plugin.json     name forkflow, 0.1.0, author mtilson
plugins/forkflow/scripts/forkflow.py            status | check | sync | ship | setup ; --dry-run ; --test
plugins/forkflow/references/rules.md            the rules + sequence rule + layout (read by every skill)
plugins/forkflow/skills/status/SKILL.md         /forkflow:status
plugins/forkflow/skills/sync/SKILL.md           /forkflow:sync
plugins/forkflow/skills/ship/SKILL.md           /forkflow:ship
plugins/forkflow/skills/setup/SKILL.md          /forkflow:setup
README.md                                       forkflow section + table row + layout line + generic adoption recipe
```

Key design decisions:

- **Script does the mechanical, testable work; Claude does judgment.** Divergence numbers, mirror
  advance, merge simulation, backups, branch creation, merges, squash + tree-hash check, invariant
  checks, push, MR command - all in the script with `--dry-run`. Conflict resolution and MR wording
  are Claude's, driven by SKILL.md.
- **Mirror + trunk, names configurable, layout fixed.** `mirror` defaults to the upstream branch
  name (`main`), `trunk` to `develop`; `.forkflow.toml` can rename both. There is no "work on main"
  mode: a fork whose `main` carries work is refused with a pointer to the adoption recipe.
- **One resolved target per sync.** Right after the fetch, `sync` resolves `target = rev-parse up()`
  once and uses it for the simulation, the commit list, the merge and the verification; the mirror
  is merely moved to `target`. A `--dry-run` therefore previews the *pending* merge even though it
  moves nothing.
- **Merge for the trunk, rebase for feature branches.** The trunk takes upstream by a single
  `--no-ff` merge inside the sync branch and then fast-forwards to it; feature branches are rebased
  onto `origin/develop` and squashed.
- **Three push helpers, nothing else pushes.** `push(ctx, branch, lease=None)` raises `Fail(..., 2)`
  for the trunk and the mirror, never passes `--force` or `--no-verify`, and only adds
  `--force-with-lease=<branch>:<sha>` when a lease is given. `push_mirror(ctx)` asserts the local
  mirror is an ancestor of `up()` and `origin/<mirror>` is an ancestor of the local mirror, then
  pushes without force. `bootstrap_trunk(ctx)` creates the trunk from the resolved upstream SHA and
  pushes it, only when neither a local nor an origin trunk exists, and only before the hook is
  installed. The hook enforces the same rules independently. All are unit-tested; a grep is the backstop.
- **`setup` bootstraps, never migrates.** A `main` that has diverged from upstream is reported with
  the recipe pointer; the plugin never resets it.
- **Backups before every rewrite, pushed, confirmed.** Nothing rewrites until
  `git ls-remote --heads origin <backup>` shows the backup; the rollback line is printed. The
  pre-sync backup rewrites nothing - it is a named restore point of `origin/develop` from before the
  sync, for reverting through a new branch + MR if the sync turns out wrong.
- **Warnings, not gates, for upstream-tracked files.** Editing an upstream-tracked file is a
  permanent merge cost but sometimes necessary; the script lists them, never refuses.
- **Both-sides verification runs on every file changed on both sides** of the merge that was
  actually made (parents `HEAD^1`/`HEAD^2`, or `MERGE_HEAD` while uncommitted), not only on files
  the merge reported as conflicted: the motivating case merged clean and was exactly the file that
  needed checking. Rows are advisory (`CHECK`), exit stays 0.
- **Server-side settings are reported, never changed.** Default branch, merge method and branch
  protection are project-wide; `setup` prints the exact `glab api` / `gh api` command and stops.
- **`check` is a subcommand, not a skill.** It is the preflight `sync` and `ship` run; `status`
  surfaces it for humans.

## Technical Details

### Module layout of `forkflow.py`

```
docstring: purpose, the layout diagram, usage block (every subcommand, common flags, --test), exit codes
constants: CONFIG_FILE=".forkflow.toml", HOOK_MARK="# forkflow pre-push hook", DEFAULT_TRUNK="develop"
class Fail(Exception): msg, code           (as in nocomment.py)
git(), git_ok()                            (as in nocomment.py, always with cwd)
git_rc(*args, cwd) -> (rc, out, err)       for every call whose failure has a documented non-2 code
load_config(root) -> dict                  absent -> {} ; present without tomllib -> Fail(2) ; invalid -> Fail(2)
@dataclass Ctx: root, cfg, origin="origin", upstream, upstream_url, upstream_branch, trunk, mirror,
                mirror_diverged (bool), platform, origin_url, dry_run
                up() -> "<upstream>/<upstream_branch>"
detect_platform(url) -> "gitlab"|"github"|"unknown"
git_version() -> (major, minor)            first two numeric components of `git version` output
resolve_ctx(cwd, args, need_upstream=True, need_trunk=True, strict_mirror=True)   see table below
clean_tree(ctx) -> bool                    `git status --porcelain --untracked-files=no` is empty
header(ctx, sub)                           common header block
step(label, cmd, result, dry=False)        one per-step block line: "  <label>  $ <cmd>  -> <result>"
ahead_behind(a, b), divergence(ctx)        numbers for header/status (origin/<trunk> vs up())
upstream_tracked(ctx, files) -> list       git cat-file -e up():<path>
push(ctx, branch, lease=None)              THE push for everything but mirror/bootstrap; refuses trunk and mirror
push_mirror(ctx)                           ancestor assertions, then plain push of the mirror
advance_mirror(ctx, target) -> (old, new)  worktree-aware ff of the local mirror to target
bootstrap_trunk(ctx, target)               setup only: `git branch --no-track <trunk> <target>` + plain push
backup(ctx, reason, from_ref) -> name      create, push (via push()), ls-remote confirm, rollback line
simulate_merge(ctx, target) -> (clean, conflicts) | None   merge-tree --write-tree --name-only; None if git < 2.38
both_sides_survived(ctx, ours, theirs)     table rows per file changed on both sides of the merge made
squash(ctx, mb, message_file) -> sha       reset --soft + commit -F; tree hash compared
mr_command(ctx, branch, title, body_file)  glab mr create ... | gh pr create ... | manual note
cmd_status / cmd_check / cmd_sync / cmd_ship / cmd_setup -> int
parse_args(argv), main(argv) -> int
run_tests()                                embedded unittest suite
```

`resolve_ctx` resolution: `upstream` remote from cfg -> the only non-origin remote; `upstream_branch`
from cfg -> `upstream/HEAD` -> `main`/`master`; `mirror` from cfg -> `upstream_branch`; `trunk` from
cfg -> `develop`. Validation: `trunk != mirror` always. With `need_upstream`, a missing upstream
remote is exit 2 with the `forkflow setup --upstream-url <URL>` hint. With `strict_mirror`, the
mirror must exist (locally or as `origin/<mirror>`) and `git merge-base --is-ancestor <mirror> up()`
must hold (rc 1 -> exit 2 "`<mirror>` has commits that are not in `up()`: it cannot be the mirror.
See README, *Adopting forkflow in an existing fork*"; rc 128 -> exit 2 "`up()` is not fetched yet:
run `git fetch <upstream>`"); otherwise the result is stored in `ctx.mirror_diverged` for reporting.
With `need_trunk`, `origin/<trunk>` must exist (exit 2 "trunk `<trunk>` not on origin - run
`forkflow setup`").

| subcommand | need_upstream | need_trunk | strict_mirror | why |
|---|---|---|---|---|
| status | yes | no | no | must always be able to explain what setup will do |
| check | yes | yes | yes | |
| sync | yes | yes | yes | |
| ship | yes | yes | yes | |
| setup | no | no | no | it adds the remote, fetches, then checks the mirror itself |

Exit-code rule: `git()` raises `Fail(msg, 2)` on any failure, which is right for preconditions
only. Every call site with a documented 3/4/5 outcome uses `git_rc()` and raises `Fail(msg, code)`
explicitly: `merge` -> 4, `rebase` -> 4, backup push / `ls-remote` confirmation -> 5, force-push
rejection -> 5, mirror push rejection -> 5, bootstrap push rejection -> 5, tree-hash mismatch -> 5,
gate command -> 3, tip check -> 3. `advance_mirror` runs `merge --ff-only` through `git_rc` so an
untracked-file collision exits 2 with git's own "would be overwritten" list.

Argparse: `--test` is handled by inspecting `argv` before `parse_args`. The common flags `-C DIR`,
`--dry-run`, `--force` live on a `parents=[common]` parser attached to every subparser, so
`forkflow.py sync --dry-run` and `forkflow.py --dry-run sync` both work; `--continue`, `--mr`,
`--title` are defined on `sync` and `ship` only, `--message-file` on `ship` only, `--fetch` on
`status` only, `--upstream`, `--upstream-url`, `--trunk`, `--mirror` on `setup` only.

"Clean tree" everywhere in this plan means `git status --porcelain --untracked-files=no` is empty:
staged or unstaged changes to tracked files block; untracked files (an uncommitted
`.forkflow.toml`, editor files) do not.

Commit and merge messages are always passed with `-F <tempfile>`, never `-m` with embedded `\n`.

### Common header (every subcommand)

```
forkflow <sub>  origin=<url>  upstream=<url>  platform=<gitlab|github|unknown>
  as of last fetch: <relative age of .git/FETCH_HEAD, or "never">
  mirror  <mirror> <sha|->   origin/<mirror> <sha|-> (=|unpushed n)   upstream/<ub> <sha|unfetched> (=|mirror behind by n|DIVERGED)
  trunk   <trunk> <sha|->    origin/<trunk> <sha|missing> (=|+n/-m)   upstream/<ub> (+n/-m vs origin/<trunk>)
  divergence: <N> files, <M> upstream-tracked
```

`-` when a branch has no local copy (single-branch clone). Per-step blocks follow, in the nocomment
style (label, command, result). With `--dry-run` each block is prefixed `would:` and the run creates
no branches, commits, pushes, config or hook changes and moves no mirror (`git fetch` still updates
remote-tracking refs; `merge-tree` writes loose objects - both harmless).

### Exit codes

| code | meaning |
|---|---|
| 0 | done, dry run, or nothing to do (`sync` already in sync; `ship` with no commits beyond `origin/<trunk>`); also `--mr` when the tool is missing or fails - the branch is pushed, the command and its stderr are printed |
| 1 | `--test` had failures |
| 2 | precondition: dirty tree, detached HEAD, rebase in progress, on the trunk or mirror when a feature branch is required, remote missing, upstream not fetched, `.forkflow.toml` present but unreadable, mirror diverged from upstream, mirror checked out in another worktree, untracked file blocking the mirror fast-forward, trunk missing on origin, local-only trunk in `setup`, target branch exists without `--force`, `--continue` with nothing to continue (no sync merge / no completed rebase), foreign pre-push hook or `core.hooksPath` without `--force` |
| 3 | invariant in `check`: gate command failed, branch not on `origin/<trunk>`'s tip |
| 4 | conflicts: `sync` leaves the sync branch checked out with markers; `ship` leaves the rebase in progress (detached HEAD); `--continue` resumes after resolution |
| 5 | rewrite safety: backup push not confirmed, tree hash differs after squash, `--force-with-lease` rejected, `push()`/`push_mirror()`/`bootstrap_trunk()` rejected by the remote |
| 130 | interrupted |

### `status` (read-only; `--fetch` to refresh remote-tracking refs first)

Header, then:

```
  branch   <name>  <n> unpushed (vs origin/<name>|not on origin)  tree: clean|<k> modified
  touches upstream-tracked files (WARNING, <m>):
    <path>
    ...
  backups  <count> (refs/remotes/origin/backup/*):  <newest 3>
  setup    upstream push: DISABLED|<url> (LIVE)   pre-push hook: installed|missing   ff-only: <trunk> yes|no, <mirror> yes|no
```

Backups are read from local remote-tracking refs, never `ls-remote` - `status` makes no network
call unless `--fetch`. `status` degrades rather than fails for a missing trunk on origin ("run
`forkflow setup`"), a diverged mirror (`DIVERGED` in the header, exit 0), an unfetched upstream, a
single-branch clone and a detached HEAD; a missing `upstream` remote is still exit 2 with the
`setup --upstream-url` hint.

### `check` (read-only; called by sync and ship)

1. upstream-tracked files touched by `<merge-base(origin/<trunk>, HEAD)>..HEAD` -> `WARNING` list,
   never a failure
2. `gate = [...]` from `.forkflow.toml`: each run with `sh -c` in the repo root, output tail on
   failure, any non-zero -> exit 3; nothing configured -> "gate: none configured (.forkflow.toml gate = [...])"
3. `origin/<trunk>` is an ancestor of HEAD -> else "not on origin/<trunk>'s tip (behind by n,
   ahead by m)" -> exit 3 (skipped when `check` runs on the trunk itself)

### `sync`

```
preflight  clean tree ; not detached ; announce "leaving <branch>, switching to sync/... (you stay there when this finishes)"
fetch      git fetch --multiple origin upstream   -> both remote-tracking refs advanced/unchanged
target     target = git rev-parse up()            (every later step uses this SHA, not the mirror branch name)
mirror     advance_mirror(target): local mirror ancestor of target (else 2) ;
           worktree = `git for-each-ref --format=%(worktreepath) refs/heads/<mirror>` :
             empty -> `git update-ref refs/heads/<mirror> <target>` ; this worktree -> `git merge --ff-only <target>` (git_rc) ;
             another worktree -> exit 2 "mirror <m> is checked out in <path>"
           push_mirror -> origin/<mirror> ancestor of local mirror (else 2), plain push (rejection -> 5)
           result: "mirror main <old> -> <new>, pushed" | "mirror up to date" ; --dry-run: "would:" with old/new
in sync?   target ancestor of origin/<trunk> -> "already in sync (mirror advanced/pushed above if it was behind)", exit 0
simulate   git merge-tree --write-tree --name-only origin/<trunk> <target>
           rc 0 -> clean ; rc 1 AND stdout line 1 is a 40-hex OID -> conflicts = lines 2..first blank ;
           any other rc 1 -> error (exit 2) ; git < 2.38 -> "simulation needs git 2.38+, skipping"
backup     backup/<UTC YYYYMMDD-HHMMSS>-pre-sync from origin/<trunk> ; push() ; ls-remote confirms  (exit 5 if not)
branch     sync/<upstream>-<YYYYMMDD> off origin/<trunk> ; exists -> exit 2 unless --force
           (--force: `git checkout --detach origin/<trunk>` first if it is checked out, then branch -D, recreate)
merge      git merge --no-ff -F <msg> <target>   msg = "Merge <mirror> (mirror of up()) into <branch>" +
           `git log --oneline origin/<trunk>..<target>`
           rc != 0 with unmerged paths -> print files, leave markers, exit 4 with the hint `forkflow sync --continue`
verify     both sides survived table on ours=HEAD^1, theirs=HEAD^2  (see below)
check      cmd_check ; exit 3 -> print "fix, then: forkflow sync --continue"
push       push(ctx, branch)            ; exit 5 -> same hint
mr         print the MR command; run it with --mr
```

The mirror step runs first because it rewrites nothing (ff-only, asserted in-script and by the
hook) and is useful on its own; a failure there leaves the trunk untouched. In a `--dry-run` the
mirror is not moved, which is why every later step reads `target` rather than the mirror branch.

`sync --continue`: preconditions - current branch has the sync prefix, no unmerged paths
(`git diff --name-only --diff-filter=U` empty), else exit 2; if `MERGE_HEAD` still exists the
script commits the merge itself (`git commit --no-edit`); then resumes at *verify* with
ours=`HEAD^1`, theirs=`HEAD^2` - taken from the merge that was actually made, so a mirror or trunk
that moved in between does not change what is verified.

Both-sides-survived: `mb = merge-base(ours, theirs)`; files changed on both sides = intersection
of `mb..ours` and `mb..theirs`; per file, added lines of `mb..ours -- f` present in the merged blob
(`ours a/b`), same for `mb..theirs -- f` (`theirs c/d`); removed lines of each side absent; lines
compared stripped. Files whose merged state is deleted, binary blobs (`\0` in the first 8 KiB), and
renames (`--find-renames` on both diffs) get a row flagged `CHECK` with the reason instead of
counts. Any shortfall -> row flagged `CHECK`, summary names the files, exit stays 0 - SKILL.md tells
Claude to look at flagged rows.

Branch name uses the configured upstream remote name, so a non-standard remote shows in the name.

### `ship`

```
preflight  feature branch required: not <trunk>, not <mirror>, not sync/*, not backup/* ; clean tree ; not detached ;
           no rebase in progress (`git rev-parse --git-path rebase-merge|rebase-apply` must not exist) -> else 2
fetch      git fetch origin
nothing?   HEAD ancestor of origin/<trunk> -> "nothing to ship: <branch> has no commits beyond origin/<trunk>", exit 0
backup     backup/<ts>-pre-ship from HEAD ; push() ; confirm  (exit 5 if not)
rebase     git rebase origin/<trunk>   conflicts -> exit 4 with the hint "resolve, `git rebase --continue`, then `forkflow ship --continue`"
           after rebase: HEAD == origin/<trunk> (every commit was already upstream) -> "nothing to ship", exit 0
squash     mb = merge-base(origin/<trunk>, HEAD) ; tree_before = HEAD^{tree}
           git reset --soft mb ; git commit -F <msg>  ; tree_after == tree_before else exit 5 (+ rollback line)
           message: --message-file, else single commit -> unchanged, else
             "<OLDEST commit's subject>\n\nSquashed from <n> commits (oldest first):\n\n- <subject 1>\n  <body 1 indented>\n- ..."
check      cmd_check (gate + must now be on origin/<trunk>'s tip) ; exit 3 -> print the rollback line
           (the gate runs once, on the state that will be pushed; nothing is pushed on exit 3)
push       branch on origin?  push(ctx, branch, lease=<origin/<branch> sha after fetch>)
           else               push(ctx, branch)        (-u)
mr         print the MR command (body = commit message); run with --mr ; note the merge button
           (GitLab: fast-forward ; GitHub: "Rebase and merge", then delete the local branch - the SHA is rewritten)
```

`ship --continue`: re-runs the full preflight (feature branch, clean tree, not detached, **no
rebase in progress**) and additionally requires `origin/<trunk>` to be an ancestor of HEAD
("the rebase did not complete; run `forkflow ship` again" -> exit 2); then resumes at *squash*.
`ship --continue` on the trunk is therefore exit 2 like any other `ship` on the trunk.

### `setup` (steps in this order; the order matters)

```
remotes    origin must exist ; upstream = --upstream NAME (the remote NAME to use everywhere), else cfg,
           else the only other remote ; none and --upstream-url URL given -> `git remote add upstream URL` ;
           none otherwise -> exit 2 with the `forkflow setup --upstream-url <URL>` hint
           a non-standard name is reported with the suggestion `git remote rename <name> upstream` and used as-is
fetch      git fetch --multiple origin <upstream>  (git_rc ; failure -> exit 2, nothing else has happened yet)
           git remote set-head origin -a ; git remote set-head <upstream> -a  (git_rc ; failure degrades to a note)
names      --trunk NAME / --mirror NAME write the keys into .forkflow.toml (create or update, other keys preserved)
target     target = git rev-parse up()
mirror     local <mirror> or origin/<mirror> must be an ancestor of target (rc 1 -> exit 2 with the README pointer ;
           never reset) ; no mirror branch at all (single-branch clone) -> `git branch --no-track <mirror> <target>` locally
trunk      neither local <trunk> nor origin/<trunk> -> bootstrap_trunk(target): `git branch --no-track <trunk> <target>`
           + plain push -u (fresh fork: the trunk equals upstream, so the push carries nothing of ours) ;
           local-only trunk -> exit 2 ("push it yourself once it is what you want, or delete it") ; present -> nothing
           MUST run before the hook is installed - the hook refuses every push of refs/heads/<trunk>, creation included
push url   git remote set-url --push <upstream> DISABLED
hook       path = os.path.join(root, `git rev-parse --git-path hooks`) ; os.makedirs(exist_ok=True) ;
           `core.hooksPath` set -> WARNING (shared hooks dir) and exit 2 unless --force ;
           existing hook without HOOK_MARK -> exit 2 unless --force (old one kept as pre-push.pre-forkflow) ;
           hook (sh, values baked in at install time):
             $1 == "<upstream>"  OR  $2 == "<upstream fetch URL>"  -> refuse "forkflow: never push to upstream"
             read stdin "<local ref> <local sha> <remote ref> <remote sha>" per line:
               refs/heads/<trunk>  -> refuse (deletion too)
               refs/heads/<mirror> -> deletion refused ; else `git merge-base --is-ancestor <local sha> refs/remotes/<upstream>/<ub>` ;
                                      rc 0 -> allow ; rc 1 -> refuse "mirror push must be a pure copy of upstream" ;
                                      rc >= 2 -> refuse "cannot verify against refs/remotes/<upstream>/<ub> (missing or
                                      unfetched); run `git fetch <upstream>`"
               other refs          -> allow, deletions allowed (stale sync/* branches can be removed)
             comment line: "validates against the last fetch of <upstream>; fetch before pushing the mirror"
           chmod +x
config     branch.<trunk>.mergeOptions=--ff-only   branch.<mirror>.mergeOptions=--ff-only   pull.ff=only   (rules)
           rerere.enabled=true   (reported as "convenience, not a rule")
platform   gitlab: glab api projects/:fullpath -> default_branch, merge_method ; protected_branches/<trunk>, /<mirror>
           github: gh api repos/{owner}/{repo} -> default_branch, allow_merge_commit, allow_rebase_merge ;
                   branches/<trunk>/protection, /<mirror>/protection (404 = none ; 403 = "not checked (insufficient rights)")
           expected: default_branch == <trunk> ; gitlab merge_method=ff ; github allow_merge_commit=true (sync MRs) and
                     allow_rebase_merge=true (ship MRs) ; trunk protected with force-push disallowed
           mirror protection: advisory line only ; a fix command only when force-push is explicitly allowed
           mismatch -> the fix command(s), e.g.
             glab api --method PUT projects/:fullpath -f default_branch=develop -f merge_method=ff
             glab api --method POST projects/:fullpath/protected_branches -f name=develop -f allow_force_push=false   (unprotected)
             glab api --method PATCH projects/:fullpath/protected_branches/develop -f allow_force_push=false          (already protected)
             gh api -X PATCH repos/<owner>/<repo> -f default_branch=develop -f allow_merge_commit=true -f allow_rebase_merge=true
           tool missing / not authenticated / non-zero -> "not checked (<tool> unavailable)"
template   .forkflow.toml written if absent (commented keys) ; it is untracked - the report says
           "commit .forkflow.toml when you are happy with it"
```

`--dry-run` prints every change as `would:` (bootstrap push included) and runs neither the fetch
side effects that write config (`set-head`) nor anything under `hook`/`config`/`template`.

`.forkflow.toml` keys (all optional):

```toml
upstream = "upstream"        # remote name of the original project
upstream_branch = "main"     # its branch we track (default: upstream's HEAD)
mirror = "main"              # our fast-forward-only copy of upstream_branch (default: same name)
trunk = "develop"            # protected, MR-only branch carrying our work
gate = []                    # e.g. ["make test", "terraform fmt -check -recursive"]
sync_prefix = "sync/"
backup_prefix = "backup/"
```

### MR command

```
gitlab:  glab mr create --source-branch <b> --target-branch <trunk> --title <t> --description-file <f> --remove-source-branch
github:  gh pr create --head <b> --base <trunk> --title <t> --body-file <f>
unknown: "open the MR manually: <branch> -> <trunk>"
```

Titles: `--title`, else `sync: <upstream>/<upstream_branch> <YYYYMMDD> (<n> commits)` / the squashed
commit subject. Sync MR body: upstream commits taken (`git log --oneline origin/<trunk>..<target>`),
both-sides rows, the mirror advance (old -> new), the backup name + rollback line, and the
merge-button note (GitLab: merge fast-forward; GitHub: "Create a merge commit" - never
squash/rebase). Ship MR body: the squashed commit message plus the upstream-tracked-files warning
if any, and the merge-button note (GitLab: fast-forward; GitHub: "Rebase and merge"). `--mr` with
the tool missing or failing prints the command (and stderr) and exits 0 - the branch is pushed, the
MR is the only thing left.

### After the MR is merged (manual, by design)

The plugin never moves the local trunk. Both `sync/SKILL.md` and `ship/SKILL.md` end with the one
manual step: `git fetch origin && git switch develop && git merge --ff-only origin/develop` (which
`setup`'s `--ff-only` config guarantees cannot become a merge commit), and for a shipped branch on
GitHub: delete the local feature branch (its SHA was rewritten by "Rebase and merge").

### Adopting forkflow in an existing fork (README recipe, generic; never automated)

For a fork whose `main` already carries work: create `develop` from `main` and push it; make
`develop` the platform default branch and protect it (`setup` prints both commands); then make
`main` a pure copy of `upstream/main` - this rewrites a published branch and is done once, by a
Maintainer, by hand: either force-push with protection temporarily allowing it, or unprotect,
delete and recreate `main` from `upstream/main`, then re-protect. Take a `backup/<ts>-pre-adoption`
of the old `main` first. Alternative with no rewrite on GitLab: project **pull mirroring** of
`upstream` into `main`, once `main` has been reset. Any project applying this recipe keeps its own
step-by-step document with its real branch names and SHAs with that project, not in the plugin.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): everything below - script, tests, skills, docs
- **Post-Completion** (no checkboxes): trying it on a real fork, project-side settings

## Implementation Steps

### Task 1: Plugin scaffold, script skeleton, push helpers and the test harness

**Files:**
- Create: `plugins/forkflow/.claude-plugin/plugin.json`
- Create: `plugins/forkflow/scripts/forkflow.py`
- Modify: `.claude-plugin/marketplace.json`

- [x] `plugin.json`: name `forkflow`, description, version `0.1.0`, author `mtilson`, homepage/repository `https://github.com/cloud-simple/ai-thingz` (mirror `plugins/nocomment`)
- [x] `marketplace.json`: add the `forkflow` entry with `name`, `source: ./plugins/forkflow`, `description` (as the nocomment entry has)
- [x] `forkflow.py`: docstring with the layout diagram and usage block (all subcommands, flags per subcommand, `--test`, exit codes incl. 1 and 130, the Python 3.11 note for config); `Fail`, `git`, `git_ok`, `git_rc`, `git_version`, `load_config` (absent -> `{}`; present without `tomllib` -> `Fail(2)`; invalid -> `Fail(2)`); `Ctx` with `up()`; `detect_platform`; `resolve_ctx` with `need_upstream`/`need_trunk`/`strict_mirror` exactly as in the table, `mirror_diverged`, rc 1 vs rc 128 messages; `clean_tree`; `header` (mirror + trunk lines incl. `unfetched`/`DIVERGED`/`missing`, "as of last fetch", `-` for missing local branches); `step`; `ahead_behind`; `divergence`; `upstream_tracked`
- [x] `push(ctx, branch, lease=None)`: refuses `ctx.trunk` and `ctx.mirror` with `Fail(..., 2)`; never emits `--force`/`--no-verify`; `--force-with-lease=<branch>:<sha>` only when `lease` given; `-u` on first push; non-zero -> `Fail(..., 5)` with git's stderr
- [x] `push_mirror(ctx)`, `advance_mirror(ctx, target)` (worktree-aware via `for-each-ref %(worktreepath)`; `merge --ff-only` through `git_rc`), `bootstrap_trunk(ctx, target)` (`--no-track`, only when neither local nor origin trunk exists, plain push, rejection -> 5)
- [x] `parse_args`: `--test` inspected in `argv` before argparse; `parents=[common]` with `-C`, `--dry-run`, `--force`; subcommand-specific flags as listed in Technical Details; `main(argv)` dispatching, mapping `Fail` -> its code with the message on stderr, `KeyboardInterrupt` -> 130; `if __name__ == "__main__"`
- [x] `run_tests()` skeleton with the harness: `isolated_env(tmp)` context manager (patches/restores `os.environ` as in Testing Strategy); `make_fork(tmp, trunk="develop", mirror="main", config=None)` (bare upstream seeded through a work clone with 2 commits: `README.md`, `src/app.py`, `shared.tf` - every `git init -b main`; bare origin cloned from it; work clone `fork/` with both remotes; trunk created from the mirror and pushed; `origin/HEAD` -> trunk, `upstream/HEAD` -> main; `config` committed on the trunk when given); `make_fresh_fork(tmp)` (only `main` on origin); helpers `commit_upstream(...)`, `commit_fork(...)` (push to origin trunk - test setup only, direct), `run(argv) -> (code, out, err)`, `origin_sha(fork, branch)`, `fake_tool(bin_dir, name, script)`, `skip_without_tomllib` decorator
- [x] write tests: `detect_platform` (ssh/https gitlab, github, local path -> unknown); `load_config` (absent -> `{}`; valid; invalid -> 2; present on a Python without `tomllib` -> 2, simulated by patching `sys.modules['tomllib'] = None`); `resolve_ctx` (defaults `develop`/`main`; custom names from config; `upstream/HEAD` -> `master` fallback; `up()`; missing upstream -> 2 with the hint, but not with `need_upstream=False`; mirror == trunk -> 2; mirror with its own commit -> 2 with the README pointer under `strict_mirror`, `mirror_diverged=True` without; unfetched upstream -> 2 "not fetched" under `strict_mirror`; trunk missing on origin -> 2 unless `need_trunk=False`); `git_version` parse (`2.39.5 (Apple Git-154)`, `2.42.0.windows.1`); `clean_tree` (untracked file -> clean; modified tracked -> dirty)
- [x] write tests: `push()` refuses trunk and mirror without calling git; pushes a feature branch; lease argument string; rejection by a `pre-receive` hook -> 5; `push_mirror()` refuses a mirror that is not an ancestor of upstream and a local mirror behind `origin/<mirror>`; `advance_mirror()` via `update-ref`, via `--ff-only` when checked out here, -> 2 when the mirror is checked out in a `git worktree add` sibling, -> 2 with git's list when an untracked file collides; `bootstrap_trunk()` on a fresh fork creates and pushes `develop` equal to upstream's SHA with no tracking config, refuses when `origin/develop` exists, works on a clone that has no local `main`; `main(["-C", fork, "status", "--dry-run"])` and `main(["status", "--dry-run", "-C", fork])` produce identical output
- [x] run `python3 plugins/forkflow/scripts/forkflow.py --test` - must pass before task 2

### Task 2: `status`

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] `cmd_status`: optional `--fetch` (`git fetch --multiple origin <upstream>`); header (mirror line: `=` | `unpushed n` | `mirror behind by n` | `DIVERGED` | `unfetched`; trunk line incl. `missing`); branch line (unpushed count vs `origin/<branch>` or "not on origin", modified count); upstream-tracked files touched by the branch as a WARNING list; backups from `refs/remotes/origin/<backup_prefix>*` (count + newest 3, sorted by name); setup line (push URL DISABLED/LIVE, hook installed/missing via HOOK_MARK, ff-only yes/no for trunk and mirror)
- [x] `status` never writes and makes no network call without `--fetch`; degrades as specified (missing trunk -> hint, diverged mirror -> `DIVERGED` and exit 0, unfetched upstream, single-branch clone `-`, detached HEAD reported); missing upstream remote -> 2
- [x] write tests: numbers match a constructed state (fork ahead 2 / upstream ahead 1, divergence N/M), WARNING list contains exactly the touched upstream-tracked file, detached HEAD reported, setup line says LIVE/missing before setup, backups listed after a `backup/*` ref exists on origin and is fetched, `--fetch` refreshes a moved `upstream/main`, mirror line shows `mirror behind by 1` after an upstream commit and `unpushed 1` after a local-only advance, fresh fork -> the setup hint, diverged mirror -> `DIVERGED` with exit 0, unfetched upstream -> `unfetched` with exit 0
- [x] write tests: error case - no `upstream` remote -> exit 2 with the hint on stderr
- [x] run tests - must pass before task 3

### Task 3: `check`

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] `cmd_check`: upstream-tracked WARNING (shared with status), gate commands via `sh -c` in the repo root with output tail on failure, "none configured" note otherwise; tip check (`origin/<trunk>` ancestor of HEAD, "behind by n, ahead by m" otherwise; skipped on the trunk)
- [x] exit 3 on gate failure or tip check failure; 0 otherwise; the WARNING alone never changes the exit code
- [x] write tests (`make_fork(config=...)` with a committed `gate`; config-dependent cases skipped without `tomllib`): gate absent -> note + 0; passing gate -> 0; failing gate -> 3 with its output tail; diverged from `origin/develop` -> 3 with both numbers; on tip -> 0; upstream-tracked touched -> WARNING listed and still 0; the tip check is against the trunk, never the mirror
- [x] run tests - must pass before task 4

### Task 4: `backup()` and `simulate_merge()`

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] `backup(ctx, reason, from_ref)`: name `<backup_prefix><UTC YYYYMMDD-HHMMSS>-<reason>`, `git branch <name> <from_ref>`, `push()`, `git ls-remote --heads origin <name>` must list it else `Fail(..., 5)`; prints the rollback line `git reset --hard origin/<name>` and, for `pre-sync`, what the backup is for; `--dry-run` prints `would:`
- [x] `simulate_merge(ctx, target)`: git < 2.38 -> `None` + note; else `git_rc("merge-tree", "--write-tree", "--name-only", "origin/<trunk>", target)`: rc 0 -> clean; rc 1 with a 40-hex first stdout line -> conflicted paths = lines 2..first blank line; other rc 1 -> `Fail(stderr, 2)`
- [x] write tests: backup created, pushed and confirmed, rollback line printed; `pre-receive` hook rejecting `refs/heads/backup/*` -> 5 and no local backup left behind; hook accepts but the harness deletes the ref before confirmation -> 5; dry run creates nothing; `simulate_merge` clean, conflicting (paths parsed), bad ref -> 2, old-git note (simulated by patching `git_version`)
- [x] run tests - must pass before task 5

### Task 5: `sync` clean path

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] `cmd_sync` clean path in the exact order of the `sync` block: preflight; fetch; `target`; mirror step (`advance_mirror(target)` + `push_mirror`, result line); "already in sync" -> 0 after the mirror step; `simulate_merge(target)`; backup from `origin/<trunk>`; sync branch off `origin/<trunk>` (exists -> 2 unless `--force`, which detaches first if that branch is checked out); `git merge --no-ff -F <msgfile> <target>` via `git_rc` with the commit list in the message; `both_sides_survived(HEAD^1, HEAD^2)` (Task 6 fills the logic - here it runs on zero or non-conflicting files); `cmd_check` (exit 3 -> hint `forkflow sync --continue`); `push()` (exit 5 -> same hint); MR command printed; `--mr` runs it (Task 8 finalises the body)
- [x] `--dry-run`: every block as `would:`, no branch/backup created, no mirror moved or pushed, no push; the simulation and commit list still describe the **pending** merge against `target`
- [x] write tests: clean sync end to end (`origin/main` == `upstream/main` afterwards, sync branch on origin, merge commit's second parent == target, message lists the upstream commits, backup on origin, `origin_sha(develop)` unchanged, exit 0, both remote-tracking refs advanced by the fetch); upstream unchanged but mirror behind on origin -> mirror pushed, then "already in sync", 0, no branch; fully in sync -> 0 and nothing moved; **dry run with upstream ahead by 2 and a conflicting hunk -> lists both commits, reports the conflict, moves nothing**; existing sync branch -> 2; `--force` while ON that sync branch -> recreated; dirty tree -> 2; detached HEAD -> 2; sync started while the mirror is checked out -> `--ff-only` path used, worktree updated; mirror checked out in a linked worktree -> 2 before anything is pushed; custom names from config used throughout (skipped without `tomllib`)
- [x] write tests: mirror push rejected by a `pre-receive` hook -> 5 before any backup or sync branch exists; a mirror with its own commit -> 2 and nothing touched; backup rejected -> 5 with no sync branch and the trunk unchanged
- [x] run tests - must pass before task 6

### Task 6: `sync` conflicts, `--continue`, both-sides verification

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] conflict path: merge rc != 0 with unmerged paths -> files listed, markers left, sync branch stays checked out, exit 4, hint `forkflow sync --continue` printed
- [x] `--continue`: precondition current branch has `sync_prefix` and no unmerged paths (else 2); commit the merge with `git commit --no-edit` if `MERGE_HEAD` exists; resume at verify with ours=`HEAD^1`, theirs=`HEAD^2` -> check -> push -> MR
- [x] `both_sides_survived(ctx, ours, theirs)`: `mb`, both-sides file set, per file `ours a/b`, `theirs c/d` (added lines present, removed lines absent, stripped comparison); deleted-in-result, binary and renamed files -> `CHECK <reason>` row; any shortfall -> `CHECK`; summary line naming flagged files
- [x] write tests: conflicting upstream change (same lines of `shared.tf`) -> exit 4, markers present, trunk unchanged, mirror already advanced and pushed; resolve in test, `git add`, `--continue` -> merge committed, table shows both sides `n/n`, branch pushed, exit 0; clean-but-both-sides file (different hunks of a 20-line `notes.txt`) appears in the table with full counts; **the mirror and `origin/<trunk>` are moved between the conflicting run and `--continue` and the table is unchanged**
- [x] write tests: `--continue` with unmerged paths -> 2; `--continue` not on a sync branch -> 2 (and on a sync branch with no merge -> 2 "nothing to continue"); `CHECK` flagged when a resolution drops one side's lines; delete/modify collision -> `CHECK deleted` row, exit 0; binary file changed on both sides -> `CHECK binary`
- [x] run tests - must pass before task 7

### Task 7: `ship`

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [ ] preflight: feature branch required (not trunk, not mirror, not `sync_prefix`, not `backup_prefix`), clean tree, not detached, no rebase in progress -> else 2
- [ ] flow: fetch origin; "nothing to ship" -> 0 when HEAD is an ancestor of `origin/<trunk>`; backup from HEAD (`-pre-ship`), confirmed; `git rebase origin/<trunk>` via `git_rc` (conflict -> 4 with the `git rebase --continue` then `forkflow ship --continue` hint); post-rebase "nothing to ship" -> 0; `squash`: tree hash before, `reset --soft mb`, `commit -F` with the composed message (`--message-file` / single commit unchanged / "Squashed from n commits (oldest first)" body, oldest subject as subject), tree hash after must match else 5 with the rollback line; `cmd_check` (exit 3 -> rollback line printed); `push()` with lease when the branch is on origin, else plain; MR command with the commit message as body and the merge-button note
- [ ] `ship --continue`: full preflight again + `origin/<trunk>` ancestor of HEAD (else 2, "the rebase did not complete; run `forkflow ship` again"); resume at squash
- [ ] write tests: 3-commit feature branch on a stale base -> one commit on `origin/develop`'s tip, tree hash equal, message composed oldest-first, pushed, backup on origin, trunk unchanged, exit 0; branch already on origin -> pushed with `--force-with-lease`; single commit -> message unchanged; `--message-file` used verbatim; "nothing to ship" -> 0 and no backup created
- [ ] write tests: on the trunk -> 2; on the mirror -> 2; on a `sync/` branch -> 2; dirty tree -> 2; mid-rebase -> 2; rebase conflict -> 4, then resolve + `git rebase --continue` + `ship --continue` -> 0; `ship --continue` on the trunk -> 2; `ship --continue` after `git rebase --abort` -> 2; stale lease (narrowed refspec + competing push from a second clone) -> 5 with trunk unchanged; gate failure after squash -> 3 with the rollback line and nothing pushed
- [ ] run tests - must pass before task 8

### Task 8: MR command and platform tools

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [ ] `mr_command(ctx, branch, title, body_file)`: gitlab `glab mr create ...`, github `gh pr create ...` (target = trunk), unknown -> manual note; body written to a temp file; sync body (upstream commits, both-sides rows, mirror advance, backup + rollback, merge-button note), ship body (commit message + WARNING list + merge-button note)
- [ ] `--mr` executes the command; tool missing or non-zero -> the command and stderr printed, exit stays 0
- [ ] `--title` override for both `sync` and `ship`; defaults as in Technical Details
- [ ] write tests: command strings per platform (URL-driven `Ctx`; target branch is the trunk, incl. a custom name); `--mr` with a fake `glab`/`gh` on `PATH` records the expected argv and body-file contents; missing tool -> 0 with the manual line; fake tool exiting 1 -> 0 with its stderr shown
- [ ] run tests - must pass before task 9

### Task 9: `setup` - remotes, names, mirror, trunk bootstrap, config, template

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [ ] remotes: resolve upstream (`--upstream NAME`, cfg, the only other remote); `--upstream-url URL` adds `upstream` when missing; none -> 2 with the hint; fetch both (git_rc, failure -> 2); `set-head` for both remotes via `git_rc`, degrading to a note; non-standard name reported with the `git remote rename` suggestion; `git remote set-url --push <upstream> DISABLED`
- [ ] names: `--trunk NAME` / `--mirror NAME` written into `.forkflow.toml` (create or update the two keys only, preserving the rest; requires `tomllib` to read an existing file)
- [ ] `target`; mirror check after the fetch (rc 1 -> 2 with the README pointer; never reset); no mirror branch anywhere -> local `--no-track` branch at `target`
- [ ] trunk: `bootstrap_trunk(target)` when absent everywhere; local-only trunk -> 2 with the explanation; existing -> nothing; **runs before the hook step**
- [ ] git config: `branch.<trunk>.mergeOptions=--ff-only`, `branch.<mirror>.mergeOptions=--ff-only`, `pull.ff=only` (named as rules), `rerere.enabled=true` (named as convenience)
- [ ] `.forkflow.toml` template written if absent (commented keys) with the "commit it" note
- [ ] `--dry-run` prints every change as `would:` (bootstrap push included), runs `set-head`, hook, config and template not at all
- [ ] write tests: after setup on `make_fork` - push URL DISABLED, config keys set, template written, second run idempotent; remote named `original` used as-is with the rename suggestion printed; `--trunk trunk --mirror upstream-main` writes the keys and preserves other keys (skipped without `tomllib`); fresh clone with one remote + `--upstream-url` -> remote added, fetched, both HEADs set, then bootstrap; `make_fresh_fork` + `setup` -> `develop` created locally and on origin equal to upstream's SHA, no tracking config, and the platform report flags the default branch; single-branch clone without local `main` -> local mirror created at `target`; a fork whose `main` has its own commits -> 2 with the README pointer and nothing changed; local-only `develop` -> 2; fetch failure (upstream URL pointing nowhere) -> 2 with nothing changed; dry run changes nothing (remotes, config, template, branches all untouched)
- [ ] run tests - must pass before task 10

### Task 10: `setup` - the pre-push hook

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [ ] hook path joined to root from `git rev-parse --git-path hooks`, `makedirs`; `core.hooksPath` set -> WARNING + 2 unless `--force`; foreign hook -> 2 unless `--force` (kept as `pre-push.pre-forkflow`); marked hook rewritten idempotently; executable bit
- [ ] hook content exactly as in the `setup` block: upstream refused by name **or** URL; trunk refused incl. deletion; mirror deletion refused; mirror push allowed only on `--is-ancestor` rc 0, refused with distinct messages on rc 1 and rc >= 2; other refs and their deletions allowed; the last-fetch comment line
- [ ] write tests (real `git push` from the fork after `setup`): `git push upstream main` -> 128 mentioning `DISABLED` (push URL, not the hook); `git push <upstream fetch URL> main` -> refused by the hook (URL match); a remote `mirror-src` with the upstream URL under another name -> `git push mirror-src main` refused by the hook (URL match); `git push origin develop` and `git push origin :develop` refused; **advance upstream, ff the local mirror, then `git push origin main` succeeds and the hook saw a ref line** (assert via the hook writing its stdin to a temp log in test mode, or via `origin/main` moving); commit on local `main` then `git push origin main` refused with "pure copy"; delete `refs/remotes/<upstream>/main` then `git push origin main` refused with "cannot verify"; `git push origin :main` refused; feature-branch push allowed; `git push origin :sync/x` allowed; second `setup` does not duplicate the hook; foreign hook -> 2, `--force` keeps a copy; `core.hooksPath` set -> 2 with WARNING, `--force` proceeds
- [ ] run tests - must pass before task 11

### Task 11: `setup` - platform report

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [ ] gitlab `glab api projects/:fullpath` (`default_branch`, `merge_method`) + `protected_branches/<trunk>` and `/<mirror>`; github `gh api repos/{owner}/{repo}` (`default_branch`, `allow_merge_commit`, `allow_rebase_merge`) + `branches/<trunk>/protection` and `/<mirror>/protection`; 404 = unprotected, 403 = "not checked (insufficient rights)", tool missing/failing = "not checked"
- [ ] expectations and fix commands exactly as in the `setup` block (PUT/POST/PATCH chosen by current state; mirror protection advisory unless force-push is explicitly allowed); never modifies anything remote
- [ ] write tests with fake `glab`/`gh`: `default_branch: "main"` -> default-branch fix; `merge_method: "merge"` -> ff fix; unprotected trunk -> POST command; protected trunk with `allow_force_push: true` -> PATCH command; github `allow_merge_commit: false` -> PATCH command; 403 on protection -> "not checked (insufficient rights)" and no fix command; mirror unprotected -> advisory line only; matching values -> "ok"; tool absent -> "not checked"; dry run runs the report read-only
- [ ] run tests - must pass before task 12

### Task 12: rules and the four skills

**Files:**
- Create: `plugins/forkflow/references/rules.md`
- Create: `plugins/forkflow/skills/status/SKILL.md`
- Create: `plugins/forkflow/skills/sync/SKILL.md`
- Create: `plugins/forkflow/skills/ship/SKILL.md`
- Create: `plugins/forkflow/skills/setup/SKILL.md`

- [ ] `rules.md`: the six hard rules (incl. the ship-MR merge button and the GitHub SHA-rewrite note) and the sequence rule, the layout diagram, plus the platform note (a stale sync MR is redone with `sync`, never "Rebase"d in the UI) - short, quotable, no project names
- [ ] `status/SKILL.md`: frontmatter (name, description with triggers "forkflow status", "where are we vs upstream", "how far behind upstream", `allowed-tools: Bash, Read`); run `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py status` (`--fetch` when the user asks for current numbers), explain the numbers and the layout, if the setup line shows LIVE/missing or the trunk is missing say so and offer `/forkflow:setup`
- [ ] `sync/SKILL.md`: triggers "sync upstream", "pull in upstream", "forkflow sync"; read `rules.md`; dry run -> real run -> exit 4: resolve conflicts file by file showing both sides, `git add`, `--continue` -> read the both-sides table, look at `CHECK` rows -> MR with `--mr` -> report mirror advance, branch, merge commit, backup + rollback line, MR URL, what was not done; never merge the MR, never touch the trunk; closing step: how the local trunk catches up after the MR merges
- [ ] `ship/SKILL.md`: triggers "ship this branch", "land this branch", "forkflow ship", "squash and open the MR"; same shape; WARNING list of upstream-tracked files shown, ask only if the branch adds to that list unexpectedly; rebase conflicts -> resolve, `git rebase --continue`, `ship --continue`; closing step incl. deleting the local branch after a GitHub "Rebase and merge"
- [ ] `setup/SKILL.md`: triggers "forkflow setup", "set up the fork", "protect the trunk locally"; fresh clone -> ask for the upstream URL and pass `--upstream-url`; run, then walk through the default-branch/merge-method/protection report and the printed fix commands; explain what the hook refuses and that it validates against the last fetch; a diverged `main` -> point to the README adoption recipe and stop (never reset it)
- [ ] every SKILL.md: state up front that the script never pushes or rebases the trunk, never commits on the mirror, and that Claude must not either; `allowed-tools: Bash, Read, AskUserQuestion` for sync/ship/setup; no project-specific names anywhere
- [ ] verify frontmatter parses (`python3 -c "import yaml"` if available, else eyeball) and every script path uses `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py`
- [ ] run `--test` - must still pass before task 13

### Task 13: Verify acceptance criteria
- [ ] verify all requirements from Overview are implemented: six rules mechanically enforced (script and hook), sync/ship/status/setup behave as specified, exit codes as tabled, `--dry-run` creates no branches/commits/pushes/config/hooks/mirror moves and still previews the pending merge
- [ ] grep the script as a backstop: every push goes through `push()`, `push_mirror()` or `bootstrap_trunk()`; no `rebase` whose target can be the trunk; no `--force` (only `--force-with-lease=`), no `--no-verify`; `update-ref`/`merge --ff-only` only inside `advance_mirror`; no project-specific names in `plugins/forkflow/` or README
- [ ] verify edge cases: git < 2.38 note, detached HEAD, `--continue` misuse (both subcommands), foreign hook, `core.hooksPath`, unknown platform, config without `tomllib`, single-branch clone, mirror checked out here / in a linked worktree, fresh fork, diverged mirror, unfetched upstream
- [ ] run full test suite: `python3 plugins/forkflow/scripts/forkflow.py --test` and `python3 -m unittest discover -s tests` (nocomment untouched)
- [ ] load the plugin locally with `claude --plugin-dir plugins/forkflow` (falling back to `/plugin marketplace add <path to this checkout>` + `/plugin install forkflow@ai-thingz` if the flag is unavailable) and check that `/forkflow:status` resolves as a skill and the script path expands; if the `/plugin:skill` form does not resolve, fall back to description-only triggers and note it in the README
- [ ] run `/forkflow:status` on this repo (no upstream -> the exit 2 hint) to confirm the end-to-end wiring

### Task 14: [Final] Update documentation
- [ ] `README.md`: table row for `forkflow`; a `## forkflow` section (the layout diagram, the six rules, the four skills, the sequence, `.forkflow.toml` with the Python 3.11 note, exit codes, `--test`, *Adopting forkflow in an existing fork* generic recipe incl. the GitLab pull-mirroring alternative); install line `/plugin install forkflow@ai-thingz`; layout block gains `plugins/<plugin>/scripts/` (shared scripts) and notes that `forkflow.py` carries its own tests so `tests/` no longer covers every plugin; state that the "link a single skill" symlink install does **not** apply to forkflow (its skills need `${CLAUDE_PLUGIN_ROOT}`); no project-specific names
- [ ] update CLAUDE.md if new patterns discovered (none expected - the repo has no CLAUDE.md)
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion
*Items requiring manual intervention or external systems - no checkboxes, informational only*

**First real fork**: apply the README adoption recipe using that project's own migration document
(kept with the project, not in this plugin): backup of the old `main`, `develop` created and
protected and made the default, `main` reset to `upstream/main` once by a Maintainer, then
`/forkflow:setup`, `gate` entries committed in `.forkflow.toml`, and the pending feature branches
shipped with `/forkflow:ship` followed by `/forkflow:sync`.

**Project-side settings the plugin only reports**: default branch, GitLab merge method / GitHub
merge options, protection of trunk (and, advisory, mirror) - changed by a Maintainer through the
printed command or the web UI.

**Review decisions recorded** (plan-review rounds 1 and 2, 2026-09-01; all critical/important
findings applied):
- kept: both-sides verification on all files changed on both sides (not only conflicted ones);
  gate runs once after rebase+squash; pre-sync backup kept with its purpose stated
- dropped: `push.default=current` (round 1), `--rename-upstream` (round 2 - the detected name is
  used as-is and a `git remote rename` suggestion is printed)
- layout: single mirror + trunk layout (pristine `main`, `develop` trunk) chosen over a
  configurable two-layout design; migration of existing forks stays manual and outside the plugin
- Python: `.forkflow.toml` requires 3.11+; an unreadable config is exit 2, never a silent default
