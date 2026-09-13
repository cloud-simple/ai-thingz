# forkflow: settings move to `git config`, and `.forkflow.toml` is retired

## Why

`.forkflow.toml` is a tracked file, and syncing exists to merge the original project's changes, so a
sync carries that project's version of the file by design. Two of its keys are dangerous in the
project's hands: `merge = "self"` disables human review of merge requests, and `gate` is a list of
shell commands run with `sh -c`.

Nine separate routes by which the original project could set `merge` in a fork's clone were found and
closed over nine review rounds on `forkflow-land-and-merge`. Closing them built an apparatus that
exists for one question only — whose copy of this file am I reading:

- content fingerprints and digests;
- a path-limited history walk over every non-`origin` remote-tracking ref and its tags;
- a persistent per-repository record, in the fork's own state file, of every config version the
  clone has ever seen the project hold;
- symlink and `.gitattributes` rendering checks, in three flavours: what is set now, what these
  bytes were already judged to be, and what the project's history ever set on the path;
- fail-closed refusals for shallow, partial, grafted and replaced clones, and for clones holding
  nothing of the project.

Settings in `git config` live in `.git/config`, which is not tracked and which no merge can reach.
The question cannot be asked, so none of the apparatus has a reason to exist.

## Decisions

**All keys move, and the file is retired.** Not only `merge`. The maintainer chose this over moving
only the dangerous key, with the trade-off below stated and accepted.

**⚠️ The accepted cost: settings stop travelling with the repository.** Branch names, remote names,
prefixes and `gate` become per clone. Every clone of the fork, on every machine and for every
teammate, configures them before the first command works. `setup` writes them, so the ordinary path
stays one command, but a teammate cloning the fork gets a tool that refuses until configured. A
fork's own `gate` no longer reaches its teammates at all.

**Key names.** Three keys cannot keep their names: a git config variable must be alphanumeric plus
`-` and start with a letter, so `upstream_branch` is not legal. The mapping is:

| `.forkflow.toml` | `git config` | kind |
|---|---|---|
| `upstream` | `forkflow.upstream` | single |
| `upstream_branch` | `forkflow.upstreamBranch` | single |
| `mirror` | `forkflow.mirror` | single |
| `trunk` | `forkflow.trunk` | single |
| `sync_prefix` | `forkflow.syncPrefix` | single |
| `backup_prefix` | `forkflow.backupPrefix` | single |
| `merge` | `forkflow.merge` | single, enum `manual` \| `self` |
| `gate` | `forkflow.gate` | **multi-valued**, `--get-all`, set with `--add` |

Git lower-cases names on lookup, so either spelling works when reading.

**⚠️ `merge` is read from `--local` only.** Every other key may come from any scope. `merge` must
not, because a single `git config --global forkflow.merge self` would silently arm unreviewed
merging in every fork on the machine. Read the rest in one `git config --get-regexp '^forkflow\.'`
call, and read `merge` separately and explicitly scoped.

**`gate` order is file order**, which `gate_commands` relies on since it stops at the first failure.
Documented, and the docs must say `--add`, never plain `git config forkflow.gate`, which replaces.

**Type validation collapses and that is fine.** git config values are always strings, so "wrong
type" ceases to exist. What survives is `valid_branch_name` and the `merge` enum. An empty value
falls through to the default, as today.

## Migration

`.forkflow.toml` shipped in 0.1.x for the layout keys, so real forks have one. In `resolve_ctx`,
where `load_config` is called today, print a notice and carry on with git config. Never exit
non-zero for it: a fork on Python 3.9 with a leftover file must not be bricked by the upgrade that
was meant to simplify it.

**⚠️ The notice must never print a command that sets `merge`.** The file may be the original
project's bytes, and a pasted `git config forkflow.merge self` would reintroduce the whole attack
through the user's own hands. This repository has already been burned twice by remedies that
produced config bytes. For `merge`, report the value found as information, with no runnable command
and a sentence saying the fork must decide it deliberately. Every other key is safe to emit as a
command: a wrong branch name fails `valid_branch_name`, and a `gate` the user pastes is one they
have read.

Rules for the notice: read the file for display and nothing else, pinned by a source invariant; cap
the read size; take only the known keys and ignore tables and unknowns; quote every value that
reaches a printed command through `sh_arg`; check `os.path.islink` before any open and never follow
a link; treat a case variant as a file to name, not to read, and print no rename remedy; and carry
on with a plain note when the file cannot be read at all.

Keep the notice for 0.3.x. Delete it, and `CONFIG_FILE` with it, at 0.4.0.

## What is removed

~1,700 lines of production code: the provenance cluster (`config_fingerprint` through
`fork_merge_refusal`), the config reader and its TOML parsing, the case-variant machinery
(`variant_remedy`, `keep_aside`, `no_commit_here` and friends), the sync-brought-a-gate guards
(`gate_arrived_in_merge`, `config_changed_in_merge`, `gate_at` and their plumbing), and setup's
template writer. ~2,850 lines of tests go with them.

**The Python 3.11 floor goes too.** `tomllib` was imported for the config reader alone, so the
script's requirement drops to Python 3.9, unconditionally — except inside the migration notice,
which needs a parser and simply says so when it has none.

**Two source invariants are deleted and replaced.** The one pinning provenance to a single reader,
and the one pinning `keep_aside` as the sole owner of a printed `cp`, both lose their subjects. The
replacement pins that the migration parser's result reaches nothing but the printer.

## What is NOT removed

`repo_id` and the remote-identity helpers, which back the rule-1 origin-alias refusal and the
generated pre-push hook. `untracked_in_the_way` and `same_file`, which answer "which untracked files
would this merge overwrite" for any file. `upstream_tracked` and its `ls-tree`. The whole `land` and
`pending` surface, which never touched the config. Every invariant pinning the push helpers,
`update-ref`, `merge --ff-only`, `rebase`, `--force-with-lease` and `subprocess`.

## Implementation Steps

### Task 1: git config reader beside the file
- [x] add the reader for the eight keys, one `--get-regexp` call plus a scoped read for `merge`
- [x] `resolve_ctx` prefers the file where both exist, so nothing changes yet
- [x] tests for multi-valued `gate`, missing keys, illegal branch names, and `merge` scope
- [x] run the suite - must pass before task 2

**⚠️ Deviation, task 1: `fork_merge_mode` consults `git_config_merge` first, here and not in
task 5.** As written, the plan had nothing read `merge` from git config until task 5, while
task 2 converts `MergeBase.self_fork()` - the fixture that declares `merge = "self"` - to
`git config`. Every `--merge` test would have gone red at task 2 and stayed red through
tasks 3 and 4, so tasks 2-4 could not be committed green. The wiring is two lines at the
top of `fork_merge_mode`: a value found in `.git/config` answers, and the file is still read
where git config is silent. It is safe to bring forward and worse to defer - it is read from
`--local` only, so nothing a merge can write reaches it, and leaving `merge` on the tracked
file through tasks 3 and 4 would have left the most dangerous key on the file after the
layout keys had left it. It changes nothing for a clone that sets no `forkflow.merge`, which
is every clone before task 7. The provenance apparatus still answers wherever git config is
silent, so its own tests keep exercising it until task 5 deletes it.

### Task 2: convert the test fixtures
- [x] `make_fork(config=...)` and `MergeBase.self_fork()` set git config instead of writing the file
- [x] the setup fixture's config helper follows
- [x] run the suite - must pass before task 3

**⚠️ Deviation, task 2: `make_fork` grew a `settings=` parameter beside `config=` rather than
changing what `config=` means.** `settings=` is a dict of the file's own key names and goes
into `git config`; `config=` still writes the tracked file and now belongs only to the tests
whose SUBJECT is that file - `TestLoadConfig`, `TestForkMergeMode`, `TestMergeGate`,
`TestConfigNameCase`, `TestCheck.test_gate_of_a_wrong_type_is_exit_2`,
`TestMergeModeAskedAgainBeforeTheMerge` (which overrides `self_fork` to keep it: its subject
is a teammate's commit turning the file from "self" to "manual", which `.git/config` cannot
express) and `TestPendingPerBranch.test_a_state_file_that_cannot_be_read_...` (the provenance
memory is of file versions). Tasks 4-6 delete those, and `config=` goes with the last of them.
24 fixtures moved to `settings=`.

**⚠️ Deviation, task 2: one production line changed.** `finish_sync` rebuilt `ctx.cfg` from
`load_config` alone after the merge it had just made, so a `gate` in git config vanished for
the rest of a sync. The precedence now lives in one helper, `load_settings`, which
`resolve_ctx` and `finish_sync` both call - a second spelling in either is exactly how one of
them came to read half the settings.

**⚠️ Note, task 2: the setup fixture had nothing to convert.** `SetupBase.cfg()` already reads
`git config`, and `SetupBase.toml_path()`/`untouched()` assert on the template `setup` still
writes. Task 7 replaces the template; the helper goes with it.

### Task 3: git config wins, the file is ignored
- [x] `resolve_ctx` stops calling `load_config`; drop the `needs_tomllib` decorators
- [x] delete `TestLoadConfig`
- [x] run the suite - must pass before task 4

**⚠️ Deviation, task 3: the suite drops 28 tests, not 12, and `setup` gained a git config
write.** The file stopping being read for configuration takes every test whose subject was
that reading with it, and four of them could not wait for the task that deletes their
production code:

- `TestLoadConfig` (12) and `TestCheck.test_gate_of_a_wrong_type_is_exit_2` (1): the file's
  parsing and its type checking. A git config value is always a string.
- `TestSyncGateOnACleanMerge` (5, the whole class) and three `TestSyncConflicts` gate tests:
  **task 4's tests, deleted here.** The guards ask whether the merge this run just made
  changed the `gate` in `.forkflow.toml`; the `gate` now comes from `.git/config`, which a
  merge cannot reach, so there is no longer a state in which they can fire. The production
  code (`config_changed_in_merge`, `gate_arrived_in_merge`, `gate_at`) is untouched and is
  still task 4's to delete - **task 4 loses its test-deletion bullet.**
- seven `TestConfigNameCase` tests: **task 6's.** Their subject is a case variant of the name
  making every subcommand refuse, which was `load_config`'s refusal reached through
  `resolve_ctx`. The variant machinery still guards `merge` (`fork_merge_mode` reads the
  untracked file through `load_config`) and still answers "what is in the way of this merge",
  and the tests for both still pass - **task 6 loses its test-deletion bullet** for the seven.

**⚠️ `setup` now writes `--trunk`/`--mirror`/`--upstream` into git config**
(`write_git_config_keys`), beside the `.forkflow.toml` it still writes. Without it, task 3
leaves `forkflow setup --upstream <name>` persisting the name nowhere anything reads, and a
clone with two non-origin remotes is exit 2 on every later command - a real regression across
four commits. Task 7 deletes the file half; the git config half is already there.

Five `TestForkMergeMode` fixtures had their LAYOUT keys moved into git config (their `merge`
stays in the file, which is their subject): they configure a trunk, mirror or upstream name
in order to aim the provenance walk, and that has to reach the Ctx to aim anything.

### Task 4: delete the sync gate guards
- [ ] `config_changed_in_merge`, `gate_arrived_in_merge`, `gate_at` and their plumbing in
      `finish_sync` and `run_check`; `run_check` loses its `config_merged` parameter
- [ ] delete the tests that exist only for them
- [ ] run the suite - must pass before task 5

### Task 5: delete the provenance apparatus
- [ ] `fork_merge_mode` becomes a scoped `git config` read; `fork_merge_refusal` becomes one sentence
- [ ] delete the cluster and its constants
- [ ] delete the two source invariants that lose their subjects
- [ ] run the suite - must pass before task 6

### Task 6: delete the case-variant machinery
- [ ] `variant_remedy`, `keep_aside`, `no_commit_here`, `tracked_config_names` and friends
- [ ] shrink `untracked_in_the_way` and `in_the_way_advice` to their file-agnostic parts
- [ ] run the suite - must pass before task 7

### Task 7: setup writes git config
- [ ] delete the template writer and the in-place key rewriter; fold the keys into `setup_git_config`
- [ ] setup prints the `gate` and `merge` lines a fork typically wants, as suggestions
- [ ] run the suite - must pass before task 8

### Task 8: the migration notice
- [ ] the notice in `resolve_ctx`, under every rule above
- [ ] the source invariant that its parsed result configures nothing
- [ ] tests: unreadable, case variant, symlink, oversized, and `merge` reported without a command
- [ ] run the suite - must pass before task 9

### Task 9: docs, manifests and the state file's wording
- [ ] README Configuration section, exit-code table, adoption recipe, changelog row
- [ ] `references/rules.md`, the five SKILL.md files, the module docstring, the `--merge` help
- [ ] reword `change_state`'s refusal and `load_state`'s docstring, which name the deleted memory
- [ ] bump the plugin to 0.3.0
- [ ] run the suite - must pass before task 10

### Task 10: verify
- [ ] the full suite and the other plugin's 24 tests
- [ ] an ordinary fork still completes setup, ship, land, sync and land on both platforms
- [ ] no invariant pinning a dangerous operation was weakened

## Post-Completion

Real-fork validation on a live fork, which was already outstanding before this change. The GET fork
has a `.forkflow.toml` today and is the natural first exercise of the migration notice.
