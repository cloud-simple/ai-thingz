---
name: ship
description: "Take a feature branch in a fork to the protected trunk as ONE squashed commit on top of a freshly fetched trunk, backed up and tree-hash verified, through a merge request. Use when the user says \"forkflow ship\", \"ship this branch\", \"land this branch\", \"squash and open the MR\", \"get this branch into develop\", or asks to open a merge request for the branch they are on."
allowed-tools: Bash, Read, AskUserQuestion
---

# forkflow ship

The script never pushes the trunk, never rebases it, and never commits on the mirror - and neither
may you. `ship` rewrites only the feature branch it is on, after a backup is confirmed on origin;
the trunk moves only through the merge request this produces, which you never merge yourself.

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

8. **Report.** The one commit (SHA and subject) replacing the n originals, the tree hash check,
   the backup name and its rollback line, the rebase result, any WARNING files, the MR URL or
   command - and what was not done: the MR is not merged, the trunk has not moved.

   Close with the manual steps:

   ```bash
   git fetch origin && git switch <trunk> && git merge --ff-only origin/<trunk>
   ```

   Merge button - GitLab: fast-forward; GitHub: "Rebase and merge". **On GitHub, delete the local
   feature branch afterwards**: "Rebase and merge" rewrites the commit, so the local branch is a
   stale copy of what landed. The script prints that reminder when the platform is GitHub.

## Other exits

| exit | what happened | what to do |
|---|---|---|
| 0 | done, dry run, or "nothing to ship" | nothing to land beyond `origin/<trunk>` |
| 2 | precondition: on the trunk / the mirror / a `sync/` or `backup/` branch, dirty tree, detached HEAD, rebase already in progress, `--continue` with an unfinished rebase or with no ship of ours to resume, `origin/<branch>` carrying commits this clone has no record of publishing | switch to the feature branch or finish the rebase; the message names it, and the command it names after `git rebase --continue` is the right one - `forkflow ship --continue` only when this clone has a ship in progress on that branch, plain `forkflow ship` otherwise (a `git pull --rebase` this skill asked for is not a ship). For the last one the commits that would be lost are listed - take them in (`git pull --rebase origin <branch>`), or, if they are yours from another clone, keep them in a `backup/` branch on origin first (the message prints that command too) |
| 3 | `check` failed after the rebase and squash - a `gate` command or the tip check; nothing was pushed | fix it on the branch, commit, then `ship --continue` (the squash already happened, so that, not a fresh `ship`, is the resume); the rollback line is printed |
| 4 | rebase conflicts | resolve, `git rebase --continue`, `ship --continue` |
| 5 | rewrite safety: backup not confirmed, tree hash changed by the squash, `--force-with-lease` rejected (someone else pushed to the branch) | do not force past it; report it and use the printed rollback line |

## Notes

- The squash is verified by tree hash: the result must be byte-identical in content to the rebased
  branch's tip. A mismatch is exit 5, never a "probably fine".
- Never `git push origin <trunk>`, never rebase the trunk, never `--force` (only the script's
  `--force-with-lease`), never `--no-verify` to get past the pre-push hook.
- Backups live on origin as `backup/<UTC timestamp>-pre-ship`; `forkflow status` lists the newest.
- `.forkflow.toml` (`gate`, branch names) needs Python 3.11+; a present but unreadable config is
  exit 2 for every subcommand rather than a guessed branch name.
