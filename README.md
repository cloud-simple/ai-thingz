# ai-thingz

Claude Code skills by [cloud-simple](https://github.com/cloud-simple), each packaged as its own plugin
in the `ai-thingz` plugin marketplace.

| skill | what it does |
|---|---|
| [`nocomment`](#nocomment) | turns the current feature branch into a code-only, comment-free review branch `nocomment/<branch>` |

## Install

From inside Claude Code:

```
/plugin marketplace add cloud-simple/ai-thingz
/plugin install nocomment@ai-thingz
```

Or link a single skill as a plain user skill, no plugin machinery:

```bash
ln -s "$PWD/plugins/nocomment/skills/nocomment" ~/.claude/skills/nocomment
```

Layout:

```
.claude-plugin/marketplace.json                marketplace "ai-thingz", lists every plugin
plugins/<plugin>/.claude-plugin/plugin.json    one directory per plugin, named after it
plugins/<plugin>/skills/<skill>/SKILL.md       the plugin's skills
plugins/<plugin>/skills/<skill>/scripts/       a skill's tooling
tests/                                         python -m unittest discover -s tests
```

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
