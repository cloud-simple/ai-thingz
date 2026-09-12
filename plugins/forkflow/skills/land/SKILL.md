---
name: land
description: "Finish a fork's ship or sync once its merge request is merged: fetch, verify that the pushed commit is on the protected trunk, fast-forward the local trunk to it, delete the landed local branch and leave you on the trunk. Use when the user says \"forkflow land\", \"the MR merged\", \"catch develop up\", \"catch the trunk up\", or after a ship or a sync printed `next: forkflow land` and the merge request has since been merged."
allowed-tools: Bash, Read
---

# forkflow land

The script never pushes the trunk, never rebases it, and never commits on the mirror - and neither
may you. `land` is the one place the plugin moves the local trunk (`setup` only creates it on a
fresh fork), and only by fast-forward to what `origin/<trunk>` already holds after the platform
merged the request; it pushes nothing,
rewrites nothing, and deletes a local branch only once it has seen that branch's pushed commit on
the trunk - and only while the branch's tip is still that commit: a commit made on the branch after
the ship keeps it.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py`.
Read `${CLAUDE_PLUGIN_ROOT}/references/rules.md` before the first run in a session.

## Process

1. **Run it.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" land
   ```

   It finishes what a `ship` or a `sync` recorded as *pending* when it pushed its branch - one
   record per branch, shared by every worktree of the clone, so ships made in linked worktrees
   are landed from whichever worktree has the trunk checked out. Which record: `forkflow land
   <branch>` names one; plain `land` on a branch that has a record lands that one; plain `land`
   anywhere else (on the trunk, usually) lands every record whose merge request has landed and
   lists the rest, kept, as `pending` lines. Preflight: a pending record ("nothing pending"
   otherwise, or "nothing pending for `<branch>`" naming what is), no rebase in progress, a clean
   tree, and the trunk not checked out in another worktree (the message names it and the command
   to run there: `forkflow land <branch>`, or plain `forkflow land` when several records are
   pending) - all exit 2, before the fetch. Then: fetch origin; decide whether the recorded
   commit is on `origin/<trunk>` - by ancestry (a fast-forward or a merge commit keeps the SHA),
   and for a ship also by patch, so a "squash and merge" or a "rebase and merge" that gave the
   commit a new SHA is recognised too; create the local trunk from `origin/<trunk>` if this clone
   has none; `git checkout <trunk>`; `git merge --ff-only origin/<trunk>`; delete the landed
   branch (`-d`, or `-D` when the landing was a rewritten copy - the patch is verifiably on the
   trunk) only while its tip is still the commit that was pushed, and otherwise keep it and say
   so (its later commits did not land); forget the record; print the `landed:` line. HEAD is on
   the trunk when it finishes, whichever branch it started from. With several records, the trunk
   is fast-forwarded once and each landed branch follows.

2. **"Not on origin/<trunk> yet" is exit 2 and not an error.** The merge request named in the
   message has not been merged: say so, and run `forkflow land` again once it is. Nothing moved -
   the trunk, the branch and the record are as they were. With several records and none landed
   the message lists each with its merge request. Plain `land` on a branch that has a record of
   its own answers for that record only; when it has not landed but others have, the message
   names each of those as `forkflow land <branch>` - run that one. For a sync the message adds the
   rule-5 note: a sync that was squashed or rebased in the web UI never becomes an ancestor of
   the trunk, so it can never be recognised - that is the one case `--force` exists for on a
   sync, and it is a broken rule 5 to report, not a quirk to smooth over.

3. **`--force`** - only when the user says the merge request was merged or closed in a way the
   tool cannot see (closed without merging, a sync squashed in the UI), or when plain `land`
   says `cannot verify the landing` - the recorded commit is no longer in this clone (its branch
   deleted and pruned after the request was abandoned). It fast-forwards the local trunk to
   whatever `origin/<trunk>` holds, **keeps** the branch (nothing proved it landed - the run
   says `is kept: its landing was not verified` and ends on `caught up, landing NOT verified:`),
   and clears the record. It is the only escape; never `git branch -f`, `git reset` or
   `git push` the trunk into shape by hand. A verified landing under `--force` is an ordinary
   landing - the branch is deleted as usual. It acts on one record: with several pending and
   HEAD on none of their branches it is refused (exit 2) until the branch is named -
   `forkflow land --force <branch>`.

4. **Dry run.** `--dry-run` fetches and decides, prints every mutating step as `would:`
   (checkout, fast-forward, branch deletion), moves nothing and keeps the record. Use it when
   the user wants to see what would land before it does, or to answer "has it merged yet?"
   without side effects (`forkflow status` answers that too, on its `pending` line).

5. **Report.** What landed and how (`ancestor` or `rewritten`, with the trunk commit it is on),
   the fast-forward (`<old>..<new>`, or `up to date` when the local trunk was already there),
   that the user is now on the trunk, which branch was deleted - or kept, and why - and any
   WARNING. On GitHub pass on the printed line: GitHub deletes no branch on merge, after a ship
   or a sync, and `git push origin --delete <branch>` is the user's call, never run unasked.

## Reading the output

After the usual header (mirror line, trunk line, divergence), one line per step:

```
  fetch  $ git fetch origin ...  -> ...
  landed?  $ git merge-base --is-ancestor <sha> origin/<trunk>  -> yes - as <sha> (ancestor) | no - <sha> is not on origin/<trunk>
  landed?  $ git cherry <sha> origin/<trunk> <base>  -> yes - as <trunk sha> (rewritten)
  trunk  $ git branch --no-track <trunk> origin/<trunk>  -> created at <sha> (no local `<trunk>` before)
  checkout  $ git checkout <trunk>  -> on <trunk>
  trunk  $ git merge --ff-only origin/<trunk>  -> <old> -> <new>
  trunk  $ git rev-parse <trunk>  -> up to date at <sha>          (instead, when the local trunk was already there)
  branch  $ git branch -d|-D <branch>  -> deleted (landed as <sha>) | `<branch>` is already gone | NOT deleted: <git's line>
  branch  $ git rev-parse <branch>  -> kept: `<branch>` is at <sha>, not the <sha> that was pushed - its later commits did not land
  origin  $ git branch -d -r origin/<branch>  -> removed - `<branch>` is gone from origin
  landed: <trunk> <old>..<new> - you are on <trunk>          (or `landed: <trunk> at <sha>` when nothing moved)
  pending  <ship|sync> <branch> -> MR <url|-> - <verdict>    (each record still pending, as `status` shows it)
```

With several records the `landed?` results name the branch (``yes - `<branch>` as <sha>``) and
the closing line lists what landed: ``landed `<branch>`, `<branch>`: <trunk> ...``.

Under `--dry-run` the mutating steps and the last line carry `would:` (`would: landed: ...`).

| what you see | what it means |
|---|---|
| `landed? ... yes - as <sha> (ancestor)` | the commit that was pushed is on the trunk under its own SHA - a fast-forward, or a merge commit |
| `landed? ... yes - as <sha> (rewritten)` | a ship landed under a new SHA with the same patch ("squash and merge", "rebase and merge") and the trunk's tip still has the change (a patch reverted since is not a landing; an empty ship lands by ancestry only); the local branch is deleted with `-D` |
| `landed? ... no - <sha> is not on origin/<trunk>` | not merged yet (exit 2, nothing moved); for a sync the message adds the rule-5 note |
| `landed? ... landing not verified (--force): fast-forwarding to whatever origin/<trunk> holds` | the escape hatch ran; the branch is kept |
| `WARNING: the ship MR was merged as a merge commit` | rule 5 asks for a fast-forward - the ship's commit is on the trunk but hangs off a merge commit; the landing is done, and the project's merge method wants checking (`forkflow setup` reports it) |
| `trunk ... created at <sha> (no local <trunk> before)` | a single-branch clone had no local trunk; it now has one, on `origin/<trunk>` |
| `` `<branch>` is kept: its landing was not verified `` | `--force` without a recognised landing - delete the branch yourself only when the user is sure |
| `` branch ... kept: `<branch>` is at <sha>, not the <sha> that was pushed `` | the landing is done, but the branch has commits made after the ship: they are on no trunk, so the branch stays - tell the user, they ship them or drop them |
| `branch ... NOT deleted: <line>` | the landing is done; git refused to delete the branch and said why (it is checked out in another worktree, say) |
| `origin ... removed - <branch> is gone from origin` | GitLab's `--remove-source-branch` removed the branch; the stale `origin/<branch>` this clone kept is dropped (under `--force` too, where the local branch is kept), so the name can be shipped again |
| `origin/<branch> may still exist: git push origin --delete <branch>` | GitHub (a ship or a sync): the remote branch is not removed by the merge; the user's call |
| `landed: <trunk> <old>..<new> - you are on <trunk>` | done; HEAD is on the trunk (`at <sha>` when the local trunk was already there) |
| `caught up, landing NOT verified: <trunk> ...` | `--force` ran without a recognised landing: the trunk follows origin, the branch is kept |
| `pending ... kept: the record for <branch> changed while this ran` | another run recorded a new ship of that branch while this one landed the old one; the new record stays for its own `land` |
| `pending  <kind> <branch> -> MR ... - <verdict>` after the landing | a record this run did not land (another branch, not merged yet, or one it was not asked to land) - kept |

## Exits

| exit | what happened | what to do |
|---|---|---|
| 0 | landed (or caught up under `--force`) | nothing; the landed records are cleared, and any still pending are listed and kept |
| 0 | dry run | nothing moved and the records are kept - run it without `--dry-run` to land |
| 2 | precondition: nothing pending (or nothing for the named branch), rebase in progress, dirty tree, trunk checked out in another worktree, the fetch failed, the recorded commit is not in this clone (`cannot verify the landing` - `--force` clears it), not on `origin/<trunk>` yet (with several records: none of them), `--force` with several records and none named, the local trunk carries commits origin lacks, `origin/<trunk>` does not resolve, or the usual setup failures (no `upstream` remote, the trunk not on origin, an origin whose push URL is the original project, an unreadable `.forkflow.toml`) | fix what the message names and rerun. A trunk checked out in another worktree means the command the message names, in that worktree - it sees the same pending records. "Not yet" means wait for the merge; "commits origin lacks" means somebody committed on the local trunk by hand - show them `git log origin/<trunk>..<trunk>` and let them decide, the plugin never moves that trunk over its own commits |

## Notes

- One record per branch: ships of different branches - from different worktrees, at the same
  time - each keep their own, and only a second `ship` of the same branch replaces its record.
  `land` clears a record only while it is still the one it landed. `forkflow status` shows every
  record on a `pending` line, with the same landed / not-landed verdict `land` would give.
- `ship --merge` and `sync --merge` (a fork with `merge = "self"`) run this same landing in the
  same process right after the merge; a merged request whose catch-up could not run exits 2 with
  a message that opens with "the merge request was merged" - then `forkflow land` finishes it
  once the reason is dealt with. A tool that answered "merged" while nothing reached the trunk
  (a merge train, auto-merge) is exit 6 instead, whichever worktree has the trunk: not merged
  yet - once it is, the `forkflow land <branch>` the message names, in the worktree it names.
- Never `git push origin <trunk>`, never rebase the trunk, never `--force` anything: `land`
  fast-forwards only, and a trunk it cannot fast-forward is a message, not a `reset`.
- `.forkflow.toml` (`gate`, `merge`, branch names) needs Python 3.11+; a present but unreadable
  config is exit 2 for every subcommand rather than a guessed branch name.
