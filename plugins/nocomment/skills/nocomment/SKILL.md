---
name: nocomment
description: Create a code-only, comment-free review branch `nocomment/<branch>` from the current feature branch - documentation files left out, comments the branch added stripped, comments it deleted restored - so `git diff <default>..nocomment/<branch>` shows code changes only. Use when the user says "nocomment", "code-only branch", "branch without comments", "strip the comments/docs from this branch", "show me just the code changes", or wants to review or hand over the pure code delta of a branch.
allowed-tools: Bash, Read, AskUserQuestion
---

# nocomment

Builds `nocomment/<current-branch>` as ONE commit on top of the merge-base with the default branch,
through a temporary git worktree. The user's checkout, index and branch are never touched. The
work is done by a deterministic script; this skill drives it and reports honestly.

Script: `${CLAUDE_PLUGIN_ROOT}/skills/nocomment/scripts/nocomment.py` (Python 3.9+, stdlib only, git 2.20+).

## What the script does

1. Resolves the default branch (`--base`, else `origin/HEAD`, else `main`/`master`), the merge-base,
   and the target name `<prefix><branch>` (prefix defaults to `nocomment/`).
2. Classifies every file changed between merge-base and HEAD:
   - **documentation** (left out entirely): `*.md`, `*.rst`, `*.txt`, `*.adoc`, `*.example`, `*.sample`,
     anything under `docs/`/`doc/`, and README/LICENSE/CHANGELOG/CLAUDE.md/AGENTS.md-style names;
   - **code** (everything else), including YAML/HCL/Jinja/shell/config files.
   `--include GLOB` / `--exclude GLOB` (repeatable, fnmatch on the repo-relative path or basename)
   and a committed `.nocomment.toml` (`include`, `exclude`, `prefix`, `base`, `keep_docstrings`,
   `keep_directives`) override the defaults.
3. For each code file, undoes the branch's **comment changes** relative to the merge-base:
   comment lines the branch added are removed, trailing comments on changed lines are cut,
   comment lines the branch deleted are put back, and a changed line that differs only by its
   trailing comment is reverted. Pre-existing untouched comments stay as they are. Blank lines
   left stranded between removed comments are collapsed. Language rules: Python (`#`, docstrings
   via the parser; shebang and coding line kept), YAML, Jinja `{# #}` plus the template's inner
   language for `*.rb.j2`/`*.yml.j2`/..., shell/Ruby/HCL/Perl/PHP (heredoc-aware), Go, C-like
   (`//`, `/* */`), TOML, SQL, Lua, XML/HTML, INI/Dockerfile/Makefile (whole-line comments only).
   Binary files, symlinks and unknown languages are copied verbatim and flagged.
4. Writes the result into a temp worktree, commits with `--no-verify`, removes the worktree, and
   prints a per-file table (`code_added`, `comments_removed`, `trailing_removed`,
   `comments_restored`) plus a self-check: any comment line
   still present in the resulting diff is reported as a WARNING.

Exit codes: 0 done / dry run, 2 precondition failed (default branch checked out, detached HEAD,
target exists without `--force`, ...), 3 nothing to commit once comments are gone.

## Process

1. **Preflight** - run `git symbolic-ref --short HEAD` and `git status --porcelain`. The user must be
   on the feature branch. Uncommitted changes are fine (they are simply not part of the branch);
   say so if the tree is dirty.
2. **Dry run** - `python3 "${CLAUDE_PLUGIN_ROOT}/skills/nocomment/scripts/nocomment.py" --dry-run`
   plus any flags the request implies. Read the CODE/EXCLUDED tables.
3. **Check the classification** against what the user asked for. Translate hints into flags
   (`--include '*.example'`, `--exclude 'tests/*'`, `--keep-docstrings`, `--keep-directives`,
   `--base <ref>`, `--prefix <p>`). If a code file shows `copied verbatim (unknown language)`, tell
   the user which one and why. Ask only when a file's fate is genuinely ambiguous and would change
   the result; otherwise proceed with the defaults.
4. **Create** - rerun without `--dry-run`. Use `--force` only when the target already exists and the
   user asked to regenerate it (or the branch is one this skill made earlier in the session).
5. **Report** - branch name and commit, the per-file table, what was excluded and why, any WARNING
   or verbatim copies, and the review command `git diff <default>..nocomment/<branch>`.
   Then stop: never push, never check the new branch out, never delete it.

## Notes

- The branch is a review artifact (one squashed commit, docs removed, comments removed) - it is not
  meant to be merged. Say so if the user seems to intend otherwise.
- Docstrings count as comments (parser-based; a docstring that is a function's entire body is kept
  so the code still parses). `--keep-docstrings` disables that. Tool directives (`# noqa`,
  `# type:`, `//go:build`, `eslint-disable`, `tflint-ignore`, ...) are stripped unless
  `--keep-directives`; shebangs and interpreter magic comments are always kept.
- Uncommitted work is not included. `.nocomment.toml` is read from the repo root of the checkout.
- Known limits: regex literals containing `//` in JS/TS, and `#` inside unusual YAML scalars, are
  heuristics - the self-check warning and the `comments_restored`/`trailing_removed` counts are
  there to notice surprises.
