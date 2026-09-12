# ai-thingz

Claude Code skills by [cloud-simple](https://github.com/cloud-simple), each packaged as its own plugin
in the `ai-thingz` plugin marketplace.

| skill | what it does |
|---|---|
| [`nocomment`](#nocomment) | turns the current feature branch into a code-only, comment-free review branch `nocomment/<branch>` |
| [`forkflow`](#forkflow) | works a fork of a moving project: a pristine mirror of upstream, a protected MR-only trunk, and `status` / `sync` / `ship` / `land` / `setup` |

## Install

From inside Claude Code:

```
/plugin marketplace add cloud-simple/ai-thingz
/plugin install nocomment@ai-thingz
/plugin install forkflow@ai-thingz
```

Or link a single skill as a plain user skill, no plugin machinery:

```bash
ln -s "$PWD/plugins/nocomment/skills/nocomment" ~/.claude/skills/nocomment
```

That symlink install does **not** apply to `forkflow`: its five skills reach their script and their
rules through `${CLAUDE_PLUGIN_ROOT}`, which only exists when the plugin is installed as a plugin.

Layout:

```
.claude-plugin/marketplace.json                marketplace "ai-thingz", lists every plugin
plugins/<plugin>/.claude-plugin/plugin.json    one directory per plugin, named after it
plugins/<plugin>/skills/<skill>/SKILL.md       the plugin's skills
plugins/<plugin>/skills/<skill>/scripts/       a skill's tooling
plugins/<plugin>/scripts/                      tooling shared by all of a plugin's skills
plugins/<plugin>/references/                   material the skills read
tests/                                         python -m unittest discover -s tests
```

`tests/` no longer covers every plugin: `forkflow.py` carries its own suite and runs it with
`--test` (see [Tests](#tests-1)).

---

## nocomment

```
docs/ldap-domino-signin   ──▶   nocomment/docs/ldap-domino-signin
21 commits, 11 files             1 commit on the merge-base, 6 files
```

- documentation files are left out entirely (`*.md`, `*.example`, `docs/`, README/LICENSE/...);
- in every other file, the comments the branch **added** are removed, trailing comments on changed
  lines are cut, and the comments the branch **deleted** are put back - so
  `git diff main..nocomment/<branch>` contains code changes and nothing else;
- comments that existed before the branch and were not touched stay exactly as they were;
- the result is written through a temporary worktree: your checkout is never modified.

The branch is a review artifact, not something to merge. The script needs only Python 3.9+ and git.

### Use

In Claude Code, on the feature branch: *"nocomment"*, *"make a code-only branch"*, *"strip the
comments and docs from this branch"*. The skill dry-runs first, shows what will be included and
excluded, then creates the branch and reports.

The report's CODE table shows, per file, what the transform did to the lines the branch touched:

| column | meaning |
|---|---|
| `code_added` | added lines kept (the code the branch actually adds) |
| `comments_removed` | added comment-only lines removed |
| `trailing_removed` | added lines whose trailing comment was cut |
| `comments_restored` | deleted comment-only lines restored from the base |

Directly:

```bash
python3 plugins/nocomment/skills/nocomment/scripts/nocomment.py --dry-run   # preview
python3 plugins/nocomment/skills/nocomment/scripts/nocomment.py             # create nocomment/<branch>
git diff main..nocomment/<branch>
```

| flag | effect |
|---|---|
| `--dry-run` | report only, create nothing |
| `--force` | replace an existing `nocomment/<branch>` |
| `--base REF` | default branch to diff against (default `origin/HEAD`, else `main`/`master`) |
| `--prefix P` | branch-name prefix (default `nocomment/`) |
| `--include GLOB` | treat matching files as code (repeatable) |
| `--exclude GLOB` | treat matching files as documentation (repeatable) |
| `--keep-docstrings` | leave Python docstrings alone |
| `--keep-directives` | keep tool directives: `# noqa`, `# type:`, `//go:build`, `eslint-disable`, `tflint-ignore`, ... |
| `-C DIR` | run against another checkout |

Globs are `fnmatch` patterns tested against the repo-relative path and the basename. The same
options can be committed as `.nocomment.toml` at the repo root:

```toml
prefix = "nocomment/"
include = ["*.yml.example"]
exclude = ["tests/fixtures/*"]
keep_docstrings = false
keep_directives = false
```

Exit codes: `0` done or dry run · `2` precondition failed (on the default branch, detached HEAD,
target exists without `--force`, ...) · `3` nothing left to commit once comments are gone.

### What counts as a comment

| language | files | stripped |
|---|---|---|
| Python | `.py` | `#` comments (via `tokenize`), docstrings (via `ast`; one that is a body's only statement is kept). Shebang and `coding:` line kept |
| YAML | `.yml` `.yaml` | `#` at line start or after whitespace; quoted scalars respected |
| Jinja | `.j2` `.jinja` `.jinja2` | `{# … #}` / `{#- … -#}` blocks, plus whole-line comments of the inner language for `x.rb.j2`, `x.yml.j2`, `x.sh.j2`, … |
| shell, Ruby, Perl, PHP | `.sh` `.bash` `.rb` `.pl` `.php` … | `#` (and `//` for PHP), heredoc bodies untouched, Ruby `=begin/=end`, POD; magic comments kept |
| HCL / Terraform | `.tf` `.tfvars` `.hcl` | `#`, `//`, `/* */`, heredocs untouched |
| Go, JS/TS, Java, C/C++, Rust, Kotlin, … | usual extensions | `//`, `/* */`; raw strings and template literals respected |
| TOML, SQL, Lua, R, PowerShell | | `#` / `--` / `<# #>` with quoting rules |
| XML / HTML / SVG / Vue | | `<!-- -->` |
| INI / `.conf`, Dockerfile, Makefile, `.env`, `.gitignore` | | whole-line comments only (`# syntax=` kept) |
| JSON, lock files | | nothing (no comment syntax) |

Files with an unknown extension and no recognisable shebang are included verbatim and flagged in
the report. Binary files and symlinks are copied as-is.

Blank lines that end up adjacent to each other because a comment between them was removed are
collapsed; other blank lines the branch added are kept, which is why a YAML block that was
"comment, key, blank, comment, key" comes out as "key, blank, key".

### Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers each language scanner, the diff-aware merge (dropped / restored / reverted lines, blank
collapsing, CRLF and missing-final-newline preservation), classification and overrides, and the
git flow end to end on throwaway repositories (dry run, creation, refusal on the default branch,
`--force`, exit 3, `.nocomment.toml`).

---

## forkflow

Develop in a fork (`origin`) of a project that keeps moving (`upstream`) without ever committing on
the mirror of upstream or pushing the fork's trunk directly. Two remotes, two long-lived branches -
the names are configurable, the layout is not:

```
upstream/main ────●───────●───────●          theirs; read-only (push URL disabled)
                   \
main (mirror) ──────●───────●───────●        pristine copy of upstream/main,
                                     \       fast-forward only, pushed to origin
develop (trunk) ──────────────────────●──●──●   upstream + our work; protected, MR-only
```

- **mirror** (named after upstream's own branch by default - `main` here, `master` on a
  master-based upstream) is never committed on. `sync` fast-forwards it to the upstream
  branch and pushes it, so teammates and CI can see "theirs vs ours" with `git diff main..develop`
  without configuring the `upstream` remote themselves.
- **trunk** (`develop` by default) carries every feature and every upstream sync and is reached only
  through merge requests. Features arrive as one squashed commit, syncs as one merge commit - so
  `git log --merges develop` is the record of when upstream was taken.

The trunk is protected and published, which makes every history rewrite on it a force-push plus a
protection change. So every push in the script goes through three helpers - `push()` refuses the
trunk and the mirror, `push_mirror()` can only fast-forward the mirror to upstream,
`bootstrap_trunk()` runs only when the trunk does not exist yet - and there is no rebase of the
trunk anywhere. The mistake is impossible, not merely discouraged.

### The six hard rules

1. **Never push to `upstream`** - its push URL is set to `DISABLED` and the pre-push hook refuses
   that remote by name *and* by URL, in any spelling: it normalises both sides before comparing,
   so a trailing `/`, a `file://` prefix, a `user@`, a default port, an added or dropped `.git`
   and a differently-cased host are all the same repository - and a local path is canonicalised
   as well, so `../upstream.git`, a `/.` suffix, a symlink to it and `file://localhost/...`
   (`localhost` in any case) all fold onto one repository. A path itself keeps its case, which a
   host does not: on a case-insensitive filesystem `/srv/UPSTREAM.git` reaches the same
   repository as `/srv/upstream.git` and is **not** recognised as it, so use one spelling of a
   local upstream. The rule is about the repository, not
   the remote name: an `origin` whose `pushurl` points at the original project is refused by every
   subcommand.
2. **Never push the trunk** - only merge requests move `origin/<trunk>`; `push()` and the hook both
   refuse it, deletion included.
3. **Never rebase the trunk** - upstream comes in by merge only.
4. **Force-push only a feature branch**, only with `--force-with-lease`, and only after a backup
   branch is confirmed on `origin`.
5. **Sync MRs are merged as merges; ship MRs fast-forward.** GitLab: merge the sync MR (the sync
   branch's tip *is* the merge commit), `merge_method=ff` for ship MRs. GitHub: "Create a merge
   commit" for sync, "Rebase and merge" for ship - which rewrites the SHA, so the local feature
   branch is deleted afterwards, never reused (`forkflow land` recognises the patch and deletes
   it). Never squash or rebase a sync MR: that rewrites upstream's SHAs out of
   the trunk's ancestry and every later sync re-conflicts on the same hunks.
6. **Never commit on the mirror** - it is only ever fast-forwarded to the upstream branch and pushed,
   never with force. The hook rejects a mirror push that is not an ancestor of the last-fetched
   upstream ref, and rejects it when that ref is missing.

Sequence rule: **sync first, then ship; rebase locally, merge globally.** The full text the skills
read is [`plugins/forkflow/references/rules.md`](plugins/forkflow/references/rules.md).

### The five skills

| skill | say | what it does |
|---|---|---|
| `/forkflow:status` | *"where are we vs upstream"*, *"how much has this fork diverged"* | one screen: mirror, trunk, `origin/*` and upstream, divergence and how much of it is upstream-tracked, what the current branch touches, whether the fork is set up, and whether the last ship or sync is still waiting to land. Read-only |
| `/forkflow:sync` | *"sync upstream"*, *"pull in upstream"* | advance and push the mirror, then bring it into the trunk through `sync/<upstream>-<date>` + one `--no-ff` merge + an MR. Conflicts are resolved in the branch; a "both sides survived" table flags every file changed on both sides, because a clean merge is not automatically a correct one |
| `/forkflow:ship` | *"ship this branch"*, *"squash and open the MR"* | take a feature branch to the trunk as **one** squashed commit (tree-hash verified) on top of a fresh `origin/<trunk>`, through an MR |
| `/forkflow:land` | *"forkflow land"*, *"the MR merged"*, *"catch develop up"* | the closing step once the MR is merged: fetch, verify that the pushed commit is on `origin/<trunk>` (by ancestry, or by patch for a ship whose SHA a "squash and merge" or "rebase and merge" rewrote), fast-forward the local trunk, delete the landed local branch (only while its tip is still the commit that was pushed), leave you on the trunk. Not merged yet is exit 2, not an error |
| `/forkflow:setup` | *"set up the fork"*, *"protect the trunk locally"* | make the rules mechanical: upstream push URL disabled, pre-push hook, ff-only merge config, trunk bootstrapped on a fresh fork, and a report of the platform's default branch / merge method / protection with the exact command to fix each mismatch |

The script does the mechanical, testable work - divergence numbers, mirror advance, merge simulation,
backups, merges, squash and tree-hash check, invariant checks, pushes, the MR command, the opt-in
merge and the landing. Claude does the judgment: conflict resolution and MR wording. Nothing about
it is specific to any one fork.

Directly:

```bash
S=plugins/forkflow/scripts/forkflow.py
python3 $S status [--fetch] [--offline]
python3 $S check
python3 $S sync [--continue] [--mr] [--merge] [--title T]
python3 $S ship [--continue] [--mr] [--merge] [--title T] [--message-file F]
python3 $S land [BRANCH] [--force]
python3 $S setup [--upstream NAME] [--upstream-url URL] [--trunk NAME] [--mirror NAME]
```

`-C DIR`, `--dry-run` and `--force` are accepted on every subcommand and may be given before or
after it; `--force` only does something in `sync` (recreate the sync branch), `setup` (replace a
foreign pre-push hook) and `land` (fast-forward a landing the tool cannot verify).
`check` is the preflight `sync` and `ship` run themselves (upstream-tracked warning, configured gate
commands, "is this branch on the trunk's tip"); it has no skill of its own - `status` surfaces it for
humans. `--dry-run` writes nothing at all: no branch, commit, push, config or hook, no mirror or
trunk moved, no branch deleted, no pending record cleared - and nothing in the git directory either.
It does not fetch, because a fetch rewrites `FETCH_HEAD`, moves `refs/remotes/*` and brings objects
in; it asks `git ls-remote` instead, which is one round trip that writes none of those, and the
`fetch` line prints what each remote has beside what this clone has. The merge simulation still runs
(`git merge-tree --write-tree` with the objects it makes sent to a scratch directory), so a dry run
still previews the pending merge and its conflicts. What it cannot do is judge a landing that has
not reached this clone's refs: `land --dry-run` says so in those words and names the run without
`--dry-run` that fetches and decides, rather than reporting "not merged" about a merged request.

`status` on a fork whose feature branch touches a file upstream also owns:

```
forkflow status  origin=git@gitlab.example.com:team/fork.git  upstream=https://example.org/project.git  platform=gitlab
  as of last fetch: 4m ago
  mirror  main 7ced8d72   origin/main 7ced8d72 (=)   upstream/main 59c508e3 (mirror behind by 1)
  trunk   develop 9d369dc7   origin/develop 9d369dc7 (=)   upstream/main (+1/-1 vs origin/develop)
  divergence: 2 files, 1 upstream-tracked
  branch   feat/retention  not on origin  tree: clean
  touches upstream-tracked files (WARNING, 1):
    shared.tf
  backups  0 (refs/remotes/origin/backup/*)
  setup    upstream push: DISABLED   pre-push hook: installed   ff-only: develop yes, main yes
```

Editing an upstream-tracked file is a permanent merge cost but sometimes necessary, so it is a
warning and never a refusal.

### Configuration

`.forkflow.toml` at the repo root, all keys optional - `setup` drops in a commented template:

```toml
upstream = "upstream"        # remote name of the original project
upstream_branch = "main"     # its branch we track (default: upstream's HEAD)
mirror = "main"              # our fast-forward-only copy of it (default: upstream_branch)
trunk = "develop"            # protected, MR-only branch carrying our work
gate = []                    # e.g. ["make test", "terraform fmt -check -recursive"]
merge = "manual"             # "self": this fork's MRs are merged by whoever opened them - enables --merge
sync_prefix = "sync/"
backup_prefix = "backup/"
```

`merge` is the fork's one-time declaration of who merges its merge requests. `"manual"` (the
default) means somebody reviews and presses the button, and `--merge` is refused with exit 2 before
anything is pushed; `"self"` means whoever opened the MR merges it, which lets `ship --merge` and
`sync --merge` do so. Config *and* flag are needed - either alone does nothing - so a reviewed fork
can never be merged by accident. `--merge` also needs an origin URL that names a GitLab or GitHub
project the merge command can address with `--repo`; one that does not is the same exit 2, before
anything is pushed. Unlike every other key, `merge` is read only where the original project
cannot write it: the `.forkflow.toml` this fork committed on `origin/<trunk>`, or - while none
is committed there - an untracked `.forkflow.toml` in the working tree, where `setup` leaves it.
The checked-out branch's own copy is never read for it: a sync branch carries upstream's file, so
upstream's `merge = "self"` can never switch the gate off, and a config upstream wrote onto the
trunk (a trunk bootstrapped from an upstream that tracks the file) counts as no declaration at all.
Whose file it is is decided by its bytes, against every `.forkflow.toml` the original project has
ever had - the whole history of every remote-tracking ref that is not `origin`'s, plus the mirror,
not just their tips, so a version upstream has since retired is still upstream's. Any edit of your
own makes it yours. The refs walked are deliberately not the ones the config names: a config
choosing the evidence against itself is no check at all. And the walk fails CLOSED - a clone that
cannot prove what the original project has had gets exit 2 for `--merge` alone, with the condition
named: shallow (`git fetch --unshallow <upstream>` ends it), partial (`--filter`), history rewritten
by `refs/replace/*` or `info/grafts`, nothing of upstream's fetched, or an object that cannot be
read. Everything else still works; the way past it is the ordinary one, `--mr` and a person merging.
It is asked again right before the merge command runs; a fork that no longer says `"self"` by then
(a teammate's commit the run's fetch brought in) gets exit 6 with the MR open. So the ship that
first commits the config is `--mr`, merged by hand: once committed on a branch it is neither
untracked nor on the trunk yet.

`gate` is the one key that is *run* rather than read - `sh -c` in the repo root, on every `check`,
`sync` and `ship` - and `.forkflow.toml` is a tracked file a sync is designed to bring in from the
original project. So a sync that changes it says so with a `CHECK` line pointing at
`git diff <merge>^1 HEAD -- .forkflow.toml`, a run whose own merge changed the `gate` prints the
commands that arrived instead of obeying them (`gate - NOT RUN`), and a `check` whose gate comes
from a file upstream also tracks says that out loud. Read the diff before merging such a sync MR.
A `gate` the merge left alone still runs: the guard is keyed on the commands, not on the file.

`setup` leaves `.forkflow.toml` untracked, so a sync that brings upstream's copy of it in would
have to write over it - which git refuses. `sync` says so and names the file before the backup
and the sync branch, so there is nothing to clean up. Do not delete it - it is this fork's
config: commit it on a branch and `ship` it, and the next sync meets upstream's copy as a tracked
file, in the open. A `.forkflow.toml` kept out of `git status` with `info/exclude` is one git
*would* write over without a word - it counts ignored files as expendable - so `sync` looks for
it in the working tree itself and refuses that collision the same way. On a case-insensitive
filesystem (the macOS and Windows default) upstream's `.ForkFlow.toml` is the same file as
`.forkflow.toml`, and `sync` treats it so. forkflow reads its config only from a file named
exactly `.forkflow.toml` and refuses a case variant; the way out it prints never deletes or
renames the variant in the working tree while the fork has a `.forkflow.toml` of its own - on
such a filesystem that would take the fork's config with it - but puts the fork's copy back from
`HEAD` through the index. Every command it prints first copies the file as it is into the git
directory and names the copy, so an edit made before running it is not lost. On the trunk, the
mirror or a detached HEAD it prints no command that commits - nothing is committed there: run
forkflow on a branch off `origin/<trunk>`, where the fix it names is committed and shipped.

The script itself needs only Python 3.9+ and git 2.20+ (the merge simulation wants 2.38+ and is
skipped with a note on older git). **Reading `.forkflow.toml` needs Python 3.11+** (`tomllib`): a
config file that is present and configures something is exit 2 for every subcommand on an older
Python, because it carries the safety-critical branch names and must never be silently defaulted.
Without a config file, 3.9+ is enough - so on an older Python `setup` writes no template (it says
so) and a file of nothing but comments is read as no config at all.

### Exit codes

| code | meaning |
|---|---|
| `0` | done, dry run, or nothing to do (already in sync; nothing to ship). Also `--mr` when the tool is missing or fails, without `--merge` - the branch is pushed and the command is printed |
| `1` | `--test` had failures |
| `2` | precondition: dirty tree, detached HEAD, missing remote, unfetched upstream, unreadable `.forkflow.toml`, mirror diverged from upstream, trunk missing on origin, foreign pre-push hook, `--merge` on a fork whose config does not say `merge = "self"` (or whose origin names no project to merge on), `land` with nothing pending or with an MR that is not merged yet, ... |
| `3` | invariant checked by `check`: a gate command failed, or the branch is not on the trunk's tip |
| `4` | conflicts - resolve them, then rerun with `--continue` |
| `5` | rewrite safety: backup not confirmed on origin, tree hash differs after the squash, push rejected |
| `6` | `--merge` only: the merge request was not created by this run (or one was already open for the branch) or not merged (tool missing or failing, the head-commit guard refused because the branch moved, or the tool answered "merged" while nothing reached the trunk) - the branch is pushed and the MR, when created, is open; merge it by hand, then `forkflow land` |
| `130` | interrupted |

### After the MR is merged

The plugin moves the local trunk only by fast-forward, through `land`, and leaves you on it. Once
the MR has merged:

```bash
python3 $S land
```

`land` finishes what a `ship` or `sync` recorded - every run that pushed a branch recorded what is
waiting to land, one record per branch, so it works in any later session, and `status` shows each
record on a `pending` line with the same verdict. The records are shared by every worktree of the
clone: ships made in linked worktrees land from the worktree that has the trunk checked out, and
`land` elsewhere names that worktree (and the command to run there) before it fetches anything.
`land <branch>` lands that branch's record; plain `land` lands the current branch's, or, run from a
branch with no record (the trunk, usually), every record that has landed - the rest are listed and
kept. It fetches; verifies that the recorded commit is on
`origin/<trunk>` - by ancestry, or for a ship by patch, so a "squash and merge" or GitHub's "Rebase
and merge" that gave the commit a new SHA is recognised too; checks the local trunk out (creating it
from `origin/<trunk>` in a single-branch clone) and fast-forwards it with `git merge --ff-only`;
deletes the landed local branch - only while its tip is still the commit that was pushed: a commit
made on it after the ship landed nowhere, so the branch is kept and the run says so; forgets the
record. A local trunk carrying commits origin lacks is
refused untouched - the plugin never creates those. "Not on `origin/<trunk>` yet" is exit 2 and not
an error: the MR is not merged, run it again once it is. A ship that landed as a merge commit lands
with a WARNING - rule 5 asks for a fast-forward, and the shape is judged on the trunk's first-parent
line.

On a solo fork - one whose `.forkflow.toml` says `merge = "self"` - `ship --merge` and `sync
--merge` are the shortcut: open the MR, merge it, land it, in one run. The merge is the method rule
5 requires - on GitLab the project's own merge method (`glab mr merge ... --auto-merge=false`,
which `setup`'s report insists is `ff`), on GitHub `gh pr merge --merge` for a sync and `--rebase`
for a ship - always with a head-commit guard (`--sha` / `--match-head-commit`), so only the exact
commit the run pushed can be merged. It merges only the MR this run opened: one already open for
the branch (its `create` fails) is not merged for you. A merge that does not happen is exit 6 with
the branch pushed and the MR open: merge by hand, then `forkflow land` - and so is a tool that
answers "merged" while nothing reaches the trunk (a merge train, auto-merge, a required pipeline),
judged before anything about worktrees: its message names `forkflow land <branch>` and, when the
trunk is checked out elsewhere, the worktree to run it in once the merge is through.
A merge that happened but whose catch-up could not run is exit 2 with a message that opens with
"the merge request was merged". In the usual worktree layout - the trunk checked out in the main
worktree, the work in a linked one - that is how a `--merge` from the linked worktree ends: the
trunk can only be fast-forwarded where it is checked out, so the message names `forkflow land
<branch>` to run in the main worktree, and the branch stays until the linked worktree lets go of it.
Every `forkflow sync` / `forkflow ship` a `--merge` (or `--mr`) run prints - the resume after a
conflict (`forkflow sync --continue --merge`), a `--force` redo, a plain rerun - carries the flag,
so following it to the letter still merges.

`land --force` is the escape for a landing the tool cannot see - an MR closed without merging, a
sync squashed or rebased in the UI (a broken rule 5, which `land` says out loud), a recorded commit
that is no longer in this clone (its branch deleted and pruned): it fast-forwards the local trunk
to whatever `origin/<trunk>` holds, **keeps** the branch, clears the record, and says the landing
was not verified. When the landing *is* verified, `--force` changes nothing: the branch is deleted
as usual. It acts on one record: with several pending and none of their branches checked out it
needs the branch named (`land --force <branch>`).

After a GitHub "Rebase and merge" of a ship MR the local feature branch is deleted by `land` (its
SHA was rewritten; the patch is recognised, and the branch's tip is still the commit that was
pushed), but the remote branch survives on GitHub, for a ship and a sync alike - `land` prints the
`git push origin --delete <branch>` line and leaves that call to you. On GitLab, where
`--remove-source-branch` removed it, `land` drops the stale `origin/<branch>` ref this clone kept
(under `--force` too), so the name can be shipped again - and `ship` itself drops one it finds
before it pushes, when origin answers that the branch is gone (a ship made after a merge in the UI
but before `land`), then pushes the branch as a new one without a lease.

### Adopting forkflow in an existing fork

`setup` bootstraps, it never migrates. A fork whose `main` already carries its own work is refused
with a pointer here, because fixing it rewrites a published branch - done once, by hand, by a
Maintainer:

1. Take `backup/<YYYYMMDD-HHMMSS>-pre-adoption` of the old `main` and push it.
2. Create `develop` from `main` and push it.
3. Make `develop` the platform default branch and protect it - `setup`'s platform report prints the
   exact `glab api` / `gh api` command for each mismatch it finds.
4. Make `main` a pure copy of `upstream/main`: either force-push it with protection temporarily
   allowing force-pushes, or unprotect, delete and recreate `main` from `upstream/main` and
   re-protect it.
5. Run `/forkflow:setup` and commit `.forkflow.toml`.

On GitLab there is a variant with no rewrite of your own: once `main` has been reset, configure
project **pull mirroring** of the upstream repository into `main` and let the platform keep it
current.

A project applying this recipe should keep its own step-by-step document, with its real branch names
and SHAs, with that project - not in this plugin.

### Tests

```bash
python3 plugins/forkflow/scripts/forkflow.py --test
```

The suite lives inside `forkflow.py` and needs no network and no `glab`/`gh`: it builds throwaway
`upstream.git` / `origin.git` / work-clone triples, exercises every subcommand end to end, produces
push rejections with a real `pre-receive` hook and stale leases with a narrowed refspec, drives the
installed pre-push hook with real `git push` invocations, and fakes the platform CLIs on `PATH` -
including one that merges the way the platform would and refuses a head-commit guard that does not
match, so `--merge` and `land` are exercised end to end. A source-invariant test parses the script
with `ast`, maps every line to the function it sits in, and asserts that the git argument `push`
appears only in the three push helpers, `update-ref` only in `advance_mirror`, `merge --ff-only`
only in `advance_mirror` and `land_trunk` (the plugin's two fast-forwards: the mirror advance and
the trunk landing), `rebase` only in `rebase_onto`, `--force-with-lease` only in `push()`, that the
only functions starting a subprocess are the git wrappers, `shell` (the gate), `run_tool` (the
platform CLI for `--mr` and `--merge`) and `api_get`, that no git call anywhere passes `--force`
or `--no-verify`, and that no string in the code spells a `forkflow sync` / `forkflow ship`
command: each printed one is built by `rerun_cmd`, which carries the run's `--merge` / `--mr`.

### Versions

The version in `plugins/forkflow/.claude-plugin/plugin.json` moves whenever a change alters what a
subcommand does, so an installed copy can be told apart from `main` (`/plugin` shows it, and the
marketplace cache directory is named after it). What each one carries:

| version | what changed |
|---|---|
| `0.2.0` | `land` subcommand: once the MR is merged, fetch, verify that the pushed commit is on `origin/<trunk>` (by ancestry, or by patch for a ship whose SHA was rewritten), fast-forward the local trunk, delete the landed branch (only while its tip is still the pushed commit) and leave you on the trunk - the printed shell catch-up line is retired for `next: forkflow land`; `--merge` on `sync` and `ship`, behind `merge = "self"` in `.forkflow.toml`, merges the MR the run opened with the method rule 5 requires and a head-commit guard, then lands it; exit `6` for a merge request not created or not merged; `status` shows every `pending` ship or sync (one record per branch, shared by the worktrees of a clone) and whether it has landed |
| `0.1.1` | `status` asks the upstream server whether its branch moved instead of trusting the last fetch (`--offline` opts out); `--mr` passes `--yes` to `glab` and runs the tool with stdin closed, so it no longer stops at a confirmation prompt. Earlier fixes that shipped under `0.1.0` and are worth knowing about: every `gh`/`glab` command names the fork with `--repo` rather than resolving to the original project; the sync gate guard compares committed config to committed config; `file://localhost/` spellings of the upstream URL are refused by the hook |
| `0.1.0` | first release |
