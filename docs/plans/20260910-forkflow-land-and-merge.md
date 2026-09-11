# forkflow land and opt-in merge

## Overview

Every `forkflow ship` and `forkflow sync` ends with a merge request and a printed shell line for
afterwards: `git fetch origin && git switch <trunk> && git merge --ff-only origin/<trunk>`. On the
first real fork (GET, 2026-09-07..09) that line was typed out by hand after every MR, and twice the
user asked Claude to merge the MR itself, which it did through `glab api PUT .../merge` with nothing
checking that the merge used the method rule 5 requires. This plan closes both gaps:

- **`forkflow land`** - the closing step as a subcommand: fetch, verify that the shipped or synced
  commit is on `origin/<trunk>`, fast-forward the local trunk, delete the landed local branch.
  Works after a human merged the MR, in any later session, and survives a human "squash and merge"
  of a ship.
- **`--merge`** on `ship` and `sync` - opt-in per fork: merges the MR the run just opened, with the
  method rule 5 requires and a head-commit guard, then chains into `land`. Refused unless the fork's
  `.forkflow.toml` says its MRs are self-merged, so a reviewed fork can never be merged by accident.

Forks come in both kinds - solo (the Maintainer merges their own MRs; the MR is where the diff and
the both-sides table live) and reviewed (someone else approves). Everything here is default-off and
safe in the second case; the fork declares its own model once in `.forkflow.toml`.

The README stance "the plugin never moves the local trunk" is retired in favour of "moves it only by
fast-forward, through `land`, and leaves you on it". None of the six hard rules changes: `land`
neither pushes, rebases nor commits on the trunk or the mirror.

Closes `docs/backlog/no-post-merge-catch-up-step.md`. Designed in a brainstorm on 2026-09-10 and
revised after a plan review the same day; every design choice below is settled. The review's
decisions are recorded at the end.

## Context (from discovery)

- `plugins/forkflow/scripts/forkflow.py` (~8.6k lines, stdlib only, Python 3.9+; `.forkflow.toml`
  needs 3.11+ for `tomllib`). Embedded unittest suite in `run_tests()`, run with `--test`, 348 tests
  green at plan time, ~200 s a run on this machine - the per-task "run tests" gate costs minutes, and
  Tasks 3-5 add on the order of 25 more fork-triple cases. No `tests/` files for this plugin;
  `tests/` holds nocomment's 24 and stays untouched.
- Conventions: `Fail(msg, code)`; `git()` raises on failure, `git_rc()` returns `(rc, out, err)` for
  calls with a documented non-2 outcome; `step(label, cmd, result, dry=)` prints one line per step
  and every printed command quotes its arguments with `sh_arg()`; `--dry-run` prints `would:` and
  writes nothing.
- `TestSourceInvariants` (AST-based; `grep -n 'class TestSourceInvariants'`) pins, by textual
  needle, the owners of `"push"` (the three push helpers), `"update-ref"` and `"merge", "--ff-only"`
  (`advance_mirror`), `"rebase"` (`rebase_onto`), `"--force-with-lease"` (`push`), and the word
  `subprocess` (`{git, git_ok, git_rc, shell, open_mr, api_get, <module>}`, test
  `test_git_and_the_platform_tools_are_the_only_subprocesses`). `owners('"-f"') == set()` blocks
  `git branch -f`. This plan makes exactly two deliberate edits there, both named in Technical Details.
- State file `.git/forkflow-state.json`: `read_state` 615, `save_state` 627, `write_state(ctx, kind,
  entry|None)` 638 (no-op under `--dry-run`), `record_published` 650, `resumable` 678 (reads an entry
  defensively - the pattern to copy for `pending`).
- MR plumbing: `mr_target` 1294 (the fork's URL for `--repo`, `("", reason)` when the origin cannot be
  named - every fixture fork is `platform=unknown`), `mr_command` 1326, `open_mr` 1350 (runs the
  tool with `stdin=DEVNULL`, prints its stdout, returns only the shown command string; both
  production callers at 2006 and 2370 discard that value).
- `finish_sync` 1959 and `finish_ship` 2322 call `open_mr`, then `write_state(ctx, kind, None)`,
  then print the shell catch-up line (2009, 2373). `cmd_sync` 2078 dispatches `--continue` on its
  fourth line, before any preflight; `cmd_ship` 2465 runs `ship_preflight` 2177 first. `cmd_sync`
  prints "leaving <branch>, switching to <sync branch> (you stay on it when this finishes)" at 2095.
  `rebase_in_progress` 2152, `clean_tree` 737, `current_branch` 794, `mirror_worktree` (the
  `for-each-ref %(worktreepath)` check `advance_mirror` uses when the mirror is checked out elsewhere).
- Config: `CONFIG_STRINGS` 198 lists the keys `parse_config` 216 type-checks; `load_config` 246;
  `resolve_ctx` 524 reads keys onto `Ctx` (560-561); `template_text` 2549 writes the commented
  template. `finish_sync` refreshes `ctx.cfg` from the merged tree with `replace(ctx, cfg=...)` - it
  does not re-derive the other `Ctx` fields. The module docstring (25-46) carries the usage block and
  the exit table (36-42: 0/1/2/3/4/5/130) and only a tomllib note about the config - the key lists
  live in `CONFIG_STRINGS`, `template_text` and README *Configuration*.
- `parse_args` 3551: `sub = p.add_subparsers(..., metavar="{status,check,sync,ship,setup}")`; the
  common `--force` help text says "No effect on status, check or ship". `COMMANDS` 3542.
  `EXIT_INTERRUPTED = 130` at 77.
- Git floor: README says git 2.20+; nothing in the script calls `git switch` (2.23+) - only the
  printed advice at 2010/2374 does. `land` therefore uses `git checkout`.
- Docs: README `## forkflow` - the stance sentence, *The six hard rules*, *The four skills*, the
  usage block, *Configuration*, *Exit codes* (row 0 says "Also `--mr` when the tool is missing or
  fails - the branch is pushed and the command is printed"), *After the MR is merged*, *Tests* (states
  the invariants in prose, including "`merge --ff-only` only in `advance_mirror`"), *Versions*
  (0.1.1 current). `plugins/forkflow/references/rules.md` rule 5 and a *Platform note*.
  `plugins/forkflow/skills/{status,sync,ship,setup}/SKILL.md`: sync:10 and ship:11 say "you never
  merge that MR yourself"; sync:98 says the closing step is "deliberately not automated"; sync:102 and
  ship:81 carry the shell closing step; ship step 7 says "exit stays 0" for a failed `--mr`; ship's
  description claims the trigger "land this branch". `plugins/forkflow/.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json` both describe "status/sync/ship/setup helpers".
- Tool facts checked on the installed versions: `glab mr merge` (1.116) enables **auto-merge by
  default** - `--auto-merge=false` is required for an immediate merge; it takes `--sha` (merge only if
  the source HEAD matches), `--remove-source-branch`, `--yes`, `-R/--repo` (URL accepted), and
  accepts the source branch name in place of an iid. `gh pr merge` (2.100) takes
  `<number>|<url>|<branch>`, `--merge`/`--rebase`/`--squash`, `--match-head-commit SHA`, `-R/--repo`.
- Test harness: `make_fork(tmp, config=...)` **commits** the config it is given (3719-3723), while a
  real `setup` leaves `.forkflow.toml` untracked - gate tests must cover the untracked case too.
  `record()` (~8441) writes argv with `>` into one file per tool name and prints a GitLab-shaped URL
  regardless of platform; `fake_tool()` writes a `#!/bin/sh` script; `identity()` sets a committer;
  `as_gitlab()`/`as_github()` force the platform. `TestOpenMr` calls `capture(open_mr, ...)` at
  seven sites (~8353, 8367, 8387, 8395, 8405, 8418, 8434), two of which assert `shown == ""`.

## Development Approach

- **testing approach**: Regular (code first, then tests) - each task ends by adding its cases to
  `run_tests()` in `forkflow.py` and running `python3 plugins/forkflow/scripts/forkflow.py --test`
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional - they are a required part of the checklist
  - tests live inside `forkflow.py` (`run_tests()`), never in `tests/`
  - tests cover both success and error scenarios (exit codes are part of the contract)
  - a guard needs a test that fails when the guard is removed
  - assert on state and decisions (`origin_sha`, refs, `pending`, the landed/not-landed verdict, the
    fake refusing a wrong sha), not on the wording of a line, wherever the two differ
- **CRITICAL: all tests must pass before starting next task** - no exceptions; a later task must not
  turn an earlier task's tests red (Task 3's tests are written to survive Task 4's chaining)
- **CRITICAL: update this plan file when scope changes during implementation**
- stdlib only, Python 3.9+ syntax (no `match`, no `X | Y` types); git 2.20+ (`git checkout`, not
  `switch`); config-dependent tests guarded with the existing `needs_tomllib`
- nothing in this plan pushes, rebases or commits on the trunk or the mirror. `TestSourceInvariants`
  gets exactly the two edits named in Technical Details and nothing else - if a fix appears to need
  a third, the fix is wrong

## Testing Strategy

- **unit tests**: required for every task, embedded in `forkflow.py`, run with `--test`; the existing
  harness (`make_fork`, `commit_upstream`, `commit_fork`, `second_clone_commit`, `run`, `capture`,
  `origin_sha`, `fake_tool`, `identity`, `as_gitlab()`/`as_github()`) is reused, not duplicated
- **the platform merge is simulated, not faked away.** One fake, defined in Task 3 and reused by
  Tasks 4 and 5, `merging_tool(name)`: a `#!/bin/sh` script on `PATH` that
  1. records its argv one per line into `<name>-<subcommand>-argv.txt` (`create` and `merge` get
     separate logs - the same binary is run twice under `--merge`, and `record()`'s single
     truncating log would lose the create argv and the body copy);
  2. on `create`, keeps a copy of the description file and prints a URL in the platform's shape -
     `https://example.invalid/-/merge_requests/1` for `glab`, `https://example.invalid/pull/1` for `gh`;
  3. on `merge`, reads the sha from its own `--sha` / `--match-head-commit` argument, compares it with
     the tip of the source branch in the bare `origin.git`, and **exits 1 with "head mismatch" when
     they differ** - so the head-commit guard is tested as behaviour, not as an argv string;
  4. then performs the merge the way the platform would: for a fast-forward (gitlab; github `--merge`
     of a sync whose tip is the merge commit) `git --git-dir=<tmp>/origin.git update-ref
     refs/heads/<trunk> <sha>`; for a github `--rebase` of a ship, a non-bare temp clone of
     `origin.git` with `identity()`, `git cherry-pick <sha>` on the trunk (a one-commit rebase: new
     SHA, same patch), `git push origin HEAD:<trunk>` - a bare repository cannot rebase;
  5. a test that wants the merge to fail sets `FORKFLOW_FAKE_FAIL=1` in the environment; the fake
     then exits 1 with a stderr line and moves nothing.
  With that fake, `--merge` -> `land` is testable end to end with no network.
  ➕ review fix (phase 1): the fake was made to match the platforms where it did not - gh
  `--merge` makes a real `--no-ff` merge commit in the temp clone (GitHub's "Create a merge
  commit" always does), glab's fast-forward refuses when the trunk is not an ancestor of the tip
  ("rebase needed") instead of rewinding it, glab without `--auto-merge=false` arms auto-merge,
  exits 0 and moves nothing, `--remove-source-branch` deletes the source branch on origin, and
  `FORKFLOW_FAKE_CREATE=fail|nourl` fails `create` or gives no URL. Tests that read the shipped
  commit from `origin/<branch>` after a glab merge read it from the recorded `--sha` instead.
- **what the suite cannot prove**: that real `glab mr merge` / `gh pr merge` accept these flags and
  merge - the fake proves the argv shape and the chain. Real validation happens on the GET fork's next
  ship after the plugin is updated to 0.2.0 (Post-Completion)
- no e2e/UI tests apply; `python3 -m unittest discover -s tests` (nocomment) must stay at 24 OK

## Progress Tracking

- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview

Two additions and one record, in the existing single-script shape:

- **`merge` config key** (`"manual"` default | `"self"`), read like every other key, on `Ctx`. The
  gate for `--merge` is *config AND flag*: the fork declares its model once in `.forkflow.toml`
  (tracked or not - `load_config` reads the working tree, which is where `setup` leaves the file);
  the flag asks for it per run; either alone does nothing.
- **`--merge`** on `ship` and `sync` implies `--mr`, is refused with exit 2 before anything is
  pushed unless `merge == "self"` and the fork's origin can be named, and after `open_mr` runs the
  platform's merge command with the method rule 5 requires and a head-commit guard. A merge request
  that was not created or not merged is the new **exit 6** - everything before it stands (branch
  pushed), so the code says exactly that.
- **`pending` state record**, written by every `ship`/`sync` that pushed a branch (with or without
  `--mr`): what is waiting to land and how to recognise it.
- **`land`**: fetch, recognise the landing (by ancestry; for a ship, by patch equivalence when the
  platform or a human rewrote the SHA), check out the trunk and fast-forward it, delete the landed
  local branch, clear `pending`, leave you on the trunk. `--merge` ends by running it. `land --force`
  is the escape hatch for a landing the tool cannot verify. `status` shows the pending entry and
  whether it has landed.

Key design decisions (from the brainstorm and the review; not to be reopened during implementation):

- **The method the project is configured for, on GitLab; per call on GitHub.** `glab mr merge` gets
  neither `--squash` nor `--rebase`: the project's `merge_method` decides - `setup`'s report insists
  on `ff`, under which a ship fast-forwards and a sync's tip (already the merge commit) fast-forwards
  too - but `setup` only reports that setting, so `land` checks the shape of what landed and says
  loudly when rule 5 was not honoured. `gh pr merge` gets `--merge` for a sync and `--rebase` for a
  ship because GitHub has no project-level method.
- **Head-commit guard always.** `--sha <head>` / `--match-head-commit <head>`: merge only the exact
  commit this run pushed, never whatever the branch points at by then - rule 4's spirit.
- **`--auto-merge=false` is mandatory** for glab; otherwise "merge" silently becomes "merge when the
  pipeline passes" and `land` finds nothing landed.
- **Landing is recognised with git only** - no API call in `land` or `status`: ancestry first, then
  for a ship `git cherry` (patch equivalence) over `<base>..origin/<trunk>`, which survives a rebase
  merge, a squash merge, and other commits landing in between. Tree equality is not used: it is
  wrong the moment anything else lands first.
- **The MR is addressed by its source branch**, which both tools accept and the run already knows.
  No URL parsing; the URL is kept only for display and the `pending` record.
- **`pending` holds the most recent run only.** A second ship before the first lands overwrites it;
  `land` handles one entry. `land --force` clears an entry whose MR was closed instead of merged.
- **Out of scope**: pipeline waiting (a required pipeline answers exit 6; merge when green), remote
  branch deletion on GitHub (a printed command; GitLab's `--remove-source-branch` already covers it),
  approvals logic, backup pruning (its own backlog item).

## Technical Details

### Config

`.forkflow.toml` key `merge`, default `"manual"`; `"self"` means *this fork's MRs are merged by the
person who opened them*. `merge` is added to `CONFIG_STRINGS` so `parse_config` type-checks it in the
same wording as every other key, then a membership check refuses anything but `"manual"`/`"self"`
with `Fail(2)`. `Ctx.merge: str = "manual"`, set in `resolve_ctx`. `finish_sync`'s
`replace(ctx, cfg=load_config(...))` refreshes `cfg` only and deliberately does **not** re-derive
`Ctx.merge`: the value the gate checked is the value the merge step uses. `template_text` writes:

```toml
# merge = "manual"            # "self": this fork's MRs are merged by whoever opened them - enables --merge
```

### Flags and dispatch

- `sync` and `ship` gain `--merge` ("open the merge request and merge it; needs merge = \"self\" in
  .forkflow.toml"). `parse_args` sets `args.mr = True` when `args.merge` is set.
- New subcommand `land` (`parents=[common]`); the subparsers `metavar` gains `land`; the common
  `--force` help text becomes "No effect on status, check or ship; `land --force` fast-forwards a
  landing the tool cannot verify". `COMMANDS["land"] = cmd_land`. Docstring usage: `forkflow.py land
  [--force] [-C DIR] [--dry-run]`, and `[--merge]` on the sync/ship lines. The docstring gains
  nothing else (its config note is only about tomllib).

### The gate

Two conditions, both checked before the fetch, the backup and any push:

1. `args.merge and ctx.merge != "self"` -> `Fail(f"--merge needs `merge = \"self\"` in
   {CONFIG_FILE}: this fork's merge requests are merged by hand", 2)`.
2. `args.merge and not mr_target(ctx)[0]` -> `Fail(f"--merge: `{ctx.origin_url}` names no project
   ... ({reason})", 2)` - knowable now, so not a push followed by an exit 6.

⚠️ review fix (phase 1): on `sync --continue` the working tree is the sync's merge, upstream's
`.forkflow.toml` included, so a `merge = "self"` the original project carries switched condition 1
off. `merge_gate(..., resume_sync=True)` now also refuses (exit 2, before anything is pushed) when
the working-tree `merge` differs from the fork's side of the merge - `HEAD` while it is
uncommitted, `<merge>^1` once committed - the comparison `gate_arrived_in_merge` makes for `gate`;
an untracked file is the fork's own and is trusted (`merge_mode_arrived_in_merge`).

⚠️ review fix (phase 1, iteration 2 - pre-existing on `main`): a config name differing only in
case evaded both that check and the `gate` guard. On a case-insensitive filesystem an upstream
`.ForkFlow.toml` is what `open(".forkflow.toml")` reads, while `git show <rev>:.forkflow.toml` and
`ls-files` match case-exactly and saw no config on either side - so a plain `sync` ran upstream's
`gate` and `sync --continue --merge` merged. `load_config` now reads only a file listed under
exactly `.forkflow.toml` and refuses a case variant (exit 2, naming it); "is it tracked"
(`config_tracked`, a `:(icase)` pathspec) and "what did this revision carry" (`config_text`, the
exact name first, else a case variant in the tree) treat a variant as the file.

Placement: in `cmd_ship`, inside/after `ship_preflight` (which already runs before its `--continue`
branch). In `cmd_sync`, **immediately after `header(ctx, "sync")` and before the `--continue`
dispatch** - `cmd_sync` dispatches `--continue` on its fourth line, before any preflight, and a
conflicted sync is exactly when `--continue` is used. A `--dry-run` with `--merge` on a `"self"` fork
prints `would: merge ...` and `would: land` in place of the two steps and writes no state.

### `run_tool` and the first invariant edit

The tool-running body of `open_mr` (the `subprocess.run(..., stdin=DEVNULL, capture_output=True)`
call and its `OSError` handling) moves into `run_tool(ctx, cmd) -> Optional[CompletedProcess]`
(`None` when the tool is missing), used by `open_mr` and `merge_mr`. **Invariant edit 1:** the
pinned owner set of the word `subprocess` in
`test_git_and_the_platform_tools_are_the_only_subprocesses` replaces `open_mr` with `run_tool` - a
rename of the one owner, not a widening. The test's docstring says so.

### MR identity

`open_mr` returns `(shown, url)`; `url` is the first stdout line starting with `http`, `""`
otherwise (tool missing, failed, not run, dry run). The two production callers discard the return
value today; the churn is `TestOpenMr`'s seven `capture(open_mr, ...)` sites, two of which become
`assertEqual(shown, ("", ""))`. No URL parsing: the merge command addresses the MR by its source
branch, which `glab mr merge` and `gh pr merge` both accept and the run already has.

### The merge step

`merge_command(ctx, kind, branch, head) -> list`:

```
gitlab: glab mr merge <branch> --repo <mr_target> --sha <head> --auto-merge=false --remove-source-branch --yes
github: gh pr merge <branch> --repo <mr_target> --match-head-commit <head> --merge      (kind == "sync")
        gh pr merge <branch> --repo <mr_target> --match-head-commit <head> --rebase     (kind == "ship")
```

`--remove-source-branch` duplicates the flag already given at `mr create` - harmless and kept, so
the merge command is complete on its own. The `"--rebase"` argument is a gh flag, not a git verb; it
does not match the invariant's `"rebase"` needle and must not be "simplified" into a form that does.

`merge_mr(ctx, kind, branch, url) -> None` runs `run_tool`, prints `step("merge", shown, "merged")`
with the tool's stdout indented, or on any failure `step("merge", shown, f"NOT MERGED (exit N)")`
plus the stderr tail and `Fail("the merge request was not merged - the branch is pushed; merge it by
hand, then: forkflow land", 6)`. `--merge` when `open_mr` produced no URL is `Fail("the merge
request was not created - the branch is pushed; open and merge it by hand, then: forkflow land", 6)`.
In `finish_sync`/`finish_ship`: after `open_mr`, after the `pending` record has its URL (so a failed
merge leaves a landable record), when `args.merge`. On success, `land_pending(ctx)` runs in the same
process. **If the merge succeeded and `land_pending` then fails**, the exit is its exit 2 and the
message opens with `the merge request was merged; the local catch-up did not run: <reason>` so
nobody reads a merged MR as "nothing happened".

⚠️ as built: `land_after_merge` returns at once under `--dry-run` (`merge_mr` has already printed
`would: land`, and a dry run wrote no record for `land_pending` to find). ➕ review fix: when the
tool exits 0 but the recorded commit is not on `origin/<trunk>` (a merge train, auto-merge, a
required pipeline), `land_pending(after_merge=True)` raises exit 6 "the platform tool reported the
merge request merged, but ..." and `land_after_merge` passes it on unwrapped - that request is not
merged, so the "was merged" opening would be false. ➕ `--continue` hints printed by a run that had
`--merge` (or `--mr`) carry the flag (`continue_cmd`). ⚠️ iteration 2: `ship_preflight`'s "a rebase
is in progress" hint too (it takes `args`; the plain-`ship` form for a rebase that is not ours
carries the flag as well).

➕ as documented (iteration 2): in the usual worktree layout - trunk checked out in the main
worktree, work in a linked one - every `--merge` from the linked worktree ends in that exit 2: the
trunk is fast-forwarded only where it is checked out (`trunk_elsewhere`), so the message names
`forkflow land <branch>` for the main worktree, and the branch stays while the linked worktree
has it. Behaviour unchanged; ship/sync SKILL.md and the README say it plainly.

### Exit code 6

Docstring table, README table, `skills/sync/SKILL.md` and `skills/ship/SKILL.md` exit rows:
`6   merge request not created or not merged (--merge only) - the branch is pushed; merge by hand,
then forkflow land`. Row 0's "Also `--mr` when the tool is missing or fails ..." gains "without
`--merge`", in README and both skills. `main()` needs no change.

### The `pending` record

Written by `finish_sync` and `finish_ship` right after the push succeeds and before `open_mr` (so it
exists even when the MR step fails), then rewritten with the URL once known:

```json
"pending": {"kind": "ship", "branch": "feat/x", "commit": "<sha of HEAD after squash>",
            "base": "<origin/<trunk> sha at push time>", "mr": "<url or empty>"}
```

For a sync `commit` is the merge commit (`HEAD` of the sync branch after the merge). Read through
`pending_entry(ctx) -> dict`, which returns `{}` for anything that is not a dict carrying `kind`,
`branch`, `commit` and `base` as strings - the `resumable` pattern. `write_state` already skips
`--dry-run`. The existing `write_state(ctx, kind, None)` (the resume entry) stays.

⚠️ review fix (phase 1): the state file is per worktree (`git rev-parse --git-path`), so a ship
from a linked worktree while the trunk was checked out in the main one dead-ended - `land` there
said "nothing pending", `land` in the linked one named the main worktree and offered "remove
that worktree", which the main one cannot be. `pending` now lives in the state file under
`git rev-parse --git-common-dir` (`SHARED_STATE`; the same file as the main worktree's own), so
every worktree sees one record; the resume entries and `published` stay per worktree. The
worktree refusal names only `forkflow land` in that worktree - a route that works. `save_state`
writes a temp file and renames it over the old one.

⚠️ review fix (phase 1, iteration 2): one shared record let parallel worktrees overwrite each
other's (W2's ship replaced W1's; `land` in W1 landed W2's branch; `--merge` re-read and landed
whatever the file held). `pending` is now a map keyed by branch in the shared file, which
supersedes "holds the most recent run only" above: a second ship of the same branch replaces its
entry, ships of other branches keep theirs, and the single bare entry an earlier build wrote reads
as a map of one. `record_pending` returns the entry it wrote and `--merge` lands that one
(`land_after_merge(ctx, entry)`), never a re-read. `land` takes `land <branch>`'s record when named
(a new optional positional - without it `--force` had no way to pick one record in the
"trunk in the main worktree" layout), else the current branch's, else every record: each verified
one lands (one fast-forward, then each branch), the rest are listed and kept, none verified is
exit 2; `--force` with several and none named is exit 2. An entry is cleared only while it still
has the branch and commit that landed, read again right before the write (`forget_pending`).
`status` prints one `pending` line per entry; the worktree refusal names `forkflow land <branch>`.

### `land`

`cmd_land(args)` -> `resolve_ctx(need_upstream=True, need_trunk=True, strict_mirror=False)`, header,
then `land_pending(ctx, force=args.force)`:

```
preflight  pending_entry(ctx) non-empty (else 2: "nothing pending: ship or sync first")
           clean tree (else 2) ; no rebase in progress (else 2)
           trunk not checked out in another worktree (for-each-ref %(worktreepath), as mirror_worktree
           does) - else 2 naming the path
           ⚠️ review fix: this preflight is the only worktree check (land_trunk's copy dropped) and
           the message names `forkflow land` there only (the pending record is shared)
fetch      git fetch <origin>                                   (git_rc; failure -> 2)
landed?    landed(ctx, entry) -> (sha | None, how):
             1. git merge-base --is-ancestor <commit> origin/<trunk>      rc 0 -> (<commit>, "ancestor")
                rc 128 -> Fail(2) "cannot verify: <commit> is not in this clone"
             2. kind == "ship" only: git cherry origin/<trunk> <commit> <base>  - a line beginning
                with "-" means an equivalent patch is already on the trunk -> (that trunk commit found
                by patch-id over <base>..origin/<trunk>, "rewritten")
             3. otherwise None
           ⚠️ as built: step 2 is one `git cherry <commit> origin/<trunk> <base>` (the trunk commits
           in <base>..origin/<trunk>, marked `-` when they carry the shipped patch - the landed
           trunk commit comes straight from that line), no `git patch-id` pipe (a new subprocess
           owner the invariants forbid). ➕ review fix: an empty ship skips step 2 (an empty patch
           matches every empty commit), and a `-` match counts only while the trunk's tip still
           has the change (`still_carries`: `merge-tree --write-tree origin/<trunk> <commit>`
           gives back the trunk's own tree; a conflict or git < 2.38 keeps git cherry's answer) -
           a patch reverted since is not a landing. A commit not in this clone raises
           "cannot verify"; ➕ `--force` catches up past that too (it verifies nothing anyway).
           None and not force -> exit 2: "MR <url or branch> is not on origin/<trunk> yet"
                                  + for a sync: "if it was squashed or rebased in the UI, rule 5 was
                                  broken (see rules.md); `land --force` fast-forwards anyway"
           None and force     -> proceed to the fast-forward, delete no branch, clear pending, and say
                                  "landing not verified (--force)"
rule 5     shape of what landed, reported not enforced:
             ship landed as a merge commit (the landed sha has 2 parents) -> WARNING "the ship MR was
               merged as a merge commit - rule 5 asks for a fast-forward; check the project's merge method"
             ⚠️ as built: judged by whether the shipped commit is on the trunk's first-parent line
               (`git rev-list --first-parent <base>..origin/<trunk>`) - an ancestor landing keeps
               the ship's own single-parent SHA, so counting the landed sha's parents never warned
             sync landed "rewritten" is impossible (step 2 is ship-only); a sync that is not an ancestor
               is the exit-2 message above
land_trunk local trunk absent (single-branch clone) -> git branch --no-track <trunk> origin/<trunk>
           local trunk not an ancestor of origin/<trunk> -> 2: "<trunk> has commits origin lacks - the
             plugin never creates these; resolve by hand"
           git checkout <trunk>            ALWAYS (HEAD is usually on the branch about to be deleted;
                                            git 2.20+, so checkout not switch); failure -> 2 with git's line
           git merge --ff-only origin/<trunk>   via git_rc; a refusal here is unreachable after the
                                            ancestor check but still exit 2 with git's line
delete     git branch -d <branch>  when it is neither the trunk nor the mirror and the landing was
           "ancestor"; when "rewritten", -d refuses (the local commit is unreachable from the trunk)
           and -D is used - the patch is verifiably on the trunk. Never under --force with no landing.
           ⚠️ review fix: deleted only while refs/heads/<branch> still equals pending.commit - a
           commit made on the branch after the ship landed nowhere, and -D destroyed it (-d alone
           is no guard: push sets -u, so -d accepts a tip origin/<branch> contains). A moved
           branch is kept with "kept: <branch> is at <tip>, not the <commit> that was pushed".
           ⚠️ as built: a `git branch -d/-D` git refuses after the trunk moved ("NOT deleted: <git's
           line>") still exits 0 with the record cleared - the landing is done.
           ➕ review fix: after a verified landing, an `origin/<branch>` that origin no longer has
           (GitLab's --remove-source-branch) is dropped (`git branch -d -r`), or the next ship of
           that name offers it as a lease and is refused "(stale info)".
           ⚠️ iteration 2: dropped under an unverified `--force` too (the kept branch is the
           one shipped again; the decision is origin's `ls-remote` answer, not the landing),
           and `cmd_ship` itself drops a stale `origin/<branch>` when `ls-remote` says origin
           has no such branch, then pushes it as a new branch without a lease (nothing on origin
           to force away, so no ownership to prove; the pre-ship backup is made as always; a
           plain push only fast-forwards whatever appears there meanwhile).
clear      write_state(ctx, "pending", None)
print      "landed: <trunk> <old>..<new> - you are on <trunk>" ; github ship: "origin/<branch> may
           still exist: git push <origin> --delete <branch>"
           ➕ review fix: the hint is printed on GitHub for a ship and a sync alike, only after a
           verified landing and only while origin still has the branch; an unverified `--force`
           ends "caught up, landing NOT verified: ..." instead of "landed: ..."
exit 0 ; --dry-run prints every mutating step as would: and moves nothing
```

Every printed command goes through `sh_arg`. **Invariant edit 2:** `land_trunk` is the second owner
of the `"merge", "--ff-only"` needle. `test_only_advance_mirror_moves_the_mirror` keeps
`"update-ref"` pinned to `advance_mirror` under its existing name; the ff-only assertion moves to a
new test, `test_the_two_fast_forwards_are_the_mirror_and_the_trunk_landing`, asserting
`{"advance_mirror", "land_trunk"}` with the reason in its docstring. `git branch -f` stays blocked
(`owners('"-f"') == set()`), which is why the fast-forward is a merge and not a ref move.

`land` leaves HEAD on the trunk. `cmd_sync`'s "you stay on it when this finishes" message becomes
"... unless --merge lands it" when `args.merge` is given.

### `status`

After the setup line, when `pending_entry(ctx)` is non-empty: `pending  <kind> <branch> -> MR
<url|-> - not on origin/<trunk> yet` or `... - landed: run forkflow land`, using `landed()` on the
refs as they are (after the fetch with `--fetch`; identical under `--offline` - the check is
git-only). `status` resolves with `need_trunk=False`, so when `origin/<trunk>` is absent or the
commit is not in this clone the line degrades to `... - cannot verify here` and `status` still exits
0; it never raises for this line.

### Edge cases (each is a test)

- `--merge` on a `"manual"` fork, with the config **committed** and, separately, **untracked** as
  `setup` leaves it: exit 2 before any backup, branch or push - `origin_sha(trunk)` unchanged, no
  `backup/*` local or on origin, no sync branch, no state entry.
- `--merge` without a config file at all: `"manual"` by default -> the same exit 2.
- `sync --continue --merge` and `ship --continue --merge` on a `"manual"` fork: exit 2, nothing pushed.
- `--merge` on a fork whose origin cannot be named (the fixture's local-path origin without
  `as_gitlab()`): exit 2 before any push.
- `--merge --dry-run`: `would: merge`, `would: land`, nothing run, no state.
- the fake refuses a mismatched head sha: exit 6, `pending` intact, trunk unchanged.
- merge tool fails (`FORKFLOW_FAKE_FAIL`) / MR not created: exit 6, `pending` written, `land` later
  works once the bare origin's trunk is moved by the test (a human merged it).
- merge succeeds but the catch-up cannot run (dirty tree created by the test between - or simplest,
  the local trunk given a commit of its own before `ship --merge`): exit 2, message opens with "the
  merge request was merged".
- `land` with nothing pending / dirty tree / mid-rebase / trunk checked out in another worktree /
  not yet merged: exit 2 each, message names which.
- `land` when the local trunk carries a commit origin lacks: exit 2, trunk untouched.
- `land` while HEAD is on the branch being deleted (the normal `--merge` path) and while HEAD is on
  an unrelated branch: both end on the trunk with the landed branch gone.
- `land` for a ship rewritten by the fake's `--rebase` (new SHA, same patch), and after another
  commit reached the trunk between the push and the merge: recognised, branch deleted with `-D`.
- `land` for a ship squashed by "a human" (the test squashes in a temp clone and pushes): recognised.
- `land --force` on an entry whose MR was closed (nothing landed): trunk fast-forwarded to whatever
  origin has, branch kept, `pending` cleared, "not verified" printed.
- a ship that landed as a merge commit: landed, with the rule-5 WARNING.
- `land` when the local trunk branch does not exist (single-branch clone): created from `origin/<trunk>`.
- `status` pending line in both states, absent when nothing is pending, `cannot verify` when
  `origin/<trunk>` is absent, unchanged under `--offline`, and `{}` for a malformed entry.
- `pending` written by ship and sync without `--mr` (mr empty), and with `--mr` (URL taken from the
  fake's output, GitLab and GitHub shapes).
- `mr_target` naming rules unchanged: existing `TestMrCommandsNameTheFork` still passes.

## What Goes Where

- **Implementation Steps** (`[ ]` checkboxes): everything below - script, tests, skills, docs
- **Post-Completion** (no checkboxes): real-fork validation, plugin update on the fork machines

## Implementation Steps

### Task 1: `merge` config key and the `--merge` gate

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] add `merge` to `CONFIG_STRINGS`; in `parse_config` the `("manual", "self")` membership check with `Fail(2)` in the other keys' wording; `Ctx.merge` field set in `resolve_ctx`
- [x] `template_text`: the commented `merge` line with its one-line explanation (Technical Details / Config)
- [x] `parse_args`: `--merge` on `sync` and `ship` implying `--mr`; module docstring usage lines gain `[--merge]`
- [x] the gate, both conditions (config, and `mr_target` can name the origin): in `cmd_ship` inside/after `ship_preflight`; in `cmd_sync` immediately after `header(ctx, "sync")` and before the `--continue` dispatch
- [x] write tests: config parse - `"self"`, default, bad value -> 2 (`needs_tomllib`); template contains the key; `parse_args` sets `mr` from `merge`
- [x] write tests: `--merge` refused before any push on a `"manual"` fork with the config committed, with it untracked (written into the working tree, not through `make_fork(config=)`), and with no config at all; on `sync --continue --merge` and `ship --continue --merge`; and on an unnameable origin: exit 2, `origin_sha(develop)` unchanged, no `backup/*` local or on origin, no sync branch, no state entry
- [x] run `python3 plugins/forkflow/scripts/forkflow.py --test` - must pass before task 2

### Task 2: `run_tool`, `open_mr` returning the URL, and the `pending` record

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] extract `run_tool(ctx, cmd) -> Optional[CompletedProcess]` from `open_mr` (the `subprocess.run` call with `stdin=DEVNULL` and the `OSError` path); `open_mr` uses it; invariant edit 1: `test_git_and_the_platform_tools_are_the_only_subprocesses` replaces `open_mr` with `run_tool` in the pinned set, with the reason in its docstring
- [x] `open_mr` returns `(shown, url)`; `url` = first stdout line starting with `http`, else `""`; update the two production call sites (they discard it) and `TestOpenMr`'s seven `capture(open_mr, ...)` sites, two of which become `assertEqual(shown, ("", ""))`
- [x] `pending_entry(ctx) -> dict` (defensive read, `resumable` pattern); `finish_sync`/`finish_ship` write `pending = {kind, branch, commit, base, mr}` after the push succeeds and before `open_mr` (`base` captured as `origin/<trunk>` before the push), then rewrite it with the URL once `open_mr` returns
- [x] write tests: `pending` shape for ship (commit = squashed HEAD) and for sync (commit = merge commit), with and without `--mr`, URL taken from the fake's output in both platform shapes; `--dry-run` writes no state; `pending_entry` returns `{}` for a list, a dict missing `commit`, and non-string fields
- [x] write tests: `run_tool` returns `None` for a missing tool and the `CompletedProcess` otherwise; `TestSourceInvariants` green with the renamed owner
- [x] run tests - must pass before task 3

### Task 3: `merge_command`, `merge_mr`, exit 6, and the merging fake

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] `merge_command(ctx, kind, branch, head)`: the per-platform argv exactly as in Technical Details (glab: `--sha`, `--auto-merge=false`, `--remove-source-branch`, `--yes`, no `--squash`/`--rebase`; gh: `--match-head-commit`, `--merge` for sync, `--rebase` for ship; both `--repo <mr_target>`, the MR addressed by branch name); `[]` when `mr_target` cannot name the fork
- [x] `merge_mr(ctx, kind, branch, url)`: `run_tool`, `step("merge", ...)`, stdout indented on success; failure -> stderr tail + `Fail(..., 6)`; no URL -> `Fail("... was not created ...", 6)`; `--dry-run` -> `would: merge` and `would: land`
- [x] wire into `finish_sync`/`finish_ship` after the `pending` rewrite, when `args.merge`; until Task 4 the success path ends with `next: forkflow land` printed (Task 4 replaces that line with the chained `land_pending`); add `6` to the docstring exit table; adjust `cmd_sync`'s "you stay on it" message under `--merge`
- [x] the harness fake `merging_tool(name)` exactly as specified in Testing Strategy (per-subcommand argv logs, platform-shaped URL, sha read from argv and refused on mismatch, trunk moved by `update-ref` or by a cherry-pick-and-push in a temp clone for github `--rebase`, `FORKFLOW_FAKE_FAIL`)
- [x] write tests (with `merging_tool`, `as_gitlab()`/`as_github()`, URL-driven `Ctx`): argv per platform - `--repo` names the fork, `--sha`/`--match-head-commit` equals the pushed HEAD, `--auto-merge=false`, `--merge` vs `--rebase` by kind, no `--squash`, `--yes`; exit 0 and the bare origin's trunk moved to the shipped/synced commit. Do NOT assert `pending` after a successful `--merge` here - Task 4's chaining clears it; assert it only on the failure paths
- [x] write tests: the fake refuses a mismatched sha -> exit 6, trunk unchanged, `pending` intact; `FORKFLOW_FAKE_FAIL` -> exit 6 "NOT MERGED", MR still reported open, `pending` intact; `--merge` with the create step failing -> exit 6 "not created"; `--mr` without `--merge` runs no merge; `--merge --dry-run` runs nothing and writes nothing
- [x] run tests - must pass before task 4

### Task 4: `land`

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] `landed(ctx, entry) -> Tuple[Optional[str], str]`: ancestry (`merge-base --is-ancestor`, rc 128 -> `Fail(2)` "cannot verify"), then for `kind == "ship"` `git cherry origin/<trunk> <commit> <base>` and the matching trunk commit by `git patch-id --stable` over `<base>..origin/<trunk>`; `(None, "")` otherwise
  - ⚠️ built as one `git cherry <commit> origin/<trunk> <base>`, no `patch-id` (see *land* in Technical Details, which also has the review fixes: empty ship, reverted patch)
- [x] `land_trunk(ctx)`: local trunk absent -> `git branch --no-track <trunk> origin/<trunk>`; local trunk not an ancestor of `origin/<trunk>` -> `Fail(2)`; trunk checked out in another worktree -> `Fail(2)` naming it; `git checkout <trunk>` always; `git merge --ff-only origin/<trunk>` via `git_rc`; invariant edit 2: the ff-only assertion moves out of `test_only_advance_mirror_moves_the_mirror` (which keeps `update-ref` pinned) into `test_the_two_fast_forwards_are_the_mirror_and_the_trunk_landing` asserting `{"advance_mirror", "land_trunk"}`
- [x] `land_pending(ctx, force)`: preflight (pending, clean tree, no rebase, worktree) -> fetch (`git_rc`, failure -> 2) -> `landed()` (None -> exit 2 with the MR named and the rule-5 note for a sync; `--force` proceeds unverified) -> rule-5 shape warning for a ship that landed as a merge commit -> `land_trunk` -> `git branch -d` (or `-D` for a "rewritten" landing; never under `--force` without a landing) -> `write_state(ctx, "pending", None)` -> `landed: ...` line and the github remote-delete hint; every mutating step `would:` under `--dry-run`; the merged-but-not-landed message contract when called from `--merge`
- [x] `cmd_land(args)`; `COMMANDS["land"]`; `parse_args` subparser `land` with `parents=[common]`, the subparsers `metavar` and the `--force` help text updated; docstring usage; `--merge` in `finish_sync`/`finish_ship` calls `land_pending` after `merge_mr` succeeds, replacing Task 3's `next: forkflow land` line; the shell catch-up line printed without `--merge` becomes `next: forkflow land`
- [x] write tests: `land` with nothing pending -> 2; dirty tree -> 2; mid-rebase -> 2; trunk checked out in a second worktree -> 2; not landed -> 2 with the MR named, trunk untouched, and the rule-5 note for a sync; landed by ancestry after the bare origin's trunk is moved -> 0, local trunk == `origin/<trunk>`, HEAD on the trunk, branch deleted, `pending` cleared; landed sync (merge commit) -> 0; landed by patch after the fake's `--rebase` (new SHA) -> 0 and after another commit reached the trunk first -> 0; a human squash in a temp clone -> 0; a ship landed as a merge commit -> 0 with the WARNING; local trunk with its own commit -> 2 and untouched; no local trunk -> created; `land` from HEAD on the landed branch and from an unrelated branch; `land --force` on a closed-MR entry -> trunk fast-forwarded, branch kept, `pending` cleared; `--dry-run` moves nothing and keeps `pending`
- [x] write tests: `--merge` end to end with `merging_tool` (gitlab ff; github `--rebase` for ship, `--merge` for sync) -> exit 0, trunk fast-forwarded locally, HEAD on it, branch deleted, `pending` cleared; merge succeeded but the catch-up refused (local trunk given a commit of its own beforehand) -> exit 2 opening with "the merge request was merged"; `TestSourceInvariants` green with both documented edits and nothing else
- [x] run tests - must pass before task 5

### Task 5: `status` pending line

**Files:**
- Modify: `plugins/forkflow/scripts/forkflow.py`

- [x] `cmd_status`: after the setup line, when `pending_entry(ctx)` is non-empty print the line in its three states (`not on origin/<trunk> yet` / `landed: run forkflow land` / `cannot verify here`), using `landed()` guarded so `status` never raises for it; unchanged under `--offline`
- [x] write tests: assert the landed/not-landed *decision* in both states (the bare origin's trunk moved or not), the line absent with nothing pending, `cannot verify` with `origin/<trunk>` absent (`make_fresh_fork` plus a hand-written entry), identical output under `--offline`, and a malformed entry ignored; `--fetch` flips the decision once the trunk moves
- [x] run tests - must pass before task 6

### Task 6: skills and rules

**Files:**
- Create: `plugins/forkflow/skills/land/SKILL.md`
- Modify: `plugins/forkflow/skills/sync/SKILL.md`
- Modify: `plugins/forkflow/skills/ship/SKILL.md`
- Modify: `plugins/forkflow/skills/status/SKILL.md`
- Modify: `plugins/forkflow/references/rules.md`

- [x] `land/SKILL.md`: frontmatter (`name: land`; description with the triggers "forkflow land", "the MR merged", "catch develop up", "catch the trunk up" - not "land this branch", which stays with `ship`; `allowed-tools: Bash, Read`); the same opening rules paragraph as the others; run `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py land`; a not-landed exit 2 is "not merged yet", never an error; `--force` only when the user says the MR was merged or closed in a way the tool cannot see; what it did (fast-forward, you are now on the trunk, branch deleted, GitHub remote-delete hint)
- [x] `sync/SKILL.md` and `ship/SKILL.md`: the closing step becomes `forkflow land`; delete sync:98's "deliberately not automated"; `--merge` documented with both gate conditions and the exact exception to "you never merge that MR yourself" (*unless the fork's `.forkflow.toml` says `merge = "self"` and the user asked for `--merge`*); note that `--merge` leaves you on the trunk; exit rows: row 0 gains "without `--merge`", row 6 added; ship's description drops "land this branch"? - no: keep it on `ship`, and make sure `land`'s description does not claim it
- [x] `status/SKILL.md`: the `pending` line's three states in the header format and the table
- [x] `rules.md`: rule 5 unchanged; one *Platform note* line: `--merge` uses the method the project is configured for on GitLab (which `setup`'s report insists is `ff`) and the method rule 5 requires per call on GitHub, always with a head-commit guard; `land` warns when what landed does not have that shape
- [x] verify every SKILL.md frontmatter parses with `name` == directory and every `${CLAUDE_PLUGIN_ROOT}` path resolves (the throwaway parser used for the original plan, or `python3 -c` with `json.loads` per value); verify no two skills claim the same trigger phrase
- [x] run `--test` - must still pass before task 7

### Task 7: Verify acceptance criteria

- [x] verify all requirements from Overview: `--merge` is inert on a `"manual"` fork (committed or untracked config, or none) and on an unnameable origin; refused before any push on every entry path incl. `--continue`; method per platform; head-commit guard proven by the fake's refusal; exit 6 leaves a landable state; `land` works after a human merge in a fresh process, after a rebase merge, after a squash merge, and with intervening commits; `land --force` is the only escape and never deletes an unverified branch; `status` shows pending and never raises for it
  - inert/refused before any push: `TestMergeGate` (committed, untracked, no config, `--continue` on both, unnameable origin - each asserts `origin_sha` of trunk and mirror, local refs, no `backup/*` on origin, no sync branch, no state file). Method per platform: `TestMergeCommand` + `TestMergeStep` (gitlab: no method flag, `--auto-merge=false`; github: `--merge` for sync, `--rebase` for ship). Head guard: `TestMergeStep.head_moved` on both platforms - the fake's "head mismatch" refusal, exit 6, trunk unchanged. `land` after a human merge: `TestLand` (`run()` is a fresh `main()` per call; state comes only from `.git/forkflow-state.json`) - fast-forward, merge commit, cherry-pick (rebase), cherry-pick behind an intervening commit, squash, `--no-ff` merge with the WARNING. `--force`: `test_force_fast_forwards_an_unverified_landing_and_keeps_the_branch` (branch kept, record cleared) and `test_force_deletes_the_branch_when_the_landing_is_verified`. `status`: `TestStatusPending` via `pending_verdict` (never raises; `cannot verify` in two ways).
  - ➕ gap closed: "exit 6 leaves a landable state" was proven only by the record's shape (`pending` intact); no test then merged by hand and ran `land`. Added `TestMergeLands.test_a_refused_merge_leaves_a_record_a_by_hand_merge_lands_from` (`FORKFLOW_FAKE_FAIL` -> exit 6 -> bare origin's trunk moved -> `land` exit 0, `assert_landed`).
- [x] verify the six hard rules are untouched: grep as a backstop that `push` still appears only in the three helpers, `rebase` only in `rebase_onto`, `subprocess` only in the pinned set with `run_tool` in place of `open_mr`, the only `merge --ff-only` owners are `advance_mirror` and `land_trunk`, and `--force`/`--no-verify`/`branch -f` nowhere in a git call
  - independent owner mapping (a regex script, not the AST test) over the code above `run_tests`: `"push"` -> {push, push_mirror, bootstrap_trunk}; `"rebase"` -> {rebase_onto}; `subprocess` -> {git, git_ok, git_rc, shell, run_tool, api_get, <module>}; `"merge", "--ff-only"` -> {advance_mirror, land_trunk}; `"update-ref"` -> {advance_mirror}; `"--force"` -> {parse_args} only; `--no-verify`, `"-f"`, `'push'`, `git('`, `git_rc('` -> none; `force-with-lease` -> {push, <module>}. `git diff main` restricted to `TestSourceInvariants` shows exactly the two documented edits (the ff-only assertion moved into `test_the_two_fast_forwards_are_the_mirror_and_the_trunk_landing`; `open_mr` -> `run_tool` with the docstring) and nothing else.
- [x] verify every edge case in Technical Details has a test, and that no test asserts on a line's wording where it could assert on the decision
  - every bullet under *Edge cases* maps to a test in `TestMergeGate`, `TestMergeStep`, `TestLand`, `TestMergeLands`, `TestStatusPending`, `TestPendingRecord`, `TestPendingEntry`; `TestMrCommandsNameTheFork` still passes. Wording assertions that remain are all alongside the state they describe (`(ancestor)`/`(rewritten)` and `-d`/`-D` are the decision itself; `landed: develop old..new`, `you are on develop`, `merged` sit next to `assert_landed`; the merged-but-not-landed `startswith` is the plan's message contract; `TestMergeGate`'s `--merge needs` vs `names no project` tells the two gate conditions apart).
  - ➕ gap closed: `TestLand.test_a_missing_local_trunk_is_created_from_origin` proved `--no-track` only by the printed command; it now also asserts `branch.develop.remote`/`.merge` are unset. `test_the_ship_of_another_clone_cannot_be_verified_here` asserted only the trunk untouched; it now also asserts HEAD and the record unchanged.
- [x] run full test suite: `python3 plugins/forkflow/scripts/forkflow.py --test` and `python3 -m unittest discover -s tests` (nocomment untouched at 24) - 424 OK in 284 s; nocomment 24 OK
- [x] load the plugin locally (`claude --plugin-dir plugins/forkflow -p ...`) and check `/forkflow:land` resolves alongside the other four; if not automatable here, verify the manifest and frontmatter non-interactively and mark skipped with the note - `claude --plugin-dir plugins/forkflow -p "List the exact names of every available skill that starts with 'forkflow'..."` answered `forkflow:land`, `forkflow:setup`, `forkflow:ship`, `forkflow:status`, `forkflow:sync`; typing the slash command interactively was not possible here (skipped - not automatable), the non-interactive load is the check

### Task 8: [Final] Update documentation

**Files:**
- Modify: `README.md`
- Modify: `plugins/forkflow/.claude-plugin/plugin.json`
- Modify: `.claude-plugin/marketplace.json`
- Delete: `docs/backlog/no-post-merge-catch-up-step.md`

- [x] README `## forkflow`: the stance sentence becomes "moves the local trunk only by fast-forward, through `land`, and leaves you on it"; *The four skills* becomes five with a `land` row; usage block gains `land [--force]` and `[--merge]`; *Configuration* gains `merge`; *Exit codes* row 0 gains "without `--merge`" and row 6 is added; *After the MR is merged* becomes `forkflow land` (with `--merge` as the solo-fork shortcut, `land --force` as the escape, and the GitHub remote-delete note kept); *Tests* paragraph's invariant prose gains `land_trunk` as the second `merge --ff-only` owner and `run_tool` in place of `open_mr`
  - the README's *Tests* paragraph never named `open_mr` or the subprocess invariant, so a clause naming the pinned owner set (git wrappers, `shell`, `run_tool`, `api_get`) was added rather than a word swapped; every claim checked against `cmd_land`, `land_pending`, `land_trunk`, `landed`, `merge_gate`, `merge_mr`, `land_after_merge`, `pending_verdict`, the docstring exit table and the five SKILL.md files (the rule-5 WARNING is judged on the trunk's first-parent line; `--force` does not help when the commit is not in this clone)
- [x] README *Versions*: `0.2.0` row - `land` subcommand, `--merge` behind `merge = "self"`, exit 6, `pending` in `status`; `plugin.json` version `0.1.1` -> `0.2.0` and its `description` names the five helpers; `.claude-plugin/marketplace.json`'s forkflow `description` likewise
- [x] `git rm docs/backlog/no-post-merge-catch-up-step.md` in this task's commit (backlog lifecycle: the item is removed by the commit that lands its fix)
- [x] update CLAUDE.md if new patterns discovered (none expected - the repo has no CLAUDE.md) - none discovered; the repo has no CLAUDE.md and none was created
- [x] move this plan to `docs/plans/completed/` - performed by the exec harness after all phases finish, not by this task
  - ⚠️ the plan is still in `docs/plans/` while review phases run; the harness moves it at the end - no task or fixer moves it by hand

## Post-Completion
*Items requiring manual intervention or external systems - no checkboxes, informational only*

**Real-fork validation** (the fake proves argv and the chain, not the tools): on the GET fork, update
the plugin to 0.2.0 (`/plugin`), set `merge = "self"` in its `.forkflow.toml`, and run the next real
`/forkflow:ship --merge`. Expected: MR opened, merged by `glab mr merge ... --auto-merge=false`,
`land` fast-forwards `develop` and leaves you on it, the feature branch is gone locally and on
origin. If glab refuses the merge (pipeline required, permissions), exit 6 with the reason and
`forkflow land` after merging by hand - that path is the fallback, not a failure of the plan.

**Fork machines**: every clone of a fork that adopts `merge = "self"` needs the updated plugin;
`status` on 0.1.x ignores the key and `--merge` is an unknown flag there.

**Backlog**: `backup-branches-accumulate-without-pruning`, `hook-refuses-trunk-bootstrap-on-a-new-origin`
and `setup-multiple-remotes-hint-ignores-config` remain open and are not affected by this plan.

**Review decisions recorded** (plan review, 2026-09-10; all critical and important findings applied):
- invariant edits: two, both named - `run_tool` replaces `open_mr` in the `subprocess` owner set (a
  rename); `land_trunk` joins `advance_mirror` as a `merge --ff-only` owner in a new, separately named
  test. `git branch -f` stays blocked, which is why the trunk fast-forward is a merge, not a ref move
- the sync gate sits before the `--continue` dispatch; `--merge` is also refused on an unnameable
  origin, so no path pushes and then exits 6 for a reason known at gate time
- landing by patch equivalence (`git cherry` + `patch-id`) replaces tree equality, which fails the
  moment another commit lands first; a human squash merge of a ship is recognised the same way
  (⚠️ built as a single `git cherry` call, no `patch-id`; see *land* in Technical Details)
- `land --force` gained a meaning (fast-forward an unverifiable landing, keep the branch, clear the
  entry) instead of being parsed and ignored; it is also how a closed-MR entry is cleared
- `git checkout` not `switch` (README's git floor is 2.20); the switch is unconditional and `land`
  leaves HEAD on the trunk, stated in the skill, the README and the adjusted sync message
- `mr_ref` URL parsing dropped: both tools accept the source branch, which the run already has
- a merge that succeeded followed by a failed catch-up is exit 2 whose message opens with "the merge
  request was merged" - non-zero, as the user chose for every incomplete outcome, and honest
- "committed config" wording dropped everywhere: `load_config` reads the working tree, and `setup`
  leaves the file untracked; the gate is tested in both states
- rule 5 on GitLab is "the method the project is configured for", reported by `setup`, not
  guaranteed by it; `land` reports the shape of what landed
- Task 3's success-path tests are written to survive Task 4's chaining (no `pending` assertion after
  a successful `--merge`); the merging fake is defined once, precisely, in Task 3
