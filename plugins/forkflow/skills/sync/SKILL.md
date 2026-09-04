---
name: sync
description: "Take the upstream project's latest into a fork: fast-forward and push the pristine mirror branch, then bring it into the protected trunk through a sync branch, one merge commit and a merge request, resolving conflicts on the way. Use when the user says \"forkflow sync\", \"sync upstream\", \"pull in upstream\", \"take upstream's changes\", \"update the fork from upstream\", or when a status shows the trunk behind upstream."
allowed-tools: Bash, Read, AskUserQuestion
---

# forkflow sync

The script never pushes the trunk, never rebases it, and never commits on the mirror - and neither
may you. The trunk moves only through the merge request this produces; you never merge that MR
yourself. Rule 5 matters here: a sync MR is merged **as a merge**, never squashed and never
rebased.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py`.
Read `${CLAUDE_PLUGIN_ROOT}/references/rules.md` before the first run in a session.

## Process

1. **Dry run first.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" sync --dry-run
   ```

   It changes nothing (no branch, no backup, no push, the mirror is not moved) and still previews
   the *pending* merge: the upstream commits that would be taken, and the merge simulation's
   verdict - clean or the list of conflicting paths. Tell the user what is coming, especially the
   conflicting files, before doing it.

   `sync` needs a clean tree and an attached HEAD (exit 2 otherwise) and starts from whatever
   branch the user is on; it announces that it leaves that branch and stays on the sync branch.

2. **Run it.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" sync
   ```

   In order: fetch both remotes; resolve `target` (the upstream SHA everything after this uses);
   fast-forward the mirror and push it; stop with "already in sync" if the trunk already has the
   target; simulate the merge; back up `origin/<trunk>` as `backup/<ts>-pre-sync` and confirm it on
   origin; create `sync/<upstream>-<YYYYMMDD>` off `origin/<trunk>`; one `--no-ff` merge commit;
   the both-sides table; `check`; push the sync branch; print the MR command.

3. **Exit 4 - conflicts.** The merge stopped, the markers are in the tree, the sync branch is
   checked out, and the conflicting files were listed. The trunk has not moved; the mirror was
   already advanced and pushed, which is fine and stays.

   Resolve them **file by file**: read the file, show the user both sides (`git log --oneline
   HEAD..MERGE_HEAD -- <file>` for what upstream did, `git diff $(git merge-base HEAD MERGE_HEAD)
   HEAD -- <file>` for what the fork did), and keep both intents where they can coexist. This is
   the judgment part - the script does not guess. Ask the user when a hunk is a genuine either/or,
   and never resolve by taking one side wholesale just because it is shorter. Then `git add` each
   resolved file (do not commit - `--continue` commits the merge).

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" sync --continue
   ```

   It commits the merge, then resumes at the verification with the parents of the merge that was
   actually made, so nothing that moved in between changes what is checked.

4. **Read the both-sides table.** Every file changed on *both* sides of the merge gets a row -
   not only the conflicted ones. A clean merge is not automatically a correct one.

   | row | meaning |
   |---|---|
   | `ours a/b  theirs c/d` with full counts | both sides' added lines are in the merged file and their removed lines are gone: nothing was lost |
   | `CHECK` (short counts) | one side's lines did not survive - open the file and look |
   | `CHECK deleted` / `CHECK binary` / `CHECK renamed` | the check could not be made mechanically - look yourself |

   Open every flagged file, decide whether the loss was intended, and say so in your report. If a
   resolution dropped something it should not have, fix it, `git add`, and rerun `--continue`.

5. **Merge request.** The script prints the command; run it in the same invocation with `--mr`
   (add `--title` to override the default `sync: <upstream>/<branch> <date> (n commits)`). The
   body it composes carries the upstream commits, the both-sides rows, the mirror advance, the
   backup name with its rollback line, and the merge-button note. If the tool is missing or fails,
   the command and its stderr are printed and the exit stays 0 - the branch is pushed, only the MR
   is left; hand the user the command.

6. **Report.** Mirror advance (old -> new, pushed), the sync branch name, the merge commit, the
   number of upstream commits taken, the backup name and its rollback line, any `CHECK` rows and
   what you concluded about them, the MR URL or command - and what was *not* done: the MR is not
   merged and the trunk has not moved.

   Close with the manual step, which is deliberately not automated:

   ```bash
   git fetch origin && git switch <trunk> && git merge --ff-only origin/<trunk>
   ```

   (`setup`'s ff-only config guarantees that can never become a merge commit.) Tell the user the
   MR must be merged **as a merge** - GitLab: merge/fast-forward the sync branch; GitHub: "Create
   a merge commit" - never squash, never rebase.

## Other exits

| exit | what happened | what to do |
|---|---|---|
| 0 | done, dry run, or "already in sync" | nothing; the mirror may still have been advanced and pushed |
| 2 | precondition: dirty tree, detached HEAD, sync branch already exists locally or on origin, mirror checked out in another worktree, mirror diverged, untracked file blocking the mirror fast-forward | fix what the message names; `--force` recreates an existing sync branch, and that is the only thing `--force` does here - when that name is already on origin the rerun publishes the next free `<name>-N` instead (a sync branch is never force-pushed), and the stale merge request is closed by hand |
| 3 | `check` failed after the merge - a `gate` command, or `origin/<trunk>` moved under the branch | read the hint the run printed: a failing gate is fixed with a commit on the sync branch and `sync --continue` (the resume picks up the merge commit, wherever it now sits in the branch); a trunk that moved on means the sync is redone against the new tip with `sync --force` - a sync MR is never rebased |
| 4 | merge conflicts | resolve, `git add`, `sync --continue` |
| 5 | rewrite safety: the backup was not confirmed on origin, or a push was rejected | do not work around it; report it - a rejected mirror push usually means the mirror is not a pure copy of upstream |

## Notes

- Sync before you ship. A feature rebased onto a trunk that already contains upstream resolves its
  conflicts once, locally.
- Never `git push origin <trunk>`, never `git rebase` the trunk, never commit on the mirror, and
  never force-push anything the script did not (the pre-push hook refuses all of it anyway - do
  not use `--no-verify` to get past it, report it instead).
- A sync MR that goes stale is redone with `forkflow sync`, not rebased in the web UI.
- `.forkflow.toml` (`gate`, branch names) needs Python 3.11+; if the config is present but
  unreadable every subcommand exits 2 rather than guessing branch names.
