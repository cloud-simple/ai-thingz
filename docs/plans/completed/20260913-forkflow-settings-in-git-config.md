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
- [x] `config_changed_in_merge`, `gate_arrived_in_merge`, `gate_at` and their plumbing in
      `finish_sync` and `run_check`; `run_check` loses its `config_merged` parameter
- [x] delete the tests that exist only for them — already done in task 3, see its note
- [x] run the suite - must pass before task 5

**⚠️ Deviation, task 4: `finish_sync` loses its `merge_sha` parameter too, and the
`cfg` refresh with it.** `merge_sha` reached `finish_sync` for one purpose — feeding
`config_changed_in_merge` and `gate_arrived_in_merge` — so with those gone it is an
argument two call sites compute and nothing reads. `cmd_sync_continue` still calls
`sync_merge_commit` for its own `ours`/`theirs`; `cmd_sync` no longer computes a
`merge_sha` at all. The `replace(ctx, cfg=load_settings(...))` refresh after the merge
went with them: `.git/config` is not in any tree, so the settings `check` obeys cannot
have changed under the merge this run just made, and its exit-2 "the merge brought a
config that cannot be read" path can no longer be reached. `run_check`'s "none
configured" line now names `git config forkflow.gate`, and the WARNING about upstream
tracking `.forkflow.toml` went with the rest — the file no longer says what `gate` is.

### Task 5: delete the provenance apparatus
- [x] `fork_merge_mode` becomes a scoped `git config` read; `fork_merge_refusal` becomes one sentence
- [x] delete the cluster and its constants
- [x] delete the two source invariants that lose their subjects
- [x] run the suite - must pass before task 6

**⚠️ Deviation, task 5: three names went beyond the list, and one that should have died
did not.**

- `merge_mode_in` and `fork_merge_source` are deleted too. Neither is on the list, and
  both had exactly one caller — the collapsing `fork_merge_mode` and the `--merge`
  refusals — so both were dead the moment those were rewritten. `merge_mode_in` was the
  last `parse_config` caller outside `load_config`; the source invariant that pinned
  `get("merge")` to three owners now pins it to `parse_config` alone.
- `finish_sync`'s `merge_sha` parameter went in task 4 (see its note).
- **`state_unreadable` is left unreferenced on purpose.** Its only production caller was
  `config_memory_unprovable`. It is the read side of `change_state`'s refusal, it has its
  own tests, and task 9 already owns the wording of that refusal and of `load_state`'s
  docstring — both of which still name the deleted memory. Deleting it belongs with that
  rewording, not here. The source-invariant line that pinned its owners is gone, since the
  one owner it named is gone.

**⚠️ Deviation, task 5: the two deleted source invariants, and what went with them.**
`test_whose_config_it_is_is_decided_by_its_bytes_in_one_place` and
`test_every_copy_a_remedy_prints_is_built_here` are both deleted, as planned. The second
is deleted ONE TASK EARLY relative to its subject: `keep_aside` and `variant_remedy` still
exist until task 6, so a printed `cp` is momentarily unpinned. **It is worth resurrecting
verbatim if any remedy ever prints a `cp` again** — it is the only thing that stopped a
remedy from producing config bytes, twice. The merge invariant
(`test_every_merge_decision_goes_through_fork_merge_mode`) is untouched apart from the
`get("merge")` owner set: the scoped-read pins tasks 1-3 added all stand.
`test_the_state_file_is_written_in_one_place_and_under_its_lock` lost the two writers that
no longer exist and the `state_unreadable` owner line; everything else in it stands.

**⚠️ Deviation, task 5: `variant_remedy` and `in_the_way_advice` lost their provenance
sentences here rather than in task 6.** Both called into the deleted cluster (the rename
remedy promised a sync would still treat a renamed file as upstream's, with its `gate`
shown rather than run and `--merge` refused; the in-the-way advice asked
`config_is_upstreams` whose bytes the untracked file was). Neither claim is true any more
and neither callee exists, so the sentences had to go with the callees. What is left of
both is task 6's to delete.

**⚠️ Task 5 tests: the suite drops from 566 to 504.** `TestForkMergeMode` (60) and
`TestMergeModeAskedAgainBeforeTheMerge` (3, the whole class — its subject is a teammate's
commit flipping the TRACKED file between the gate and the merge, which `.git/config`
cannot express) are deleted outright. `TestMergeGate` keeps the gate that survives — the
two conditions, both `--continue` routes, the unnamed origin, the request-number branch,
the end-to-end pass — and loses the seven fail-closed clone-shape refusals, the
untracked-file reads and the whose-bytes wording (13). `TestState` loses six memory tests
and keeps the seventh, rewritten as
`test_a_state_file_that_cannot_be_read_is_not_written_over` (the half of it that is about
`change_state`, not about the memory). Two tests are ADDED:
`TestGitConfigSettings.test_a_config_file_that_says_merge_self_does_not_arm_merge` pins the
closed route at the reader, and `TestMergeGate`'s two upstream-`merge = "self"`-arrives-
with-the-sync tests are kept and re-pointed at it end to end — those three together are
the whole security claim of this change.

### Task 6: delete the case-variant machinery
- [x] `variant_remedy`, `keep_aside`, `no_commit_here`, `tracked_config_names` and friends
- [x] shrink `untracked_in_the_way` and `in_the_way_advice` to their file-agnostic parts
- [x] run the suite - must pass before task 7

**⚠️ The `cp` window is closed.** The invariant that pinned `keep_aside` as the sole owner
of a printed `cp` was deleted in task 5, one task ahead of its subject. With `keep_aside`
and `variant_remedy` gone, NO string and no comment in the production half of the file
holds `cp ` at all - checked by reading every string constant and f-string below
`run_tests` out of the AST, and by a raw count over the same lines: both zero. No remedy
prints a copy, so there is nothing left to pin, and the deleted invariant stays worth
resurrecting verbatim the day one does again.

**⚠️ Deviation, task 6: `config_text` and `config_name_at` went too.** Neither is on the
list. Both answer "what is `.forkflow.toml` in this tree, under any case of its name" -
`config_name_at` is `config_name_in` over an `ls-tree` - and both lost their last
production caller in task 4 with the sync gate guards. They were reachable only from
`TestConfigNameCase`, which this task deletes, so leaving them would have left two
functions nothing calls and no test covers.

**⚠️ Deviation, task 6: `load_config` was touched, which task 6 was told not to do.** Its
case-variant refusal was the last caller of both `config_name_in` and `variant_remedy`,
so the two could not go while it stood. It now opens the exact name and answers `{}` when
there is no such file; the `os.listdir` sweep, the exit-2 refusal and the remedy are gone,
and the docstring says why no setting rides on which spelling the filesystem opens any
more. `parse_config`, `CONFIG_FILE` and `have_tomllib` are untouched, and task 8's notice
has its parser. Task 8 will want the "a case variant is a file to name, not to read" rule
in the notice itself, where the plan already puts it.

**⚠️ Deviation, task 6: `setup_template` lost its `tracked_config_names` guard here, not
in task 7.** The "commit it on a branch and ship it" line was printed only when no config
was tracked yet; that call was the helper's last one. The line is now unconditional for
the one commit between this task and task 7, which deletes the whole function.

**⚠️ Task 6 tests: the suite drops from 504 to 489.** `TestConfigNameCase` (14, the whole
class) goes: seven of its tests were already deleted in task 3, and what was left was the
remedies, `no_commit_here`'s branch rules, and the two ignored/variant-config
in-the-way cases. `TestSyncConflicts.test_the_untracked_merge_fallback_for_the_config_keeps_it`
(1) goes with the config branch of `in_the_way_advice` that was its subject.
`test_an_untracked_file_upstream_tracks_is_refused_before_the_backup` is KEPT and
re-pointed at an ordinary `docs/theirs.md`: its subject is the preflight that refuses
before the backup is pushed, which survives whole, and with the config branch gone the
advice it now prints is the generic one - so the test also became independent of the
template `setup` stops writing in task 7. Five test helpers that lost their last caller
went with the class: `remedy_of`, `no_remedy`, `without_stamp`, `kept_configs`,
`run_every_printed` (the last three were already unreferenced after task 5) and the
`SHELL_VERBS` tuple.

**The two survivors are intact.** `untracked_in_the_way` keeps its case-fold collision
check - a case-insensitive filesystem collides on any name, and the check is what stops a
sync from refusing after the backup instead of before it - and lost only the block that
listed the top of the tree to catch a config hidden by `.git/info/exclude`.
`in_the_way_advice` keeps its "move them out of the working tree, or get them into the
trunk" tail. Both are covered for ordinary files by
`test_an_untracked_file_upstream_tracks_is_refused_before_the_backup` and
`test_the_untracked_merge_fallback_names_a_rerun_that_is_not_a_closed_loop`.

### Task 7: setup writes git config
- [x] delete the template writer and the in-place key rewriter; fold the keys into `setup_git_config`
- [x] setup prints the `gate` and `merge` lines a fork typically wants, as suggestions
- [x] run the suite - must pass before task 8

Deleted: `setup_template`, `template_text`, `config_with_keys` (the in-place key rewriter
that kept the trailing comments), `toml_string`, `CONFIG_KEY_LINE` and `write_config_keys`.
`write_git_config_keys` is gone as a separate function and its work is a `pairs` argument
to `setup_git_config`, which now writes the `--trunk`/`--mirror`/`--upstream` names and the
four rule settings in ONE loop - one spelling of "already set", of the dry run and of the
printed command for all of them. `cmd_setup` reads the flags where it read them before and
passes them down; nothing else in the run changes.

**⚠️ `setup` says the per-clone cost out loud, every run.** The last thing `setup` prints is
`setup_suggestions`: the `git config --add forkflow.gate '<command>'` line (with `--add`
appends, plain `git config` replaces, and they run in order and stop at the first failure),
the `git config --local forkflow.merge self` line (with the sentence that it merges merge
requests without review and is a deliberate choice, never a default), and then that none of
it travels with the repository - `.git/config` is not tracked, no merge can write it, and
another clone, another machine or a teammate has none of it until it is set there too.
Suggestions and not actions: `setup` runs neither of them, and the idempotency test asserts
that the run arms no `forkflow.merge` and writes no `forkflow.gate` of its own.

**⚠️ Deviation, task 7: the timing of the flag write moved.** `--upstream`/`--trunk`/
`--mirror` used to be persisted immediately after the `names` step; they are now written
where `setup_git_config` runs, after the mirror, trunk, push URL and hook steps. A `setup`
that fails at one of those no longer leaves the name behind - the rerun needs the flag
again, as it did before task 3 added the git config writer. One writer for all of git
config is worth that: two loops setting a `forkflow.*` key is exactly how one of them comes
to disagree with the other about the dry run.

**⚠️ Deviation, task 7: `gate_commands`'s docstring was corrected.** It said `load_config`
had already refused every other shape, which stopped being true when the gate moved to git
config, where every value is a string. It now says the order is the whole of what a gate
means and why a blank entry is dropped. Prose only.

**⚠️ Task 7 tests: the suite drops from 489 to 486.** The three `config_with_keys` tests go
with the function. `SetupBase.toml_path` and `untouched`'s assertion that no config file was
written go with the template. `test_configures_the_clone_and_is_idempotent` swaps its four
template assertions for the suggestion lines, the two `forkflow.*` settings left unset and
the absent file. `test_no_template_is_written_on_a_python_that_cannot_read_one` becomes
`test_a_python_without_tomllib_configures_the_clone_and_runs_every_command`: the subject
that survives is the Python floor - `setup` configures the clone and every command after it
works with no `tomllib` at all, and nothing says "Python 3.11" any more.
`test_the_upstream_flag_is_used_and_written_to_the_config` reads `forkflow.upstream` back
out of git config, and `test_trunk_and_mirror_flags_are_written_and_other_keys_survive`
becomes `..._and_other_settings_survive`: a two-command `gate` already in `.git/config` is
still there in its order after `setup`, and a second run reports the names as already set.

`load_config`, `parse_config`, `configures_nothing`, `have_tomllib` and `CONFIG_FILE` are
now unreferenced by anything but each other, which is what task 8 wants: the migration
notice needs a parser. `load_config` no longer refuses a case variant (task 6), so the
notice will have to treat one as a file to name itself, as the Migration section says.

### Task 8: the migration notice
- [x] the notice in `resolve_ctx`, under every rule above
- [x] the source invariant that its parsed result configures nothing
- [x] tests: unreadable, case variant, symlink, oversized, and `merge` reported without a command
- [x] run the suite - must pass before task 9

`migration_notice` is called in `resolve_ctx` where `load_config` was, right after the root
is known. It remembers the roots it has already told (`MIGRATION_SAID`) so `setup`'s two
`resolve_ctx` calls say it once, and it prints under `--dry-run` because it writes nothing.
`migration_report` is the one reader of the file left: it takes the name from the directory
listing, asks `os.path.islink` before anything opens the path, insists on a regular file,
caps the read at `MIGRATION_MAX` (64 KiB), takes only the eight known keys, and turns every
failure - an OS error, bytes that are not UTF-8, TOML that does not parse, a Python with no
`tomllib` - into a sentence with the reason and "open it yourself". Nothing in it raises.

**⚠️ Deviation, task 8: `load_config` is deleted.** The notice needs a reader that is
careful about a file it does not trust - the link check before the open, the regular-file
check, the cap - and `load_config` did none of that. Keeping it would have left a second,
less careful reader of the same file with no caller, which is exactly the shape that took
nine review rounds to close the first time. `parse_config`, `configures_nothing`,
`have_tomllib` and `CONFIG_FILE` stay, as the plan says, until 0.4.0.

**⚠️ Deviation, task 8: `parse_config` lost its `where` parameter.** With one caller there
is one file it can be parsing, and the caller's sentence already names it: the refusals
became phrases ("it is not valid TOML (...)", "`gate` must be a list of shell commands") so
they read as the reason inside that sentence. Its no-`tomllib` message was rewritten for the
same reason - it used to tell the user to turn every line into a comment to unbrick the
tool, which was true when the file was read for configuration and is now nonsense.

**⚠️ The merge-decision invariant is untouched, and the new one carries the weight for the
notice.** `owners('get("merge")')` still answers `{"parse_config"}`: the notice reads the
key through `MIGRATION_KEYS`, the plan's own mapping table, whose `merge` row carries NO
`git config` variable - deliberately, and not by omission. What pins rule 2 is stronger than
a spelling: `test_the_migration_parser_configures_nothing` reads every string constant in
`migration_report` out of the AST and asserts that none of them names `forkflow.merge` and
that no line built for pasting mentions `merge` at all, and
`test_merge_is_reported_and_nothing_printed_would_set_it` asserts the same over the rendered
output of a file that holds every key. A command that sets `merge` has to name the variable,
so a notice that never names it cannot print one.

**⚠️ `MIGRATION_SAID` is cleared at the top of `main`.** Once per RUN, and a run is one
`main` call: the embedded tests call `main` many times in one process, and without the clear
the notice would have been once per process - the second `forkflow status` of a test saying
nothing. In production it is a no-op.

**⚠️ Task 8 tests: the suite goes from 486 to 500.** `TestMigrationNotice` (13) covers every
key in order, `merge` reported with nothing to paste, a gate entry holding a quote or a
newline coming back out of `shlex.split` as one argument equal to what the file said, the
symlink named and its target's bytes absent, the case variant named and not read with no
line of any kind to paste, the oversized file, the not-a-regular-file, the file that says
nothing, four unreadable files that all still leave `status` and `check` at exit 0, a Python
with no `tomllib` running every subcommand, the notice said exactly once across `setup`'s
two `resolve_ctx` calls and again in the next run, and - the point of the whole change - the
settings actually in force after the notice coming from `git config` while the file on disk
names different ones. `TestSourceInvariants` gains the one invariant (1).

### Task 9: docs, manifests and the state file's wording
- [x] README Configuration section, exit-code table, adoption recipe, changelog row
- [x] `references/rules.md`, the five SKILL.md files, the module docstring, the `--merge` help
- [x] reword `change_state`'s refusal and `load_state`'s docstring, which name the deleted memory
- [x] bump the plugin to 0.3.0
- [x] run the suite - must pass before task 10

The README's Configuration section is rewritten around a `git config` block. The essay on how
`merge` is judged to be the fork's own is four lines, because there is nothing left to judge;
the sync-brings-a-gate, untracked-config and case-variant passages are gone whole; and two facts
are stated as headings of their own - that the settings do NOT travel with the repository, and
that `forkflow.merge` is read from `--local` scope alone so one `--global` cannot arm unreviewed
merging in every fork on the machine. `gate` says `--add` appends, plain `git config` replaces,
and that the commands run in order and stop at the first failure. A paragraph documents the
migration notice: reported, never read for configuration, the commands printed for every other
key, and for `merge` the value reported with deliberately nothing to paste.

**⚠️ Deviation, task 9: the exit-2 row lost the unreadable config and GAINED a replacement.**
Dropping `unreadable .forkflow.toml` outright would have left the row silent about the
precondition that took its place: a `forkflow.*` setting that is not a legal branch name is
exit 2 for every subcommand (`valid_branch_name`, the one check that survived the file parser).
The row names that instead, and its `--merge` clause now names `git config --local
forkflow.merge` rather than the file key.

**⚠️ Deviation, task 9: `change_state`'s refusal may not say `forkflow land`.**
`test_every_printed_sync_and_ship_command_comes_from_rerun_cmd` pins every spelled `forkflow sync|ship|land`
to `land_cmd`, and the first rewording of the refusal - "what `forkflow land` would have landed" -
failed it. What the user loses is named without the command: "every branch's pending ship or sync
and with it every landing they have waiting". The docstrings of `load_state` and `change_state` are
prose, which the invariant skips, so they name the pending map and `forkflow land` freely.

**⚠️ Task 9: `.claude-plugin/marketplace.json` needed nothing.** It carries no `version` field -
the version lives in `plugins/forkflow/.claude-plugin/plugin.json` alone, which is bumped to
`0.3.0` - and its forkflow description names no config file and no Python floor. Left untouched.

**⚠️ Deviation, task 9: `docs/backlog/setup-multiple-remotes-hint-ignores-config.md` was rewritten,
which is outside the task's list of documents.** Its remedy was "commit `upstream = \"upstream\"`
in `.forkflow.toml`", which is now impossible, and its premise was the commented-out template that
no longer exists. The item itself is still real - the "several non-origin remotes" hint names only
the `setup` flags and never the setting that ends the refusal - so it is kept, re-pointed at
`git config forkflow.upstream` and at the line number the hint sits on now. Leaving it would have
left the only backlog item in the repository telling a reader to do something the tool no longer
supports.

The five SKILL.md files: `status` drops the 3.11 note from its script line; `land`, `ship` and
`sync` swap their `.forkflow.toml` bullet for one that names the `git config` settings and says
they do not travel with the repository; `ship` and `sync` collapse the whose-bytes-are-these
paragraph in their `--merge` step to the scoped read; `sync` loses the config `CHECK` paragraph in
its conflict step (a merge cannot reach `.git/config`, so no sync brings a gate) and the config
half of its exit-2 untracked-file row, keeping the generic refusal; `setup` replaces its
`.forkflow.toml` step with the two settings it prints and will not set, and says the per-clone cost
out loud. `references/rules.md` reworks the gate section: still arbitrary shell, still never run on
a dry run, multi-valued with `--add`, run in order to the first failure - and the sync-brings-a-gate
premise is replaced by why it cannot.

### Task 10: verify
- [x] the full suite and the other plugin's 24 tests
- [x] an ordinary fork still completes setup, ship, land, sync and land on both platforms
- [x] no invariant pinning a dangerous operation was weakened

**Suites.** `forkflow.py --test`: 500 tests, OK. `python3 -m unittest discover -s tests`: 24
tests, OK.

**End to end, both platforms** (scratchpad `g5/E1.sh`, on the `e3` fake `gh`/`glab`, with the
fork's setup converted from the config file to `git config`). setup, `ship --merge`, land,
upstream moves, `sync --merge`, land - exit 0 throughout on GitLab and on GitHub. The mirror
ends byte-equal to `upstream/main` on both. The trunk moves only through `git merge --ff-only`
(GitLab `ac3a8e6 -> 061f370 -> 4bf9110`, GitHub `0dbfb05 -> 589a5c1 -> 2e3038a`), and the
first-parent line is one squashed ship followed by one sync merge commit. `--merge` really
merged: `glab mr merge ... --sha <head> --auto-merge=false`, `gh pr merge ... --rebase` for the
ship and `--merge` for the sync, each with the head-commit guard. Because `--merge` chains the
landing itself, the `land` that follows it correctly reports nothing pending (exit 2) - the run
had already left the user on the trunk. Two refusals were checked in the same fixture before the
setting was made: `--merge` with no `forkflow.merge` at all is exit 2, and `--merge` with
`git config --global forkflow.merge self` (against a throwaway `GIT_CONFIG_GLOBAL`, never the
user's own) is exit 2 as well.

**A fork with a leftover `.forkflow.toml`** (scratchpad `g5/E2.sh`): `git config forkflow.trunk
maintrunk` against a file saying `trunk = "develop"`, `mirror = "not-a-branch-of-ours"`,
`backup_prefix`, a two-command `gate` starting `exit 2`, and `merge = "self"`. `status` and
`check` both exit 0 and both report the trunk as `maintrunk` - the git config value, and a name
that is neither the default nor the file's. `check` says `gate - none configured`: the file's
`exit 2` never ran. `ship --merge` is exit 2 - the file's `merge = "self"` arms nothing. The
notice is printed once per run, in full, with a `git config` line for every key but `merge` and
the value of `merge` reported with nothing to paste.

**Invariant audit.** Every one of them still stands, unweakened: `"push"` to the three push
helpers; the argv-matcher blind-spot test (single-quoted argv, `'push'`, `-f`, `-u`);
`--force`/`--no-verify`/`--force-with-lease`; `update-ref` to `advance_mirror`; `merge --ff-only`
to `advance_mirror` and `land_trunk`; `rebase` to `rebase_onto`; the subprocess owner set; the
dry-run rule (`merge-tree`, `["fetch"]`, `fetch_preview`); the printed-command helpers
(`rerun_cmd` for every `forkflow sync`/`ship`, `land_cmd` for every `forkflow land`); and the git
floor. The merge decision keeps all four of its scoped-read pins - `get("merge")` to
`parse_config`, `GIT_CONFIG_MERGE` to the module and `git_config_merge`, `git_config_merge(` to
itself and `fork_merge_mode`, and `"--local"` to `git_config_merge` alone - plus the refusals in
`merge_gate` and `merge_mr` and the ordering of both. The new
`test_the_migration_parser_configures_nothing` is in place.

**⚠️ Mutation test: both mutants died, on the tests that are supposed to kill them.**
(a) The merge reader made unscoped (`git_rc("config", "--get", GIT_CONFIG_MERGE, ...)`): 2
failures - the BEHAVIOURAL
`TestGitConfigSettings.test_merge_is_read_from_the_local_scope_and_the_sweep_never_carries_it`
and the source invariant. (b) The migration parser's result reaching configuration
(`load_settings` filling unset keys from the file through `parse_config`): 4 - the invariant
`test_the_migration_parser_configures_nothing`, `test_git_config_is_read_and_the_file_is_not`,
and two `TestMigrationNotice` tests. Neither central guarantee is untested.

**⚠️ Deviation, task 10: one fixing commit, for a stale string the end-to-end run surfaced.**
The `--merge` refusals still opened with ``--merge needs `merge = "self"` in this fork's own
config`` - TOML syntax for a setting that is no longer in a TOML file. Task 9's brief named the
two `--merge` help strings and not these, but they are the same stale spelling and they are what
a user actually reads when the gate refuses. `merge_gate` now opens "--merge needs this fork's
own config to say `self` - " and hands over to `fork_merge_refusal` as before; `merge_mr`'s
exit-6 message and its `NOT RUN` step line both name `git config --local forkflow.merge`
directly. The constant `GIT_CONFIG_MERGE` is deliberately NOT used in either: the merge-decision
invariant pins that name to the module and `git_config_merge`, and spelling it in a message
would have widened that set. `REFUSED = "--merge needs"`, which the tests match on, is unchanged.

## Post-Completion

Real-fork validation on a live fork, which was already outstanding before this change. The GET fork
has a `.forkflow.toml` today and is the natural first exercise of the migration notice.
