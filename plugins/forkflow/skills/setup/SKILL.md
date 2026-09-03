---
name: setup
description: "Make the fork workflow's rules mechanical in a clone: disable the upstream push URL, install the pre-push hook that refuses pushes to upstream, to the trunk and of a non-pristine mirror, set ff-only merge config, bootstrap the trunk on a fresh fork, and report the hosting platform's default branch, merge method and branch protection with the exact command to fix each mismatch. Use when the user says \"forkflow setup\", \"set up the fork\", \"protect the trunk locally\", \"install the forkflow hook\", or after a status shows the hook missing or the upstream push URL live."
allowed-tools: Bash, Read, AskUserQuestion
---

# forkflow setup

The script never pushes the trunk (the one exception is bootstrapping it on a fresh fork, where
the trunk equals upstream and carries nothing of ours), never rebases it, and never commits on the
mirror - and neither may you. `setup` changes **this clone only**: it reports the server-side
settings and prints the commands, and never applies them.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py`.
Read `${CLAUDE_PLUGIN_ROOT}/references/rules.md` before running it.

## Process

1. **Find the upstream remote.** `git remote -v`. If there is no remote for the original project,
   ask the user for its URL (AskUserQuestion or plainly) and pass it - the script will not invent
   one:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" setup --upstream-url <URL>
   ```

   An existing remote under a non-standard name is used as it is; the script prints a
   `git remote rename <name> upstream` suggestion, which is optional. `--upstream NAME` picks one
   when several non-origin remotes exist. `--upstream-url` is ignored (with a note) when the
   remote already exists.

2. **Dry run when the user wants to see it first.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" setup --dry-run
   ```

   Everything is printed as `would:`; no config, no hook, no template, no branch, no push. (The
   fetch still runs, and the platform report still runs - every one of its calls is a GET.) A dry
   run that would have to *add* the remote stops there: there is nothing to preview yet.

3. **Run it.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" setup
   ```

   In order: resolve and fetch both remotes and set their HEADs; write `--trunk` / `--mirror` into
   `.forkflow.toml` if given; check the mirror is a pure copy of upstream (creating it locally in a
   single-branch clone); bootstrap the trunk if it exists nowhere; disable the upstream push URL;
   install the pre-push hook; set the ff-only git config; report the platform; write the
   `.forkflow.toml` template if absent. The trunk step runs **before** the hook, because the hook
   refuses every push of the trunk, creation included.

4. **Walk through the platform report.** It is read-only and each finding that needs action is
   followed by an exact `fix:` command. Show the user the findings and the commands; a Maintainer
   runs them (or does it in the web UI). Do not run them yourself unless the user asks.

   | expectation | why |
   |---|---|
   | default branch = the trunk | merge requests open against it and a fresh clone starts there |
   | GitLab `merge_method=ff` | ship MRs fast-forward; a sync MR still goes in whole, its tip being the merge commit |
   | GitHub `allow_merge_commit=true` | a sync MR must land as a merge commit - squashing it rewrites upstream's SHAs out of the trunk |
   | GitHub `allow_rebase_merge=true` | a ship MR is merged with "Rebase and merge" |
   | trunk protected, force-push disallowed | the whole point: no direct push, no rewrite of a published branch |
   | mirror protection | advisory only - the pre-push hook already keeps it a pure copy |

   "not checked (insufficient rights)" means the token cannot read protection (HTTP 403) - say so
   and let a Maintainer check; "not checked (`glab`/`gh` unavailable)" means the tool is missing or
   unauthenticated, which is not a failure of the setup.

5. **Explain the hook.** It is `pre-push` in this clone's hooks directory and it refuses:
   any push to the upstream remote (matched by name **and** by URL, so pushing to the URL directly
   is refused too); any push of the trunk, deletion included; deletion of the mirror; and any
   mirror push that is not an ancestor of the last-fetched upstream ref. Everything else, including
   deleting stale `sync/*` branches, is allowed. Say clearly that the mirror check validates
   against the **last fetch** of upstream - `git fetch <upstream>` before pushing the mirror, and a
   missing or unfetched upstream ref is a refusal ("cannot verify"), not a pass.

   Pushes to the upstream remote fail even earlier, on the `DISABLED` push URL, before any hook
   runs.

6. **`.forkflow.toml`.** The template is written commented-out and left **untracked**; tell the
   user to commit it once the branch names and the `gate` list are right, so everyone in the fork
   shares them. Reading it needs Python 3.11+ (`tomllib`); a config that is present but unreadable
   is exit 2 for every subcommand - it carries the safety-critical branch names.

7. **Report** what changed in the clone (push URL, hook, config keys, any branch created), what
   was only reported (the platform findings and their fix commands), and what is left for the
   user: commit `.forkflow.toml`, run the platform fixes, then `/forkflow:sync` before
   `/forkflow:ship`.

## Refusals to handle, not work around

| exit 2 | meaning | what to do |
|---|---|---|
| mirror "has commits that are not in upstream" | the fork's mirror branch carries its own work - this is a **migration**, not a setup | point at the README section *Adopting forkflow in an existing fork* and stop. Never reset, force-push or delete that branch on the user's behalf; it is done once, by hand, by a Maintainer, after a backup |
| trunk exists locally but not on origin | the script never pushes the trunk | the user pushes it once themselves (`git push -u origin <trunk>`) or deletes it and reruns setup |
| `core.hooksPath` is set | the hooks directory is shared with other repositories | say what that means; install it there by hand, or rerun with `--force` if the user accepts it |
| an existing foreign `pre-push` hook | not forkflow's | rerun with `--force` after the user agrees; the old hook is kept as `pre-push.pre-forkflow` |
| no remote for the original project | nothing to fetch | ask for the URL, rerun with `--upstream-url` |
| fetch failed | wrong URL or no access | nothing has been changed yet; fix the URL and rerun |

## Notes

- Running `setup` again is safe and idempotent: it reports what is already in place and rewrites
  only its own hook.
- A fresh fork (only the mirror branch on origin) is the one case where the script creates and
  pushes the trunk - at the upstream SHA, so the push carries nothing of the fork's own.
- After setup, verify with `/forkflow:status`: the setup line should read
  `upstream push: DISABLED   pre-push hook: installed   ff-only: <trunk> yes, <mirror> yes`.
