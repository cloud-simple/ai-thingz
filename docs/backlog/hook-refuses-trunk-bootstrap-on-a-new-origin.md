---
worth: yes
where: plugins/forkflow/scripts/forkflow.py:2842
added: 2026-09-09
---
# the installed hook refuses the trunk bootstrap when origin is re-pointed

The hook's rule-2 case refuses every push of `refs/heads/<trunk>`, creation included. `setup`
orders its steps so the bootstrap push runs before the hook is installed - correct on a fresh
clone, but not on a clone that already ran `setup` once. On 2026-09-08 the GET fork's `origin` was
renamed to `origin-deprecated` and a new empty repository added as `origin`; `setup` then had to
bootstrap `develop` on the new origin, and the hook from the previous `setup` refused it. The
session worked around it by moving the hook aside by hand for one run ("the hook predates the new
origin, so set it aside") and letting `setup` reinstall it.

Two ways to make that unnecessary: let the hook allow a trunk push only when the remote sha is all
zeros and the local sha equals the last-fetched upstream ref (a pure bootstrap carries nothing of
ours, which is exactly the condition `bootstrap_trunk` already enforces in Python), or have
`bootstrap_trunk` bypass its own marked hook for that single push - it already tells an
`installed` hook from a `foreign` one. The first keeps the hook self-contained; the second is the
smaller change. Either needs a test that re-points origin at an empty bare repo after a first
`setup` and runs `setup` again without touching `.git/hooks`.
