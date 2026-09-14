---
worth: yes
where: plugins/forkflow/scripts/forkflow.py:2121
added: 2026-09-14
---
# glab mr create posts to the upstream project despite --repo naming the fork

On the GET fork, running forkflow 0.3.0 with glab 1.116.0, `sync --merge` pushed the sync branch and
then ran the command `mr_command` builds:

```
glab mr create --repo ssh://gitlab.com/treestyle/suite/ibagroup/get --source-branch sync/upstream-20260914 --target-branch develop --title 'sync: upstream/main 20260914 (8 commits)' --description-file <file> --remove-source-branch --yes
```

glab named the right project and then posted to the original one:

```
Creating merge request for sync/upstream-20260914 into develop in treestyle/suite/ibagroup/get
ERROR
Post https://gitlab.com/api/v4/projects/gitlab-org%2Fgitlab-environment-toolkit/merge_requests: 403 {message: 403 Forbidden}.
```

The run exited 6 with the branch pushed and no merge request. Every GitLab `ship --mr`, `ship --merge`,
`sync --mr` and `sync --merge` goes through this one command, and `setup` adds the original project as a
remote named `upstream` by default, so this hits every GitLab fork forkflow manages.

**Likely cause, unconfirmed.** GitLab's create endpoint is `POST /projects/:id/merge_requests` on the
SOURCE project. glab used upstream as the source, or head, repository while `--repo` set only the target.
Unless `-H/--head` is given, glab resolves the head from the clone's remotes, and this clone has no
`glab-resolved` setting steering it, so glab picked the `upstream` remote over `origin` on its own. So
`--repo` alone is not enough in a clone with an `upstream` remote. Confirm on a real fork that the same
command with `--head <fork URL>` opens the request on the fork before building on it.

**What worked.** The request was opened as !9 through the API, which no remote can redirect:

```
glab api --method POST projects/treestyle%2Fsuite%2Fibagroup%2Fget/merge_requests -f source_branch=sync/upstream-20260914 -f target_branch=develop -f title='...'
```

**Two fix shapes.** Add `--head <fork>` beside `--repo` in the `glab mr create` argv, which is cheapest if
it holds on a real fork. Or open the request through `glab api` against the fork's project path, which
`mr_target` already computes, and which sidesteps remote resolution entirely.

**Why the 500 tests passed.** The fake `glab` asserts argv and cannot reproduce glab's internal head
resolution. This is the real-fork validation gap left open when PR #10 landed. A regression test can pin
whichever flag or API call the fix uses, but only a real fork proves it.

**Unknown.** `glab mr merge <branch> --repo <fork> --sha <head>` has never run for real. In the same session
the Claude Code auto-mode classifier blocked it as a merge without review, which is a permission setting on
the user's side and not a plugin defect. Whether `mr merge` has the same head-resolution problem is
unverified. Nothing in this evidence implicates the GitHub path.
