---
name: status
description: "Report where a fork stands against the project it was forked from: mirror, trunk, origin and upstream, how far the fork has diverged and on how many upstream-tracked files, and what the current branch touches. Use when the user says \"forkflow status\", \"where are we vs upstream\", \"how far behind upstream are we\", \"how much has this fork diverged\", \"is the fork set up\", or asks what state the fork is in before a sync or a ship."
allowed-tools: Bash, Read
---

# forkflow status

Read-only. The script never pushes or rebases the trunk and never commits on the mirror - and
neither may you: do not push, rebase, reset or merge anything while answering a status question.
Without `--fetch` this makes no network call and writes nothing.

Script: `${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py` (Python 3.9+, stdlib only; `.forkflow.toml`
needs 3.11+). The layout and the rules it enforces: `${CLAUDE_PLUGIN_ROOT}/references/rules.md`.

## Process

1. **Run it.**

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/forkflow.py" status
   ```

   Add `--fetch` when the user asks for *current* numbers ("are we behind right now?", "did
   anything land upstream?") or is about to sync; without it the numbers are as of the last fetch,
   which the header states. Add `-C DIR` when the repository is not the working directory.

2. **Read the output.** Header first (mirror line, trunk line, divergence), then the branch line,
   the upstream-tracked WARNING list, backups, and the setup line.

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
  as of last fetch: <relative age, or "never">
  mirror  <mirror> <sha|->  origin/<mirror> <sha|-> (=|unpushed n)  upstream/<ub> <sha|unfetched> (=|mirror behind by n|DIVERGED)
  trunk   <trunk> <sha|->   origin/<trunk> <sha|missing> (=|+n/-m)  upstream/<ub> (+n/-m vs origin/<trunk>)
  divergence: <N> files, <M> upstream-tracked
```

| what you see | what it means |
|---|---|
| `-` in a branch column | no local copy of that branch (single-branch clone) - not a problem |
| `unpushed n` on the mirror | the local mirror is ahead of `origin/<mirror>`; `sync` pushes it |
| `mirror behind by n` | upstream has moved; `sync` fast-forwards the mirror and takes it into the trunk |
| `DIVERGED` | the mirror has commits of its own - it is not a mirror; a manual migration, never reset by the plugin |
| `unfetched` | `upstream/<branch>` is not in this clone yet; rerun with `--fetch` |
| `missing` on the trunk | the trunk is not on origin; `setup` bootstraps it on a fresh fork |
| `divergence: N files, M upstream-tracked` | how big the fork is, and how much of it will cost merge work forever - the M files are the whole conflict surface |
| branch `n unpushed` / `not on origin` | the current branch against `origin/<branch>` |
| `touches upstream-tracked files (WARNING, m)` | this branch edits files upstream also owns; a warning, never a blocker |
| `backups  n (...)` | backup branches on origin, newest three; read from remote-tracking refs, no network |
| `setup    upstream push: DISABLED\|<url> (LIVE)  pre-push hook: installed\|missing\|foreign  ff-only: ...` | which guarantees are actually in place in this clone |

`status` degrades rather than fails: a missing trunk on origin, a diverged mirror, an unfetched
upstream, a single-branch clone and a detached HEAD are all reported with exit 0. The one failure
is a missing `upstream` remote - exit 2 with the `forkflow setup --upstream-url <URL>` hint; offer
`/forkflow:setup` then.

## Notes

- `forkflow check` (same script, `check` subcommand) is the read-only preflight `sync` and `ship`
  run: the upstream-tracked warning, the configured `gate` commands, and "is this branch on
  `origin/<trunk>`'s tip". Run it when the user asks whether a branch is ready to ship; exit 3
  means an invariant failed and the output names it.
- Report the numbers you actually saw. Never estimate divergence or "behind by" counts from
  memory of an earlier run in the session - rerun the command.
