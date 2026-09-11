---
name: status
description: "Report where a fork stands against the project it was forked from: mirror, trunk, origin and upstream, how far the fork has diverged and on how many upstream-tracked files, and what the current branch touches. Use when the user says \"forkflow status\", \"where are we vs upstream\", \"how far behind upstream are we\", \"how much has this fork diverged\", \"is the fork set up\", or asks what state the fork is in before a sync or a ship."
allowed-tools: Bash, Read
---

# forkflow status

Read-only. The script never pushes or rebases the trunk and never commits on the mirror - and
neither may you: do not push, rebase, reset or merge anything while answering a status question.
It writes nothing. Without `--fetch` the numbers are as of the last fetch, and one `ls-remote`
asks the upstream server whether its branch has moved since - that is the only network call, and
`--offline` skips it for a clone with no route to the server.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py` (Python 3.9+, stdlib only; `.forkflow.toml`
needs 3.11+). The layout and the rules it enforces: `${CLAUDE_PLUGIN_ROOT}/references/rules.md`.

## Process

1. **Run it.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" status
   ```

   Add `--fetch` when the user asks for *current* numbers ("are we behind right now?", "did
   anything land upstream?"), is about to sync, or when the `upstream` line says the server has
   moved and the user wants to know by how much - without a fetch the script can say *that*
   upstream moved but not by how many commits. Add `--offline` only when the user says the
   server is unreachable. Add `-C DIR` when the repository is not the working directory.

   **Never conclude "upstream has not moved" from a run that did not ask the server.** The
   `upstream` line is the answer to that question; the `as of last fetch` age is not - it names
   which remotes the last fetch reached, and after a `ship` that is often `origin only`.

2. **Read the output.** Header first (mirror line, trunk line, divergence), then the branch line,
   the upstream-tracked WARNING list, backups, the setup line, and - when a ship or a sync is
   waiting to land - the `pending` line. The record behind it is shared by every worktree of the
   clone, so a ship made in a linked worktree shows here too.

3. **Explain, don't just paste.** Say in plain words where the fork stands: how far the trunk is
   ahead of upstream and behind it, whether the mirror is behind upstream or unpushed, whether the
   current branch is on the trunk's tip, and what the numbers imply for the next step - `sync`
   when upstream has moved, `ship` when the branch has commits to land. Sync first, then ship.

4. **Surface the setup gaps.** If the script prints a `hint  run \`forkflow setup\`` line (trunk
   not on origin, hook `missing` or `foreign`, upstream push URL `LIVE`, ff-only config not set),
   say which guarantee is missing and offer `/forkflow:setup`. A `DIVERGED` mirror is a migration,
   not a setup: point at the README section *Adopting forkflow in an existing fork* and stop.

## Reading the output

Header:

```
forkflow status  origin=<url>  upstream=<url>  platform=<gitlab|github|unknown>
  as of last fetch: <relative age, or "never"> (<remotes it reached, e.g. "origin only - upstream not in it">)
  mirror  <mirror> <sha|->  origin/<mirror> <sha|-> (=|unpushed n|behind n|-)  upstream/<ub> <sha|unfetched> (=|mirror behind by n|DIVERGED|no mirror|unfetched|?)[ as fetched, server moved]
  trunk   <trunk> <sha|->   origin/<trunk> <sha|missing> (=|+n/-m|missing|-)  upstream/<ub> (+n/-m vs origin/<trunk>|vs origin/<trunk>: unknown)
  divergence: <N> files, <M> upstream-tracked        (or: unknown (fetch upstream and create the trunk first))
  upstream  $ git ls-remote --heads upstream refs/heads/<ub>  -> server at <sha> = fetched | server at <sha>, fetched <sha> - upstream moved since the last fetch: ... | server not reachable (...) | not asked (--offline)
```

After the `setup` line, only while the most recent `ship` or `sync` has not been landed:

```
  pending  <ship|sync> <branch> -> MR <url|-> - not on origin/<trunk> yet | landed: run forkflow land | cannot verify here
```

| what you see | what it means |
|---|---|
| `-` in a branch column | no local copy of that branch (single-branch clone) - not a problem |
| `unpushed n` on the mirror | the local mirror is ahead of `origin/<mirror>`; `sync` pushes it |
| `mirror behind by n` | upstream has moved; `sync` fast-forwards the mirror and takes it into the trunk |
| `DIVERGED` | the mirror has commits of its own - it is not a mirror; a manual migration, never reset by the plugin |
| `unfetched` | `upstream/<branch>` is not in this clone yet; rerun with `--fetch` |
| `as fetched, server moved` on the mirror | the `(=)` or `behind by n` before it is true of the *last fetch*, and the server has moved since; the `upstream` line has the server's sha. `--fetch` for the count, `sync` to take it |
| `upstream  ... server at <sha> = fetched` | the fetched ref is current; the numbers above can be trusted as of now |
| `upstream  ... server not reachable` | the one network call failed; the numbers are as of the last fetch and nothing more is known |
| `missing` on the trunk | the trunk is not on origin; `setup` bootstraps it on a fresh fork |
| `behind n` on the mirror | `origin/<mirror>` is ahead of the local one (a teammate synced); fetch and fast-forward |
| `no mirror` | neither a local nor an `origin/` copy of the mirror exists yet; run `setup` |
| `?` or `unknown` | the two refs cannot be compared (one of them is missing); fetch, then read it again |
| `divergence: N files, M upstream-tracked` | how big the fork is, and how much of it will cost merge work forever - the M files are the whole conflict surface |
| branch `n unpushed` / `not on origin` | the current branch against `origin/<branch>` |
| `touches upstream-tracked files (WARNING, m)` | this branch edits files upstream also owns; a warning, never a blocker |
| `backups  n (...)` | backup branches on origin, newest three; read from remote-tracking refs, no network |
| `setup    upstream push: DISABLED\|<url> (LIVE)  pre-push hook: installed\|missing\|foreign  ff-only: ...` | which guarantees are actually in place in this clone |
| `pending  ... - not on origin/<trunk> yet` | the branch the last `ship`/`sync` pushed is not merged yet (as of the refs on disk - `--fetch` to ask again); wait, or offer to check the MR |
| `pending  ... - landed: run forkflow land` | the merge request is merged and the local trunk has not caught up: offer `/forkflow:land` |
| `pending  ... - cannot verify here` | `origin/<trunk>` or the pushed commit is not in this clone (a fresh fork, or a clone other than the one that ran the ship); not an error - `land` in that clone, or after a fetch |

`status` degrades rather than fails: a missing trunk on origin, a diverged mirror, an unfetched
upstream, a single-branch clone, a detached HEAD and a pending record it cannot judge are all
reported with exit 0. The `pending` verdict is git-only (ancestry, or the patch for a ship), so it
is the same under `--offline`; `--fetch` moves the refs it is read from. The one failure
is a missing `upstream` remote - exit 2 with the `forkflow setup --upstream-url <URL>` hint; offer
`/forkflow:setup` then.

## Notes

- `forkflow check` (same script, `check` subcommand) is the read-only preflight `sync` and `ship`
  run: the upstream-tracked warning, the configured `gate` commands, and "is this branch on
  `origin/<trunk>`'s tip". Run it when the user asks whether a branch is ready to ship; exit 3
  means an invariant failed and the output names it.
- Report the numbers you actually saw. Never estimate divergence or "behind by" counts from
  memory of an earlier run in the session - rerun the command.
