---
worth: later
where: plugins/forkflow/scripts/forkflow.py:1374
added: 2026-09-09
---
# backup branches accumulate and nothing prunes them

Every ship and sync creates a `backup/<ts>-<reason>` branch and pushes it, by design. After three
days on the GET fork there were 14 local and 7 origin backups, and `status` lists only the newest
three with a count. Nothing distinguishes a backup whose protected tip has since landed on the
trunk (safe to drop) from one guarding an MR still open.

A `backups` subcommand that lists each with its age, reason and whether its tip is now an ancestor
of `origin/<trunk>`, plus `--prune` for the ones that are, would close it. What is unresolved is
the retention policy: whether "landed" alone is enough to drop a restore point, or whether the
user wants an age floor as well. The hook allows `backup/*` deletion, so no rule stands in the way.
