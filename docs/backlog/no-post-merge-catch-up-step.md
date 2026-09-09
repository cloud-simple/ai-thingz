---
worth: later
added: 2026-09-09
---
# after the MR merges, catching the local trunk up is manual every time

Every ship and sync ends with the printed line `after the MR is merged: git fetch origin && git
switch develop && git merge --ff-only origin/develop`, and in the GET session that step was
typed out by the user each time ("please merge MR 7, then fetch origin, switch to develop, and
merge origin/develop to it"). Twice the user also asked Claude to merge the MR itself, which it
did through `glab api PUT .../merge` - correct on GitLab with `merge_method=ff`, since a sync
branch's tip is already the merge commit, but nothing in the tool checked that.

Two candidate additions, which are separate decisions:

- a `land` (or `catch-up`) subcommand, or a `status` hint, for the local side: when
  `origin/<trunk>` is ahead of the local trunk, print or run the ff-only merge. Pure fast-forward
  of a local branch; no rule is touched.
- an opt-in `--merge` that merges the MR through the platform API with the method rule 5
  requires (merge commit for a sync, fast-forward for a ship). This one is the unresolved value
  question: the skills currently say "you never merge that MR yourself", and an API merge is the
  one action that makes the trunk move without a human on the button. Decide whether the tool
  should do it on explicit request, or whether the SKILL.md should instead spell out how Claude
  merges it correctly when asked.
