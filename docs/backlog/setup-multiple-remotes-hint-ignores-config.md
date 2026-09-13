---
worth: yes
where: plugins/forkflow/scripts/forkflow.py:655
added: 2026-09-09
---
# "several non-origin remotes" exit 2 without naming the setting that ends it

With `origin`, `origin-deprecated` and `upstream` configured, every subcommand exits 2 with
`several non-origin remotes (origin-deprecated, upstream); run forkflow setup --upstream-url <URL>
(or --upstream <NAME>)`. The resolver falls back to "the only other remote" and there is more than
one, so it refuses - correctly - but the hint names only the `setup` flags, never the setting that
answers the question for good.

Since 0.3.0 that setting is `git config forkflow.upstream <NAME>`, in this clone's own
`.git/config`; `setup --upstream <NAME>` writes it, but a user who has already run `setup` and then
added a second remote has no reason to guess that rerunning `setup` is what persists it. Name the
`git config` line in the hint alongside the flags - it is one line the user can paste, and it is the
key every later run depends on.

Seen on 2026-09-08, when the tree still held a `.forkflow.toml` whose keys were all commented out,
so the file answered nothing either. That file no longer configures anything at all.
