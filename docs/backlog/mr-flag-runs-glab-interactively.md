---
worth: yes
where: plugins/forkflow/scripts/forkflow.py:1283
added: 2026-09-09
---
# --mr runs glab mr create without --yes, so it prompts

`mr_command` builds `glab mr create --repo ... --title ... --description-file ...` with no
`--yes`; glab still asks for confirmation before creating, and under `--mr` that prompt has no
terminal to answer it. In the GET session `--mr` was never used for any of the eight merge
requests: the assistant ran the printed command by hand every time, "plus --yes for a
non-interactive run". The flag the design put there to close the loop was not usable.

Add `--yes` for glab (gh with `--title`/`--body-file` is already non-interactive), run the tool
with stdin from `/dev/null` so any remaining prompt fails fast rather than hanging, and have the
fake-tool test assert the flag. Worth checking at the same time that `--mr` surfaces the MR URL
prominently and records it in the state file, since the closing step ("after the MR is merged")
needs it.
