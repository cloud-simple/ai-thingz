---
name: ship
description: "Take a feature branch in a fork to the protected trunk as ONE squashed commit on top of a freshly fetched trunk, backed up and tree-hash verified, through a merge request. Use when the user says \"forkflow ship\", \"ship this branch\", \"land this branch\", \"squash and open the MR\", \"get this branch into develop\", or asks to open a merge request for the branch they are on."
allowed-tools: Bash, Read, AskUserQuestion
---

# forkflow ship

The script never pushes the trunk, never rebases it, and never commits on the mirror - and neither
may you. `ship` rewrites only the feature branch it is on, after a backup is confirmed on origin;
the trunk moves only through the merge request this produces, which you never merge yourself -
*unless the fork's `.forkflow.toml` says `merge = "self"` and the user asked for `--merge`*, in
which case the script merges it, with the method rule 5 requires and a head-commit guard, and
lands it. The local trunk catches up through `forkflow land`, never by hand.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py`.
Read `${CLAUDE_PLUGIN_ROOT}/references/rules.md` before the first run in a session.

## Process

1. **Sync first if upstream has moved.** `forkflow status` (or `/forkflow:status`) says so. Rule:
   sync first, then ship - rebase locally, merge globally.

2. **Dry run.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" ship --dry-run
   ```

   Nothing is created, rebased, squashed or pushed (it does fetch). Use it to show the user the
   commits that will become one, and the upstream-tracked WARNING list it prints where the real
   run prints it after the squash.

3. **Run it.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" ship
   ```

   Preflight requires a feature branch (not the trunk, not the mirror, not a `sync/` or `backup/`
   branch), a clean tree, an attached HEAD and no rebase in progress - all exit 2. Then: fetch
   origin; "nothing to ship" and exit 0 if the branch has no commits beyond `origin/<trunk>`; back
   up HEAD as `backup/<ts>-pre-ship` and confirm it on origin; `git rebase origin/<trunk>`; squash
   to one commit and verify the tree hash is unchanged; `check`; push (with
   `--force-with-lease` when the branch is already on origin); print the MR command.

4. **The commit message.** By default: the *oldest* commit's subject as the subject, then
   "Squashed from n commits (oldest first)" with each subject and its body. A single-commit branch
   keeps its message unchanged. If the composed message would not read well, write a better one to
   a file and pass `--message-file <file>` - it is used verbatim. `--title` overrides only the MR
   title, not the commit.

5. **Exit 4 - rebase conflicts.** The rebase stopped; HEAD is detached mid-rebase and the
   conflicting files were listed. Resolve them file by file, showing the user both sides, `git
   add` each one, then:

   ```bash
   git rebase --continue
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" ship --continue
   ```

   `--continue` re-runs the full preflight, requires `origin/<trunk>` to be an ancestor of HEAD
   (otherwise "the rebase did not complete", exit 2) and resumes at the squash. `git rebase
   --abort` puts the branch back; the pre-ship backup on origin is the other way back.

6. **Upstream-tracked files.** The WARNING list names files the branch touches that upstream also
   owns; every one of them is a permanent merge cost. Show it, do not gate on it. Ask the user
   only when the branch adds a file to that list unexpectedly - an upstream file edited where a
   fork-owned file or an override would have done.

7. **Merge request.** `--mr` runs the printed command. The body is the squashed commit message
   plus the WARNING list and the merge-button note. Tool missing or failing -> the command and its
   stderr are printed, exit stays 0; hand the user the command exactly as printed - its
   `--repo <the fork's URL>` is what keeps the merge request off the original project
   (`rules.md`).

   **`--merge`** (implies `--mr`) opens the merge request and merges it in the same run, then
   lands it (step 9). It is refused - exit 2, before the fetch, the backup and any push, on a
   plain run and on `--continue` alike - unless **both** hold: the fork's `.forkflow.toml` says
   `merge = "self"` (the fork has declared that whoever opens its merge requests merges them;
   the default is `"manual"`), and the origin URL names a project the merge command can address
   with `--repo` (`rules.md`). Never work around that gate - a reviewed fork is meant to stop
   here. The merge is the method rule 5 requires with a head-commit guard, so only the exact
   commit this run pushed can be merged: GitLab `glab mr merge <branch> --repo <fork> --sha
   <head> --auto-merge=false --remove-source-branch --yes` (the project's own merge method,
   which `setup`'s report insists is `ff`); GitHub `gh pr merge <branch> --repo <fork>
   --match-head-commit <head> --rebase`. A merge that does not happen - tool missing, failing,
   the guard refusing, or no merge request created - is exit 6: the branch is pushed and the
   merge request (when created) is open, so merge it by hand and run `forkflow land`. With
   `--merge` the run ends on the trunk, not on the feature branch. `--dry-run` shows `would:
   merge` and `would: land` and runs neither.

8. **Report.** The one commit (SHA and subject) replacing the n originals, the tree hash check,
   the backup name and its rollback line, the rebase result, any WARNING files, the MR URL or
   command - and what was not done: without `--merge` the MR is not merged and the trunk has not
   moved; with it, what the merge step and the landing did (step 9).

9. **Land it.** Merge button - GitLab: fast-forward; GitHub: "Rebase and merge". Once the merge
   request is merged, the closing step is

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" land
   ```

   (`/forkflow:land`; the run prints `next: forkflow land`). It fetches, verifies that the
   shipped commit is on `origin/<trunk>` - by ancestry, or by patch when GitHub's "Rebase and
   merge" rewrote its SHA - fast-forwards the local trunk, deletes the local feature branch (kept
   instead when it carries commits made after the ship) and leaves you on the trunk. Not merged yet is exit 2 and not an error. On GitHub the remote
   branch may survive a rebase merge; `land` prints the `git push origin --delete <branch>` line
   for the user. `--merge` runs that landing itself, right after the merge.

## Other exits

| exit | what happened | what to do |
|---|---|---|
| 0 | done, dry run, or "nothing to ship"; also `--mr` when the tool is missing or fails, without `--merge` | after a done run, `forkflow land` once the MR is merged; "nothing to ship" means nothing to land beyond `origin/<trunk>` |
| 2 | precondition: on the trunk / the mirror / a `sync/` or `backup/` branch, dirty tree, detached HEAD, rebase already in progress, `--continue` with an unfinished rebase or with no ship of ours to resume, `origin/<branch>` carrying commits this clone has no record of publishing | switch to the feature branch or finish the rebase; the message names it, and the command it names after `git rebase --continue` is the right one - `forkflow ship --continue` only when this clone has a ship in progress on that branch, plain `forkflow ship` otherwise (a `git pull --rebase` this skill asked for is not a ship). For the last one the commits that would be lost are listed - take them in (`git pull --rebase origin <branch>`), or, if they are yours from another clone, keep them in a `backup/` branch on origin first (the message prints that command too) |
| 3 | `check` failed after the rebase and squash - a `gate` command or the tip check; nothing was pushed | fix it on the branch, commit, then `ship --continue` (the squash already happened, so that, not a fresh `ship`, is the resume); the rollback line is printed |
| 4 | rebase conflicts | resolve, `git rebase --continue`, `ship --continue` |
| 5 | rewrite safety: backup not confirmed, tree hash changed by the squash, `--force-with-lease` rejected (someone else pushed to the branch) | do not force past it; report it and use the printed rollback line |
| 6 | `--merge` only: the merge request was not created, or was not merged (tool missing or failing, or the head-commit guard refused because the branch moved) - the branch is pushed and the request, when created, is open with its URL in the message | merge it by hand with the method rule 5 requires, then `forkflow land`; the pending record is kept for it. Never retry the merge through `glab api` / `gh api` yourself |

## Notes

- The squash is verified by tree hash: the result must be byte-identical in content to the rebased
  branch's tip. A mismatch is exit 5, never a "probably fine".
- Never `git push origin <trunk>`, never rebase the trunk, never `--force` (only the script's
  `--force-with-lease`), never `--no-verify` to get past the pre-push hook.
- Backups live on origin as `backup/<UTC timestamp>-pre-ship`; `forkflow status` lists the newest.
- After the push the run records what is waiting to land (`forkflow status` shows it on its
  `pending` line); `forkflow land` reads that record, so it works in a later session too.
- `.forkflow.toml` (`gate`, `merge`, branch names) needs Python 3.11+; a present but unreadable
  config is exit 2 for every subcommand rather than a guessed branch name.
