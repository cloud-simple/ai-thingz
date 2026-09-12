---
name: sync
description: "Take the upstream project's latest into a fork: fast-forward and push the pristine mirror branch, then bring it into the protected trunk through a sync branch, one merge commit and a merge request, resolving conflicts on the way. Use when the user says \"forkflow sync\", \"sync upstream\", \"pull in upstream\", \"take upstream's changes\", \"update the fork from upstream\", or when a status shows the trunk behind upstream."
allowed-tools: Bash, Read, AskUserQuestion
---

# forkflow sync

The script never pushes the trunk, never rebases it, and never commits on the mirror - and neither
may you. The trunk moves only through the merge request this produces; you never merge that MR
yourself - *unless the fork's `.forkflow.toml` says `merge = "self"` and the user asked for
`--merge`*, in which case the script merges it, as rule 5 requires and with a head-commit
guard, and lands it. Rule 5 matters here: a sync MR is merged **as a merge**, never squashed
and never rebased. The local trunk catches up through `forkflow land`, never by hand.

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
   conflicting files, before doing it. It does not fetch either: the `fetch` line shows a
   `git ls-remote` of each remote beside what this clone has, and the preview is of the upstream
   commits already fetched - when that line says `NOT fetched (dry run)`, upstream has more, and
   the real run (or a `git fetch upstream`) is what brings it into view.

   `sync` needs a clean tree and an attached HEAD (exit 2 otherwise) and starts from whatever
   branch the user is on; it announces that it leaves that branch and stays on the sync branch
   (or, with `--merge`, ends on the trunk once the landing is done).

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
   actually made, so nothing that moved in between changes what is checked. Resume with the flag
   the first run had (`--mr` or `--merge`) - the conflict message prints the command with it.

4. **Read the both-sides table.** Every file changed on *both* sides of the merge gets a row -
   not only the conflicted ones. A clean merge is not automatically a correct one.

   | row | meaning |
   |---|---|
   | `ours a/b  theirs c/d` with full counts | both sides' added lines are in the merged file and their removed lines are gone: nothing was lost |
   | `CHECK` (short counts) | one side's lines did not survive - open the file and look |
   | `CHECK deleted` / `CHECK binary` / `CHECK renamed` | the check could not be made mechanically - look yourself |

   Open every flagged file, decide whether the loss was intended, and say so in your report. If a
   resolution dropped something it should not have, fix it, `git add`, and rerun `--continue`.

   `.forkflow.toml` is one of those files, and the run says so with a `CHECK` line of its own when
   the merge changes it: it names the branches every safety check depends on and holds `gate`,
   which forkflow runs with `sh -c`. When *this* merge changed the `gate`, the commands that
   arrived are printed rather than run (`gate - NOT RUN`) - what is shown is exactly what the
   `forkflow check` the message names would run. Read the `git diff <merge>^1 HEAD --
   .forkflow.toml` line the run prints with the user, and treat a `gate` that arrived from the
   original project as untrusted shell until they have said otherwise - it runs on every later
   `check`, `sync` and `ship`. A `gate` the merge left alone runs as usual, even when upstream
   edited another line of the file.

5. **Merge request.** The script prints the command; run it in the same invocation with `--mr`
   (add `--title` to override the default `sync: <upstream>/<branch> <date> (n commits)`). It
   carries `--repo <the fork's URL>`: hand it over exactly as printed, never trimmed - without
   that flag both tools open the merge request on the original project (`rules.md`). The
   body it composes carries the upstream commits, the both-sides rows, the mirror advance, the
   backup name with its rollback line, and the merge-button note. If the tool is missing or fails,
   the command and its stderr are printed and the exit stays 0 - the branch is pushed, only the MR
   is left; hand the user the command.

   **`--merge`** (implies `--mr`) opens the merge request and merges it in the same run, then
   lands it (step 7). It is refused - exit 2, right after the header and before the fetch, the
   mirror advance, the backup and any push, on a plain run and on `--continue` alike - unless
   **both** hold: the fork's `.forkflow.toml` says `merge = "self"` (the fork has declared that
   whoever opens its merge requests merges them; the default is `"manual"`), and the origin URL
   names a project the merge command can address with `--repo` (`rules.md`). The `merge = "self"`
   has to be the fork's own: it is read only from the `.forkflow.toml` committed on
   `origin/<trunk>` (or, while none is, an untracked one), and only while those bytes are not a
   `.forkflow.toml` the original project has - never from the sync branch, whose tree holds
   upstream's file. Refused on `--continue`, resume with the command it prints (`sync --continue
   --mr`: the MR is opened, not merged) and have the MR merged by hand. It is asked again right
   before the merge; a fork that stopped saying `"self"` meanwhile is exit 6. Never work around
   that gate - a reviewed fork is meant to stop here. The merge is the one rule 5 requires, with
   a head-commit guard so only the merge commit this run pushed can be merged: GitLab `glab mr
   merge <sync branch> --repo <fork> --sha <head> --auto-merge=false --remove-source-branch
   --yes` (the project's own merge method - a fast-forward of the sync branch, whose tip *is* the
   merge commit); GitHub `gh pr merge <sync branch> --repo <fork> --match-head-commit <head>
   --merge`. A merge that does not happen - tool missing, failing, the guard refusing, or no
   merge request created - is exit 6: the sync branch is pushed and the request (when created) is
   open, so merge it by hand **as a merge** and run `forkflow land`. With `--merge` the run ends
   on the trunk, not on the sync branch. `--dry-run` shows `would: merge` and `would: land` and
   runs neither. Run from a linked worktree while the trunk is checked out in another one,
   `--merge` merges and then ends with exit 2 "the merge request was merged; the local catch-up
   did not run": expected - `forkflow land <sync branch>` in the worktree the message names
   catches the trunk up (it cannot delete a branch the linked worktree still has checked out). A
   merge the tool only queued is exit 6 there as anywhere, naming the same command and worktree
   for once it is through.

6. **Report.** Mirror advance (old -> new, pushed), the sync branch name, the merge commit, the
   number of upstream commits taken, the backup name and its rollback line, any `CHECK` rows and
   what you concluded about them, the MR URL or command - and what was *not* done: without
   `--merge` the MR is not merged and the trunk has not moved; with it, what the merge step and
   the landing did (step 7).

7. **Land it.** Tell the user the MR must be merged **as a merge** - GitLab: merge/fast-forward
   the sync branch; GitHub: "Create a merge commit" - never squash, never rebase. Once it is
   merged, the closing step is

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" land
   ```

   (`/forkflow:land`; the run prints `next: forkflow land`). It fetches, verifies that the merge
   commit is an ancestor of `origin/<trunk>`, fast-forwards the local trunk (`git merge
   --ff-only`, so it can never become a merge commit), deletes the local sync branch and
   leaves you on the trunk. Not merged yet is exit 2 and not an error. A sync that was squashed
   or rebased in the UI never lands by that check - `land` says rule 5 was broken and names
   `land --force` as the way to fast-forward anyway; report it rather than smoothing it over.
   `--merge` runs that landing itself, right after the merge.

## Other exits

| exit | what happened | what to do |
|---|---|---|
| 0 | done, dry run, or "already in sync"; also `--mr` when the tool is missing or fails, without `--merge` | after a done run, `forkflow land` once the MR is merged; the mirror may still have been advanced and pushed |
| 2 | precondition: dirty tree, detached HEAD, sync branch already exists locally or on origin, mirror checked out in another worktree, mirror diverged, untracked file blocking the mirror fast-forward, untracked file the merge would write over (`setup` leaves `.forkflow.toml` untracked - git-ignored or not, since git overwrites an ignored file silently - and an upstream that uses forkflow tracks it - under that name or, on a case-insensitive filesystem, as `.ForkFlow.toml`, the same file there; never delete it: commit it on a branch and ship it with the command printed, then sync again; no backup or sync branch was made), or a `.forkflow.toml` the merge brought in that cannot be read, a case variant of the name included (the merge commit, the sync branch, the mirror push and the backup are already made and the message says so - fix the file on the sync branch with the command it prints - which first copies the file into the git directory and names the copy, and for a variant beside the fork's own config goes through the index only - commit it, then the `sync --continue` it prints; do not edit the file before running it) | fix what the message names; `--force` recreates an existing sync branch, and that is the only thing `--force` does here - when that name is already on origin the rerun publishes the next free `<name>-N` instead (a sync branch is never force-pushed), and the stale merge request is closed by hand |
| 3 | `check` failed after the merge - a `gate` command, or `origin/<trunk>` moved under the branch | read the hint the run printed: a failing gate is fixed with a commit on the sync branch and `sync --continue` (the resume picks up the merge commit, wherever it now sits in the branch); a trunk that moved on means the sync is redone against the new tip with `sync --force` - a sync MR is never rebased |
| 4 | merge conflicts | resolve, `git add`, `sync --continue` |
| 5 | rewrite safety: the backup was not confirmed on origin, or a push was rejected | do not work around it; report it - a rejected mirror push usually means the mirror is not a pure copy of upstream |
| 6 | `--merge` only: the merge request was not created by this run (or one was already open for the branch - `--merge` merges only what it opened), or was not merged (tool missing or failing, the head-commit guard refused because the branch moved, or the tool answered "merged" while nothing reached the trunk - a merge train, auto-merge) - the sync branch is pushed and the request, when created, is open with its URL in the message | merge it by hand **as a merge**, then `forkflow land`; the pending record is kept for it. Never retry the merge through `glab api` / `gh api` yourself |

## Notes

- Sync before you ship. A feature rebased onto a trunk that already contains upstream resolves its
  conflicts once, locally.
- Never `git push origin <trunk>`, never `git rebase` the trunk, never commit on the mirror, and
  never force-push anything the script did not (the pre-push hook refuses all of it anyway - do
  not use `--no-verify` to get past it, report it instead).
- A sync MR that goes stale is redone with `forkflow sync`, not rebased in the web UI.
- After the push the run records what is waiting to land (`forkflow status` shows it on its
  `pending` line); `forkflow land` reads that record, so it works in a later session too.
- `.forkflow.toml` (`gate`, `merge`, branch names) needs Python 3.11+; if the config is present
  but unreadable every subcommand exits 2 rather than guessing branch names.
