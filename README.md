# ai-thingz

Claude Code skills by [cloud-simple](https://github.com/cloud-simple), each packaged as its own plugin
in the `ai-thingz` plugin marketplace.

| skill | what it does |
|---|---|
| [`nocomment`](#nocomment) | turns the current feature branch into a code-only, comment-free review branch `nocomment/<branch>` |
| [`forkflow`](#forkflow) | works a fork of a moving project: a pristine mirror of upstream, a protected MR-only trunk, and `status` / `sync` / `ship` / `setup` |

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

That symlink install does **not** apply to `forkflow`: its four skills reach their script and their
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
   that remote by name *and* by URL.
2. **Never push the trunk** - only merge requests move `origin/<trunk>`; `push()` and the hook both
   refuse it, deletion included.
3. **Never rebase the trunk** - upstream comes in by merge only.
4. **Force-push only a feature branch**, only with `--force-with-lease`, and only after a backup
   branch is confirmed on `origin`.
5. **Sync MRs are merged as merges; ship MRs fast-forward.** GitLab: merge the sync MR (the sync
   branch's tip *is* the merge commit), `merge_method=ff` for ship MRs. GitHub: "Create a merge
   commit" for sync, "Rebase and merge" for ship - which rewrites the SHA, so delete the local
   feature branch afterwards. Never squash or rebase a sync MR: that rewrites upstream's SHAs out of
   the trunk's ancestry and every later sync re-conflicts on the same hunks.
6. **Never commit on the mirror** - it is only ever fast-forwarded to the upstream branch and pushed,
   never with force. The hook rejects a mirror push that is not an ancestor of the last-fetched
   upstream ref, and rejects it when that ref is missing.

Sequence rule: **sync first, then ship; rebase locally, merge globally.** The full text the skills
read is [`plugins/forkflow/references/rules.md`](plugins/forkflow/references/rules.md).

### The four skills

| skill | say | what it does |
|---|---|---|
| `/forkflow:status` | *"where are we vs upstream"*, *"how much has this fork diverged"* | one screen: mirror, trunk, `origin/*` and upstream, divergence and how much of it is upstream-tracked, what the current branch touches, whether the fork is set up. Read-only |
| `/forkflow:sync` | *"sync upstream"*, *"pull in upstream"* | advance and push the mirror, then bring it into the trunk through `sync/<upstream>-<date>` + one `--no-ff` merge + an MR. Conflicts are resolved in the branch; a "both sides survived" table flags every file changed on both sides, because a clean merge is not automatically a correct one |
| `/forkflow:ship` | *"ship this branch"*, *"squash and open the MR"* | take a feature branch to the trunk as **one** squashed commit (tree-hash verified) on top of a fresh `origin/<trunk>`, through an MR |
| `/forkflow:setup` | *"set up the fork"*, *"protect the trunk locally"* | make the rules mechanical: upstream push URL disabled, pre-push hook, ff-only merge config, trunk bootstrapped on a fresh fork, and a report of the platform's default branch / merge method / protection with the exact command to fix each mismatch |

The script does the mechanical, testable work - divergence numbers, mirror advance, merge simulation,
backups, merges, squash and tree-hash check, invariant checks, pushes, the MR command. Claude does
the judgment: conflict resolution and MR wording. Nothing about it is specific to any one fork.

Directly:

```bash
S=plugins/forkflow/scripts/forkflow.py
python3 $S status [--fetch]
python3 $S check
python3 $S sync [--continue] [--mr] [--title T]
python3 $S ship [--continue] [--mr] [--title T] [--message-file F]
python3 $S setup [--upstream NAME] [--upstream-url URL] [--trunk NAME] [--mirror NAME]
```

`-C DIR`, `--dry-run` and `--force` are accepted on every subcommand and may be given before or
after it; `--force` only does something in `sync` (recreate the sync branch) and `setup` (replace a
foreign pre-push hook).
`check` is the preflight `sync` and `ship` run themselves (upstream-tracked warning, configured gate
commands, "is this branch on the trunk's tip"); it has no skill of its own - `status` surfaces it for
humans. `--dry-run` creates no branch, commit, push, config or hook and moves no mirror, and still
previews the merge that is pending. It is not read-only: it fetches (that is how it knows what is
pending), so `refs/remotes/*` and `FETCH_HEAD` are refreshed and the merge simulation writes a tree
object - nothing that changes a branch, a worktree or a setting.

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
sync_prefix = "sync/"
backup_prefix = "backup/"
```

The script itself needs only Python 3.9+ and git 2.20+ (the merge simulation wants 2.38+ and is
skipped with a note on older git). **Reading `.forkflow.toml` needs Python 3.11+** (`tomllib`): a
config file that is present but cannot be read is exit 2 for every subcommand, because it carries
the safety-critical branch names and must never be silently defaulted. Without a config file, 3.9+
is enough.

### Exit codes

| code | meaning |
|---|---|
| `0` | done, dry run, or nothing to do (already in sync; nothing to ship). Also `--mr` when the tool is missing or fails - the branch is pushed and the command is printed |
| `1` | `--test` had failures |
| `2` | precondition: dirty tree, detached HEAD, missing remote, unfetched upstream, unreadable `.forkflow.toml`, mirror diverged from upstream, trunk missing on origin, foreign pre-push hook, ... |
| `3` | invariant checked by `check`: a gate command failed, or the branch is not on the trunk's tip |
| `4` | conflicts - resolve them, then rerun with `--continue` |
| `5` | rewrite safety: backup not confirmed on origin, tree hash differs after the squash, push rejected |
| `130` | interrupted |

### After the MR is merged

The plugin never moves the local trunk. Once the MR has merged, catch up by hand - `setup`'s
`--ff-only` config guarantees this cannot silently become a merge commit:

```bash
git fetch origin && git switch develop && git merge --ff-only origin/develop
```

After a GitHub "Rebase and merge" of a ship MR, also delete the local feature branch: its commit SHA
was rewritten.

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
installed pre-push hook with real `git push` invocations, and fakes the platform CLIs on `PATH`. A
source-invariant test parses the script with `ast`, maps every line to the function it sits in, and
asserts that the git argument `push` appears only in the three push helpers, `update-ref` and
`merge --ff-only` only in `advance_mirror`, `rebase` only in `rebase_onto`, `--force-with-lease` only
in `push()`, and that no git call anywhere passes `--force` or `--no-verify`.
