---
worth: yes
where: plugins/forkflow/scripts/forkflow.py:307
added: 2026-09-09
---
# "several non-origin remotes" exit 2 even though .forkflow.toml is present

With `origin`, `origin-deprecated` and `upstream` configured, every subcommand exits 2 with
`several non-origin remotes (origin-deprecated, upstream); run forkflow setup --upstream-url <URL>
(or --upstream <NAME>)` - and did so on 2026-09-08 with a `.forkflow.toml` sitting in the tree.
The file was the template `setup` writes, whose keys are all commented out, so `upstream` was
never set and the resolver fell back to "the only other remote". The hint does not mention the
config key at all, and `--upstream NAME` is only persisted when given to `setup`, so the fix the
user needs (`upstream = "upstream"` in `.forkflow.toml`, committed) is the one thing the message
does not say. Name it in the hint, and consider having the template write `upstream` uncommented
when `setup` resolved it - that key is the one every later run depends on.
