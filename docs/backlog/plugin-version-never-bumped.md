---
worth: yes
where: plugins/forkflow/.claude-plugin/plugin.json
added: 2026-09-09
---
# plugin.json stays at 0.1.0 across behaviour-changing fixes

PRs #5 and #6 changed what `sync`, `setup` and `--mr` do (gate guard, the `file://localhost`
bypass, every gh/glab command re-targeted from upstream to the fork) and the manifest still says
`0.1.0`. The installed copy in `~/.claude/plugins/cache/ai-thingz/forkflow/0.1.0` happens to
match `main` today only because it was installed after #6; a user who installed a day earlier is
running the version that opens merge requests on the original project and has no signal to
update. Bump the version in the PR that changes behaviour, and say in the README which version
carries which rule fix.
