---
worth: yes
where: plugins/forkflow/scripts/forkflow.py:1342
added: 2026-09-09
---
# status reports a stale upstream as in-sync, with no warning

`status` is read-only by design: without `--fetch` it makes no network call and prints the numbers
from the last fetch, marking them only with `as of last fetch: 6h ago`. That age line is the whole
signal, and in practice nobody acts on it. In the first real fork (GET, 2026-09-07..09) `status`
printed `upstream/main (=)` twice while upstream had already moved - at "22h ago" on 09-08 and at
"6h ago" on 09-09 - and the second one led straight to wrong advice ("upstream has not moved since
the last sync") before a `--fetch` showed the mirror 4 commits behind. The user noticed the gap
himself: "the upstream remote is updated ... but we didn't fetch it. does status validate updates?"

A stale answer to "where are we vs upstream" is worse than a slow one; the question is the only
reason to run the command. Options, cheapest first:

- keep the no-writes contract but drop the no-network one: `git ls-remote <upstream> <branch>`
  writes nothing, and comparing the server tip with `refs/remotes/<upstream>/<branch>` lets the
  header say `upstream/main 1115e838 as fetched, 76a07382 on the server (4 not fetched)` instead
  of `(=)`. Add `--offline` for the no-network case.
- failing that, a loud line whenever the age is above a threshold: `stale: fetched 6h ago, run
  status --fetch before trusting these numbers`.
- `skills/status/SKILL.md` should tell Claude to pass `--fetch` unless the user asked for an
  offline answer, to state the fetch age in its summary, and never to conclude "upstream has not
  moved" from an un-fetched status.

The plan chose "no network without --fetch" deliberately, so this reverses a recorded decision;
the GET session is the evidence that the decision was wrong.
