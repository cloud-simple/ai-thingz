# forkflow rules

Read this before running `sync`, `ship`, `land` or `setup`. The script enforces these
mechanically; you must not work around them by driving git by hand.

## Layout

Two remotes, two long-lived branches. The names are configurable (`git config forkflow.mirror`,
`forkflow.trunk`, `forkflow.upstream`, `forkflow.upstreamBranch`), the layout is not. Those
settings live in `.git/config`, which is in no tree, so they are per clone: a teammate cloning
the fork runs `forkflow setup` before anything works, and they do not travel with the
repository.

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
   the remote by name *and* by URL - in any spelling, because it normalises both sides before
   comparing (trailing `/`, `file://`, `user@`, a default port, `.git`, host case; a local path
   is canonicalised too, so `..`, a `/.` suffix, a symlink and `file://localhost/...` all fold
   onto one repository - but a path keeps its case where a host does not, so a differently-cased
   spelling of a local path is not recognised as the same repository). The rule is
   about the repository, not the remote name: an `origin` whose `pushurl` points at the original
   project is refused by every subcommand (`git config --unset-all remote.origin.pushurl`), the
   same normalisation deciding what "the original project" is.
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
     the commit SHA, so the local feature branch is **deleted afterwards**, never reused - `land`
     recognises the patch and deletes it.
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

Every one of those commands names the fork's own project - `repos/<owner>/<repo>` for `gh`,
`projects/<group%2Fsubgroup%2Fproject>` for `glab` - and never gh's `{owner}/{repo}` or glab's
`:fullpath`. Those two placeholders are resolved by the tool from the repository it is run in, and
both answer with the remote named `upstream` when there is one - which in a forkflow fork is always
the original project. A command carrying one would read upstream's settings and, run by somebody
who is a Maintainer there too, write them: rule 1, undone by a pasted fix. Do not "simplify" a
printed command back to a placeholder.

The merge request command that `sync` and `ship` print names the fork the same way, with
`--repo <the fork's URL>`. Without it `gh pr create` and `glab mr create` work the repository out
from the remotes and answer with `upstream` as well - so a command missing the flag opens the merge
request **on the original project**, and `--mr` opens it there itself rather than merely advising
it. `glab mr create` also carries `--head <the fork's URL>`: glab opens the request on the project
the source branch lives in and works that one out from the remotes too, so `--repo` alone still
posted it to the original project on a real fork. The URL carries the host as well as the project, because a bare `owner/repo` is resolved
against the tool's own default host (github.com, gitlab.com) and not the one the fork is on. Pass
the command on exactly as printed. When the origin URL names no project at all, forkflow prints no
command and says which URL it could not address: open that merge request in the web UI.

`--merge` (on `sync` and `ship`, only in a clone whose `git config --local forkflow.merge` says
`self` - read from local scope and from nowhere else, so a `--global` setting cannot arm it, and
`.git/config` is in no tree, so no sync can write it) merges with the method the project is
configured for on GitLab - which `setup`'s report insists is `ff` - and
with the method rule 5 requires per call on GitHub (`--merge` for a sync, `--rebase` for a ship),
always with a head-commit guard (`--sha` / `--match-head-commit`) so only the exact commit the
run pushed can be merged; `land` warns when what landed does not have that shape (a ship's commit
reachable only through a merge commit's second parent, not on the trunk's first-parent line).

## What is never automated

A fork whose mirror branch already carries its own work is a migration, not a setup: it rewrites
a published branch and is done once, by hand, by a Maintainer. `setup` refuses it and points at
the section *Adopting forkflow in an existing fork* of the project README
(github.com/cloud-simple/ai-thingz, section `forkflow`), which carries the five-step recipe.
Never reset or force-push that branch on the user's behalf.

## The gate is arbitrary shell

`forkflow.gate` is run with `sh -c` by `check`, `sync` and `ship`, in the order the commands were
added, stopping at the first one that fails. It is whatever shell the clone was told to run, run
unattended, so treat adding one as the code review it is - `setup` prints the line and never runs
it for you. A dry run prints the gate and never executes it.

It is multi-valued: `git config --add forkflow.gate '<command>'` appends one more command, while
plain `git config forkflow.gate '<command>'` REPLACES every command there is.

The gate lives in `.git/config`, which is in no tree, so a sync cannot bring one in from the
original project - that is why the settings moved there. It is also why a fork's gate is per
clone and reaches no teammate: each clone adds its own.
