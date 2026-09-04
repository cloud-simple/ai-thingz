# forkflow rules

Read this before running `sync`, `ship` or `setup`. The script enforces these mechanically; you
must not work around them by driving git by hand.

## Layout

Two remotes, two long-lived branches. The names are configurable (`.forkflow.toml`), the layout
is not.

```
upstream/main ────●───────●───────●          theirs; read-only (push URL disabled)
                   \
main (mirror) ──────●───────●───────●        pristine copy of upstream/main,
                                     \       fast-forward only, pushed to origin
develop (trunk) ──────────────────────●──●──●   upstream + our work; protected, MR-only
```

- **mirror** (named after upstream's own branch by default: `main` here, `master` on a
  master-based upstream) is never committed on. It is only fast-forwarded to the upstream
  branch and pushed, so anyone can see "theirs vs ours" with `git diff main..develop` without
  configuring the `upstream` remote.
- **trunk** (`develop` by default) carries every feature and every upstream sync, and is reached
  only through merge requests that fast-forward it. Features arrive as one squashed commit; syncs
  arrive as one merge commit, so `git log --merges develop` is the record of when upstream was
  taken.

## The six hard rules

1. **Never push to `upstream`.** Its push URL is set to `DISABLED` and the pre-push hook refuses
   the remote by name *and* by URL. The rule is about the repository, not the remote name: an
   `origin` whose `pushurl` points at the original project is refused by every subcommand
   (`git config --unset-all remote.origin.pushurl`).
2. **Never push the trunk.** Only merge requests move `origin/<trunk>`. `push()` refuses it and
   the hook refuses it, deletion included.
3. **Never rebase the trunk.** Upstream comes in by merge only.
4. **Force-push only a feature branch**, only with `--force-with-lease`, and only after a backup
   branch has been confirmed on `origin`.
5. **Sync MRs are merged as merges; ship MRs fast-forward.**
   - sync - GitLab: merge (fast-forward of the sync branch, whose tip is the merge commit);
     GitHub: "Create a merge commit". Never squash or rebase a sync MR: that rewrites upstream's
     SHAs out of the trunk's ancestry, and every later sync re-conflicts on the same hunks.
   - ship - GitLab: fast-forward (`merge_method=ff`); GitHub: "Rebase and merge". GitHub rewrites
     the commit SHA, so **delete the local feature branch afterwards** instead of reusing it.
6. **Never commit on the mirror.** It is only ever fast-forwarded to the upstream branch and
   pushed by `sync`, never with force. The hook rejects a mirror push that is not an ancestor of
   the last-fetched upstream ref, and rejects it when that ref is missing or unfetched.

## Sequence rule

**Sync first, then ship; rebase locally, merge globally.**

Take upstream into the trunk before shipping a feature, so the feature is rebased onto a trunk
that already contains upstream, and the conflicts are resolved once, locally, on a branch.

## Platform note

A sync merge request that has gone stale (the trunk moved under it) is **redone** with
`forkflow sync` - a new sync branch off the current trunk. Never press "Rebase" in the web UI and
never squash it: both rewrite the upstream commits the sync exists to preserve. Close the stale
MR and open the new one.

Server-side settings - default branch, merge method / merge options, branch protection - are
project-wide. `forkflow setup` reports them and prints the exact command to fix each mismatch; it
never changes them itself. A Maintainer runs the command.

## What is never automated

A fork whose mirror branch already carries its own work is a migration, not a setup: it rewrites
a published branch and is done once, by hand, by a Maintainer. `setup` refuses it and points at
the section *Adopting forkflow in an existing fork* of the project README
(github.com/cloud-simple/ai-thingz, section `forkflow`), which carries the five-step recipe.
Never reset or force-push that branch on the user's behalf.

## The gate is arbitrary shell

`gate = [...]` in `.forkflow.toml` is run with `sh -c` by `check`, `sync` and `ship`. That file is
tracked and committed, so a sync can bring a `gate` in from upstream: read the diff of
`.forkflow.toml` in a sync merge request before the next `sync`/`ship` runs it. A dry run never
executes a gate.
