#!/usr/bin/env python3
"""nocomment - build a code-only, comment-free branch from the current feature branch.

Given a checkout of a non-default branch, create ``<prefix><branch>`` (default
prefix ``nocomment/``) whose diff against the default branch contains ONLY
code changes:

* documentation files (Markdown, ``*.example``, ``docs/``, README/LICENSE/...)
  are left out entirely;
* in every remaining file, comments the branch ADDED are removed and comments
  the branch DELETED are put back, so the resulting diff carries no comment
  lines in either direction. Trailing comments on changed code lines are
  stripped too. Comments that existed before the branch and were not touched
  are left exactly as they were.

The new branch is one commit on top of the merge-base, written through a
temporary git worktree, so the current checkout is never modified.

Exit status: 0 done (or dry-run), 2 precondition failed, 3 nothing to commit.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import io
import os
import re
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

CONFIG_FILE = ".nocomment.toml"
DEFAULT_PREFIX = "nocomment/"
DEFAULT_BRANCH_CANDIDATES = ("main", "master", "trunk", "develop")


# --------------------------------------------------------------------------- #
# language specifications
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Str:
    """A string-literal delimiter the scanner must skip over."""
    delim: str
    escapes: bool = True        # backslash escapes the delimiter
    multiline: bool = False     # may span lines
    doubled: bool = False       # the delimiter escapes itself by doubling ('' in YAML/SQL/TOML)


@dataclass(frozen=True)
class Lang:
    name: str
    line: tuple = ()                    # line-comment markers, e.g. ("#",) or ("//",)
    blocks: tuple = ()                  # (open, close) block-comment markers
    strings: tuple = ()                 # Str(...) entries, longest delimiter first
    needs_space: bool = False           # line marker only at line start or after whitespace
    full_line_only: bool = False        # never strip trailing comments, only whole-line ones
    heredoc: Optional[str] = None       # regex with a ``term`` group, matched mid-line
    line_blocks: tuple = ()             # (open_regex, close_regex) for line-anchored blocks
    string_at_value_start: bool = False  # YAML: a quote only opens a string at a value start
    keep: tuple = ()                    # regexes: comments always kept (interpreter directives)
    directives: tuple = ()              # regexes: comments kept only with --keep-directives
    python: bool = False                # use tokenize/ast instead of the generic scanner


_SHEBANG = r"^#!"
_PY_CODING = r"^#.*coding[:=]\s*[-\w.]+"
_RB_MAGIC = r"^#\s*-\*-|^#\s*(frozen_string_literal|encoding|warn_indent|shareable_constant_value)\s*:"

HASH_DIRECTIVES = (
    r"#\s*noqa\b", r"#\s*type:\s", r"#\s*pragma\b", r"#\s*pylint:", r"#\s*fmt:\s",
    r"#\s*ruff:", r"#\s*isort:", r"#\s*mypy:", r"#\s*flake8:", r"#\s*rubocop:",
    r"#\s*shellcheck\b", r"#\s*yamllint\b", r"#\s*tflint-ignore", r"#\s*checkov:",
    r"#\s*trivy:", r"#\s*nosec\b", r"#\s*ts:skip", r"#\s*ansible-lint\b",
)
SLASH_DIRECTIVES = (
    r"//\s*go:", r"//\s*\+build\b", r"//\s*nolint\b", r"//\s*eslint", r"/\*\s*eslint",
    r"//\s*@ts-", r"//\s*prettier-ignore", r"//\s*NOSONAR\b", r"//\s*#\s*sourceMappingURL",
    r"//\s*biome-ignore", r"//\s*istanbul\b",
)

PYTHON = Lang(
    "python", line=("#",),
    strings=(Str('"""', True, True), Str("'''", True, True), Str('"'), Str("'")),
    keep=(_SHEBANG, _PY_CODING), directives=HASH_DIRECTIVES, python=True,
)
YAML = Lang(
    "yaml", line=("#",), strings=(Str('"'), Str("'", escapes=False, doubled=True)),
    needs_space=True, string_at_value_start=True, keep=(_SHEBANG,), directives=HASH_DIRECTIVES,
)
SHELL = Lang(
    "shell", line=("#",), strings=(Str('"'), Str("'", escapes=False)), needs_space=True,
    heredoc=r"<<(?!<)-?\s*(?P<q>['\"]?)(?P<term>[A-Za-z_][A-Za-z0-9_]*)(?P=q)",
    keep=(_SHEBANG,), directives=HASH_DIRECTIVES,
)
RUBY = Lang(
    "ruby", line=("#",), strings=(Str('"'), Str("'")), needs_space=True,
    heredoc=r"<<[-~]?(?P<q>['\"]?)(?P<term>[A-Z_][A-Z0-9_]*)(?P=q)",
    line_blocks=((r"^=begin\b", r"^=end\b"),),
    keep=(_SHEBANG, _RB_MAGIC), directives=HASH_DIRECTIVES,
)
HCL = Lang(
    "hcl", line=("#", "//"), blocks=(("/*", "*/"),), strings=(Str('"'),),
    heredoc=r"<<-?(?P<term>[A-Za-z_][A-Za-z0-9_]*)", directives=HASH_DIRECTIVES + SLASH_DIRECTIVES,
)
GO = Lang(
    "go", line=("//",), blocks=(("/*", "*/"),),
    strings=(Str("`", escapes=False, multiline=True), Str('"'), Str("'")),
    directives=SLASH_DIRECTIVES,
)
CLIKE = Lang(
    "c-like", line=("//",), blocks=(("/*", "*/"),),
    strings=(Str("`", multiline=True), Str('"'), Str("'")),
    keep=(_SHEBANG,), directives=SLASH_DIRECTIVES,
)
PHP = Lang(
    "php", line=("//", "#"), blocks=(("/*", "*/"),), strings=(Str('"'), Str("'")),
    heredoc=r"<<<(?P<q>['\"]?)(?P<term>[A-Za-z_][A-Za-z0-9_]*)(?P=q)",
    keep=(_SHEBANG,), directives=SLASH_DIRECTIVES + HASH_DIRECTIVES,
)
CSS = Lang("css", blocks=(("/*", "*/"),), strings=(Str('"'), Str("'")))
SQL = Lang("sql", line=("--",), blocks=(("/*", "*/"),), strings=(Str("'", escapes=False, doubled=True), Str('"')))
LUA = Lang("lua", line=("--",), blocks=(("--[[", "]]"),), strings=(Str('"'), Str("'")))
TOML = Lang(
    "toml", line=("#",),
    strings=(Str('"""', True, True), Str("'''", False, True), Str('"'), Str("'", escapes=False)),
    needs_space=True,
)
INI = Lang("ini", line=("#", ";"), full_line_only=True)
HASHLINE = Lang("hash-lines", line=("#",), full_line_only=True, keep=(_SHEBANG,))
DOCKER = Lang("dockerfile", line=("#",), full_line_only=True, keep=(_SHEBANG, r"^#\s*syntax=", r"^#\s*escape="))
MAKE = Lang("makefile", line=("#",), strings=())
POWERSHELL = Lang("powershell", line=("#",), blocks=(("<#", "#>"),), strings=(Str('"'), Str("'", escapes=False)), keep=(_SHEBANG,))
PERL = Lang(
    "perl", line=("#",), strings=(Str('"'), Str("'")), needs_space=True,
    heredoc=r"<<(?P<q>['\"]?)(?P<term>[A-Z_][A-Z0-9_]*)(?P=q)",
    line_blocks=((r"^=[a-zA-Z]", r"^=cut\b"),), keep=(_SHEBANG,),
)
RLANG = Lang("r", line=("#",), strings=(Str('"'), Str("'")), keep=(_SHEBANG,))
XML = Lang("xml", blocks=(("<!--", "-->"),))
JINJA = Lang("jinja", blocks=(("{#", "#}"),))
NONE = Lang("no-comments")

EXT_LANG = {
    ".py": PYTHON, ".pyi": PYTHON, ".pyw": PYTHON,
    ".yml": YAML, ".yaml": YAML,
    ".sh": SHELL, ".bash": SHELL, ".zsh": SHELL, ".ksh": SHELL,
    ".rb": RUBY, ".rake": RUBY, ".gemspec": RUBY, ".ru": RUBY,
    ".tf": HCL, ".tfvars": HCL, ".hcl": HCL, ".nomad": HCL,
    ".go": GO,
    ".js": CLIKE, ".mjs": CLIKE, ".cjs": CLIKE, ".ts": CLIKE, ".tsx": CLIKE, ".jsx": CLIKE,
    ".java": CLIKE, ".c": CLIKE, ".h": CLIKE, ".cc": CLIKE, ".cpp": CLIKE, ".hpp": CLIKE,
    ".cs": CLIKE, ".rs": CLIKE, ".swift": CLIKE, ".kt": CLIKE, ".kts": CLIKE, ".scala": CLIKE,
    ".groovy": CLIKE, ".dart": CLIKE, ".proto": CLIKE, ".gradle": CLIKE,
    ".php": PHP,
    ".css": CSS, ".scss": CSS, ".less": CSS,
    ".sql": SQL, ".lua": LUA, ".toml": TOML,
    ".ini": INI, ".cfg": INI, ".conf": INI, ".properties": INI, ".editorconfig": INI,
    ".env": HASHLINE, ".gitignore": HASHLINE, ".dockerignore": HASHLINE, ".gitattributes": HASHLINE,
    ".ps1": POWERSHELL, ".psm1": POWERSHELL,
    ".pl": PERL, ".pm": PERL,
    ".r": RLANG,
    ".xml": XML, ".html": XML, ".htm": XML, ".svg": XML, ".xsl": XML, ".xslt": XML, ".vue": XML,
    ".json": NONE, ".lock": NONE,
    ".j2": JINJA, ".jinja": JINJA, ".jinja2": JINJA,
    ".mk": MAKE,
}
NAME_LANG = {
    "dockerfile": DOCKER, "containerfile": DOCKER,
    "makefile": MAKE, "gnumakefile": MAKE,
    "gemfile": RUBY, "rakefile": RUBY, "vagrantfile": RUBY, "guardfile": RUBY, "podfile": RUBY,
    "jenkinsfile": CLIKE,
    ".bashrc": SHELL, ".bash_profile": SHELL, ".zshrc": SHELL, ".profile": SHELL,
}
SHEBANG_LANG = (
    (r"python", PYTHON), (r"\b(ba|z|k|da)?sh\b", SHELL), (r"ruby", RUBY),
    (r"perl", PERL), (r"\bnode\b|\bdeno\b|\bbun\b", CLIKE), (r"\bRscript\b", RLANG),
    (r"\bphp\b", PHP), (r"\bpwsh\b|powershell", POWERSHELL), (r"\blua\b", LUA),
)

DOC_EXT = {
    ".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".asciidoc", ".org", ".tex",
    ".pod", ".example", ".sample", ".pdf", ".docx", ".rtf", ".odt",
}
DOC_NAME_RE = re.compile(
    r"^(readme|license|licence|copying|changelog|changes|history|notice|authors|contributors|"
    r"contributing|code_of_conduct|security|maintainers|codeowners|todo|claude|agents)(\..*)?$",
    re.IGNORECASE,
)
DOC_DIRS = {"docs", "doc", "documentation"}
CODE_NAME_RE = re.compile(r"^(requirements[-\w]*\.txt|constraints[-\w]*\.txt|robots\.txt)$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _suffixes(name: str) -> list:
    parts = name.split(".")
    return ["." + p for p in parts[1:]] if len(parts) > 1 else []


def classify(path: str, include: Sequence[str], exclude: Sequence[str]) -> tuple:
    """Return ('code'|'doc', reason)."""
    for g in exclude:
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(_basename(path), g):
            return "doc", f"--exclude {g}"
    for g in include:
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(_basename(path), g):
            return "code", f"--include {g}"
    name = _basename(path)
    if CODE_NAME_RE.match(name):
        return "code", "well-known code file"
    for d in path.split("/")[:-1]:
        if d.lower() in DOC_DIRS:
            return "doc", f"under {d}/"
    sfx = _suffixes(name)
    if sfx and sfx[-1].lower() in DOC_EXT:
        return "doc", f"{sfx[-1]} file"
    if DOC_NAME_RE.match(name):
        return "doc", "documentation file name"
    return "code", "default"


def detect_lang(path: str, first_line: str = "") -> tuple:
    """Return (Lang, nested Lang or None). Nested is the inner language of a template."""
    name = _basename(path)
    lname = name.lower()
    if lname in NAME_LANG:
        return NAME_LANG[lname], None
    if lname.startswith("dockerfile") or lname.endswith(".dockerfile"):
        return DOCKER, None
    sfx = [s.lower() for s in _suffixes(name)]
    if sfx:
        lang = EXT_LANG.get(sfx[-1])
        if lang is JINJA:
            inner = EXT_LANG.get(sfx[-2]) if len(sfx) > 1 else None
            if inner is None and len(sfx) > 1:
                inner = None
            return JINJA, inner if inner not in (None, NONE, JINJA) else None
        if lang is not None:
            return lang, None
    if first_line.startswith("#!"):
        for pat, lang in SHEBANG_LANG:
            if re.search(pat, first_line):
                return lang, None
    return None, None


# --------------------------------------------------------------------------- #
# comment scanning
# --------------------------------------------------------------------------- #

@dataclass
class LineInfo:
    comment_only: bool      # the whole line is comment (or blank inside a block comment)
    stripped: str           # line content (no newline) with trailing comment removed
    had_comment: bool


def _kept(text: str, idx: int, lang: Lang, keep_directives: bool) -> bool:
    if idx == 0 and text.startswith("#!"):
        return True
    for pat in lang.keep:
        if (idx < 2 or not pat.startswith("^#!")) and re.search(pat, text):
            return True
    if keep_directives:
        for pat in lang.directives:
            if re.search(pat, text):
                return True
    return False


def _find_close(line: str, start: int, s: Str) -> int:
    i = start
    n = len(line)
    while i < n:
        if s.escapes and line[i] == "\\":
            i += 2
            continue
        if line.startswith(s.delim, i):
            if s.doubled and line.startswith(s.delim * 2, i):
                i += 2 * len(s.delim)
                continue
            return i
        i += 1
    return -1


def _at_value_start(line: str, i: int) -> bool:
    before = line[:i].rstrip()
    return before == "" or before[-1] in ":-[{,?"


def _split_lines(text: str) -> list:
    if not text:
        return []
    return re.split(r"(?<=\n)", text)


def _strip_nl(raw: str) -> str:
    return raw[:-2] if raw.endswith("\r\n") else raw[:-1] if raw.endswith("\n") else raw


def scan_generic(lines: list, lang: Lang, keep_directives: bool, nested: Optional[Lang] = None) -> list:
    """Per-line comment analysis with a small stateful scanner."""
    infos: list = []
    block_close: Optional[str] = None
    str_open: Optional[Str] = None
    heredocs: list = []
    line_block_close: Optional[str] = None
    heredoc_re = re.compile(lang.heredoc) if lang.heredoc else None
    nested_markers = tuple(nested.line) if nested and nested.line else ()

    for idx, raw in enumerate(lines):
        line = _strip_nl(raw)
        if heredocs:
            if line.strip() == heredocs[0]:
                heredocs.pop(0)
            infos.append(LineInfo(False, line, False))
            continue
        if line_block_close:
            if re.match(line_block_close, line):
                line_block_close = None
            infos.append(LineInfo(True, "", True))
            continue
        if not str_open and not block_close:
            for open_re, close_re in lang.line_blocks:
                if re.match(open_re, line):
                    line_block_close = close_re
                    break
            if line_block_close:
                infos.append(LineInfo(True, "", True))
                continue

        code: list = []
        had = False
        i = 0
        n = len(line)
        if block_close:
            j = line.find(block_close)
            if j < 0:
                infos.append(LineInfo(True, "", True))
                continue
            had = True
            i = j + len(block_close)
            block_close = None

        while i < n:
            if str_open:
                j = _find_close(line, i, str_open)
                if j < 0:
                    code.append(line[i:])
                    i = n
                    if not str_open.multiline:
                        str_open = None
                    break
                code.append(line[i:j + len(str_open.delim)])
                i = j + len(str_open.delim)
                str_open = None
                continue

            matched = False
            if heredoc_re:
                m = heredoc_re.match(line, i)
                if m:
                    heredocs.append(m.group("term"))
                    code.append(m.group(0))
                    i = m.end()
                    continue

            for s in lang.strings:
                if line.startswith(s.delim, i) and (not lang.string_at_value_start or _at_value_start(line, i)):
                    j = _find_close(line, i + len(s.delim), s)
                    if j < 0:
                        code.append(line[i:])
                        i = n
                        if s.multiline:
                            str_open = s
                    else:
                        code.append(line[i:j + len(s.delim)])
                        i = j + len(s.delim)
                    matched = True
                    break
            if matched:
                continue

            for o, c in lang.blocks:
                if line.startswith(o, i):
                    j = line.find(c, i + len(o))
                    if j < 0:
                        text = line[i:]
                        if _kept(text, idx, lang, keep_directives):
                            code.append(text)
                        else:
                            had = True
                            block_close = c
                        i = n
                    else:
                        text = line[i:j + len(c)]
                        if _kept(text, idx, lang, keep_directives):
                            code.append(text)
                        else:
                            had = True
                        i = j + len(c)
                    matched = True
                    break
            if matched:
                continue

            for mk in lang.line:
                if line.startswith(mk, i) and (not lang.needs_space or i == 0 or line[i - 1] in " \t"):
                    if lang.full_line_only and "".join(code).strip():
                        break
                    text = line[i:]
                    if _kept(text, idx, lang, keep_directives):
                        code.append(text)
                    else:
                        had = True
                    i = n
                    matched = True
                    break
            if matched:
                continue

            if nested_markers and not "".join(code).strip():
                for mk in nested_markers:
                    if line.startswith(mk, i) and (i == 0 or line[i - 1] in " \t"):
                        text = line[i:]
                        if _kept(text, idx, nested, keep_directives):
                            code.append(text)
                        else:
                            had = True
                        i = n
                        matched = True
                        break
                if matched:
                    continue

            code.append(line[i])
            i += 1

        code_s = "".join(code)
        if had and not code_s.strip():
            infos.append(LineInfo(True, "", True))
        elif had and not lang.full_line_only:
            infos.append(LineInfo(False, code_s.rstrip(), True))
        else:
            infos.append(LineInfo(False, line, False))
    return infos


def scan_python(lines: list, keep_docstrings: bool, keep_directives: bool) -> Optional[list]:
    src = "".join(lines)
    n = len(lines)
    comments: list = [None] * n
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                comments[tok.start[0] - 1] = (tok.start[1], tok.string)
    except (tokenize.TokenError, SyntaxError, IndentationError, IndexError):
        return None
    doc_lines: set = set()
    if not keep_docstrings:
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = node.body
                if len(body) < 2:
                    continue  # a docstring that is the whole body must stay (syntax)
                first = body[0]
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    doc_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    infos: list = []
    for idx, raw in enumerate(lines):
        line = _strip_nl(raw)
        if idx + 1 in doc_lines:
            infos.append(LineInfo(True, "", True))
            continue
        c = comments[idx]
        if c is None:
            infos.append(LineInfo(False, line, False))
            continue
        col, text = c
        if _kept(text, idx, PYTHON, keep_directives):
            infos.append(LineInfo(False, line, False))
            continue
        code = line[:col]
        if not code.strip():
            infos.append(LineInfo(True, "", True))
        else:
            infos.append(LineInfo(False, code.rstrip(), True))
    return infos


def analyze(lines: list, lang: Lang, nested: Optional[Lang], keep_docstrings: bool, keep_directives: bool) -> list:
    if lang.python:
        infos = scan_python(lines, keep_docstrings, keep_directives)
        if infos is not None:
            return infos
    return scan_generic(lines, lang, keep_directives, nested)


# --------------------------------------------------------------------------- #
# diff-aware merge
# --------------------------------------------------------------------------- #

@dataclass
class Stats:
    code_added: int = 0        # added lines kept
    comments_dropped: int = 0  # added comment-only lines removed
    trailing_stripped: int = 0  # added lines whose trailing comment was cut
    restored: int = 0          # deleted comment-only lines put back
    blanks_collapsed: int = 0
    reverted: int = 0          # added lines identical to a base line once stripped


def _is_blank(raw: str) -> bool:
    return not _strip_nl(raw).strip()


def _merge_block(base_lines, base_infos, head_lines, head_infos, out: list, st: Stats) -> None:
    subs: dict = {}
    restored: list = []
    for k, (line, info) in enumerate(zip(base_lines, base_infos)):
        if info.comment_only:
            restored.append((k, line))
        else:
            subs.setdefault(info.stripped, []).append((k, line))
    r = 0

    def flush(upto: int) -> None:
        nonlocal r
        while r < len(restored) and restored[r][0] < upto:
            out.append(restored[r][1])
            st.restored += 1
            r += 1

    dropped_prev = False
    for line, info in zip(head_lines, head_infos):
        nl = line[len(_strip_nl(line)):]
        if info.comment_only:
            st.comments_dropped += 1
            dropped_prev = True
            continue
        if _is_blank(line):
            if dropped_prev and out and _is_blank(out[-1]):
                st.blanks_collapsed += 1
                continue
            dropped_prev = False
            out.append(line)
            st.code_added += 1
            continue
        dropped_prev = False
        s = info.stripped
        if info.had_comment and s != _strip_nl(line):
            st.trailing_stripped += 1
        if s in subs and subs[s]:
            k, orig = subs[s].pop(0)
            flush(k)
            out.append(orig)
            st.reverted += 1
        else:
            out.append(s + nl)
            st.code_added += 1
    flush(len(base_lines))


def transform(base_text: str, head_text: str, lang: Lang, nested: Optional[Lang],
              keep_docstrings: bool, keep_directives: bool) -> tuple:
    """Return (new_text, Stats): head_text with comment changes relative to base_text undone."""
    base_lines = _split_lines(base_text)
    head_lines = _split_lines(head_text)
    base_infos = analyze(base_lines, lang, nested, keep_docstrings, keep_directives)
    head_infos = analyze(head_lines, lang, nested, keep_docstrings, keep_directives)
    sm = difflib.SequenceMatcher(None, base_lines, head_lines, autojunk=False)
    out: list = []
    st = Stats()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(base_lines[i1:i2])
        else:
            _merge_block(base_lines[i1:i2], base_infos[i1:i2], head_lines[j1:j2], head_infos[j1:j2], out, st)
    return "".join(out), st


def residual_comment_lines(base_text: str, new_text: str, lang: Lang, nested: Optional[Lang],
                           keep_docstrings: bool, keep_directives: bool) -> int:
    """Self-check: comment-only lines that still differ between base and result."""
    base_lines = _split_lines(base_text)
    new_lines = _split_lines(new_text)
    bi = analyze(base_lines, lang, nested, keep_docstrings, keep_directives)
    ni = analyze(new_lines, lang, nested, keep_docstrings, keep_directives)
    sm = difflib.SequenceMatcher(None, base_lines, new_lines, autojunk=False)
    count = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        count += sum(1 for x in bi[i1:i2] if x.comment_only)
        count += sum(1 for x in ni[j1:j2] if x.comment_only)
    return count


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #

class Fail(Exception):
    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


def git(*args: str, cwd: Optional[str] = None, check: bool = True, binary: bool = False):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if check and p.returncode != 0:
        raise Fail(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    if binary:
        return p.stdout
    return p.stdout.decode("utf-8", "replace").strip()


def git_ok(*args: str, cwd: Optional[str] = None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def blob(rev: str, path: str, cwd: str) -> bytes:
    return git("show", f"{rev}:{path}", cwd=cwd, binary=True)


def mode_of(rev: str, path: str, cwd: str) -> str:
    out = git("ls-tree", rev, "--", path, cwd=cwd)
    return out.split(" ", 1)[0] if out else "100644"


def resolve_base(cwd: str, explicit: Optional[str]) -> tuple:
    """Return (ref, short_name) of the default branch."""
    if explicit:
        if not git_ok("rev-parse", "-q", "--verify", explicit + "^{commit}", cwd=cwd):
            raise Fail(f"--base {explicit!r} does not resolve to a commit")
        return explicit, explicit.split("/")[-1]
    for remote in ("origin", "upstream"):
        ref = git("symbolic-ref", "-q", f"refs/remotes/{remote}/HEAD", cwd=cwd, check=False)
        if ref:
            short = ref[len("refs/remotes/"):]
            return short, short.split("/", 1)[1]
    for cand in DEFAULT_BRANCH_CANDIDATES:
        if git_ok("rev-parse", "-q", "--verify", f"refs/heads/{cand}", cwd=cwd):
            return cand, cand
    raise Fail("cannot determine the default branch; pass --base <ref>")


def changed_files(mb: str, head: str, cwd: str) -> list:
    """[(status, old_path, new_path)] with status in A M D R T C."""
    raw = git("diff", "--name-status", "-M", "-z", mb, head, cwd=cwd, binary=True).decode("utf-8", "surrogateescape")
    parts = raw.split("\0")
    entries: list = []
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status[0] in "RC":
            entries.append((status[0], parts[i + 1], parts[i + 2]))
            i += 3
        else:
            entries.append((status[0], parts[i + 1], parts[i + 1]))
            i += 2
    return entries


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def load_config(root: str) -> dict:
    path = os.path.join(root, CONFIG_FILE)
    if not os.path.exists(path):
        return {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        print(f"warning: {CONFIG_FILE} found but this Python has no tomllib; ignoring it", file=sys.stderr)
        return {}
    with open(path, "rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise Fail(f"{CONFIG_FILE}: {exc}")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

@dataclass
class Item:
    status: str
    old: str
    new: str
    kind: str                # code | doc
    reason: str
    lang: Optional[Lang] = None
    nested: Optional[Lang] = None
    verbatim: str = ""       # why the file is copied as-is (binary, symlink, unknown language...)
    content: Optional[bytes] = None
    unchanged: bool = False
    stats: Stats = field(default_factory=Stats)
    residual: int = 0


def plan(cwd: str, mb: str, head: str, include, exclude, keep_docstrings, keep_directives) -> list:
    items: list = []
    for status, old, new in changed_files(mb, head, cwd):
        kind, reason = classify(new if status != "D" else old, include, exclude)
        it = Item(status, old, new, kind, reason)
        items.append(it)
        if kind != "code" or status == "D":
            continue
        mode = mode_of(head, new, cwd)
        if mode in ("120000", "160000"):
            it.verbatim = "symlink" if mode == "120000" else "submodule"
            continue
        head_b = blob(head, new, cwd)
        if b"\0" in head_b[:8192]:
            it.verbatim = "binary"
            continue
        head_t = head_b.decode("utf-8", "surrogateescape")
        first = head_t.split("\n", 1)[0]
        lang, nested = detect_lang(new, first)
        if lang is None:
            it.verbatim = "unknown language"
            continue
        it.lang, it.nested = lang, nested
        if lang is NONE:
            it.verbatim = "language has no comments"
            continue
        base_t = blob(mb, old, cwd).decode("utf-8", "surrogateescape") if status in ("M", "R", "C", "T") else ""
        new_t, st = transform(base_t, head_t, lang, nested, keep_docstrings, keep_directives)
        it.stats = st
        it.residual = residual_comment_lines(base_t, new_t, lang, nested, keep_docstrings, keep_directives)
        it.content = new_t.encode("utf-8", "surrogateescape")
        it.unchanged = status != "A" and new_t == base_t and old == new
    return items


def lang_label(it: Item) -> str:
    if it.lang is None:
        return "-"
    return it.lang.name + (f"+{it.nested.name}" if it.nested else "")


def report(items: list, branch: str, target: str, base_ref: str, base_short: str, mb: str, head: str) -> None:
    code = [i for i in items if i.kind == "code"]
    docs = [i for i in items if i.kind == "doc"]
    print(f"nocomment: {branch} -> {target}")
    print(f"  base {base_ref} (merge-base {mb[:8]})   head {head[:8]}")
    print()
    print(f"CODE ({len(code)} file{'s' if len(code) != 1 else ''}) - included, comment changes removed")
    width = max((len(i.new) for i in code), default=10)
    if code:
        print(f"  {'':2} {'path':<{width}}  {'language':<14} {'code_added':>10} {'comments_removed':>16} "
              f"{'trailing_removed':>16} {'comments_restored':>17}  note")
    for it in code:
        st = it.stats
        note = ""
        if it.status == "D":
            note = "deleted on branch"
        elif it.verbatim:
            note = f"copied verbatim ({it.verbatim})"
        elif it.unchanged:
            note = "no code change after stripping - left out"
        elif it.residual:
            note = f"WARNING {it.residual} comment line(s) still differ"
        if it.status == "R":
            note = (note + "; " if note else "") + f"renamed from {it.old}"
        print(f"  {it.status:2} {it.new:<{width}}  {lang_label(it):<14} {st.code_added:>10} "
              f"{st.comments_dropped:>16} {st.trailing_stripped:>16} {st.restored:>17}  {note}")
    print()
    print(f"EXCLUDED ({len(docs)} file{'s' if len(docs) != 1 else ''}) - documentation, not on the new branch")
    for it in docs:
        print(f"  {it.status:2} {it.new}  ({it.reason})")
    print()


def build_branch(cwd: str, items: list, target: str, mb: str, head: str, branch: str,
                 base_ref: str, force: bool) -> str:
    existed = git_ok("rev-parse", "-q", "--verify", f"refs/heads/{target}", cwd=cwd)
    if existed:
        if not force:
            raise Fail(f"branch {target!r} already exists; pass --force to recreate it")
        wt = git("worktree", "list", "--porcelain", cwd=cwd)
        if f"branch refs/heads/{target}\n" in wt + "\n":
            raise Fail(f"branch {target!r} is checked out in a worktree; remove it first")
        git("branch", "-D", target, cwd=cwd)
    tmp = tempfile.mkdtemp(prefix="nocomment-")
    os.rmdir(tmp)
    created = False
    try:
        git("worktree", "add", "-q", "-b", target, tmp, mb, cwd=cwd)
        created = True
        for it in items:
            if it.kind != "code":
                continue
            if it.status == "D":
                git("rm", "-q", "--", it.old, cwd=tmp)
                continue
            if it.status == "R":
                git("rm", "-q", "--", it.old, cwd=tmp)
            if it.unchanged:
                continue
            git("checkout", "-q", head, "--", it.new, cwd=tmp)
            if it.content is not None:
                with open(os.path.join(tmp, it.new), "wb") as fh:
                    fh.write(it.content)
        git("add", "-A", cwd=tmp)
        if git_ok("diff", "--cached", "--quiet", cwd=tmp):
            raise Fail("nothing to commit: no code changes remain after stripping comments", 3)
        msg = commit_message(items, branch, target, base_ref, mb, head)
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as fh:
            fh.write(msg)
            msg_path = fh.name
        try:
            git("commit", "-q", "--no-verify", "-F", msg_path, cwd=tmp)
        finally:
            os.unlink(msg_path)
        return git("rev-parse", "HEAD", cwd=tmp)
    except BaseException:
        if created:
            subprocess.run(["git", "worktree", "remove", "--force", tmp], cwd=cwd, capture_output=True)
            subprocess.run(["git", "branch", "-D", target], cwd=cwd, capture_output=True)
        raise
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", tmp], cwd=cwd, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=cwd, capture_output=True)


def commit_message(items: list, branch: str, target: str, base_ref: str, mb: str, head: str) -> str:
    code = [i for i in items if i.kind == "code" and not i.unchanged]
    docs = [i for i in items if i.kind == "doc"]
    lines = [
        f"nocomment: code-only view of {branch}",
        "",
        f"Generated from {branch} @ {head[:12]} against {base_ref} (merge-base {mb[:12]}).",
        "Documentation files are excluded and comment changes are undone, so the",
        f"diff against {base_ref} shows code changes only. Review artifact - not for merging.",
        "",
        f"Included ({len(code)}):",
    ]
    for it in code:
        st = it.stats
        detail = f" (-{st.comments_dropped} comment lines, -{st.trailing_stripped} trailing)" if it.content is not None and it.status != "D" else ""
        lines.append(f"  {it.status} {it.new}{detail}")
    if docs:
        lines.append("")
        lines.append(f"Excluded as documentation ({len(docs)}):")
        lines.extend(f"  {it.status} {it.new}" for it in docs)
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="nocomment", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Globs are fnmatch patterns tested against the repo-relative path and the basename.")
    p.add_argument("-C", dest="dir", default=".", help="repository directory (default: cwd)")
    p.add_argument("--base", help="default-branch ref to diff against (default: origin/HEAD, else main/master)")
    p.add_argument("--prefix", help=f"branch-name prefix (default: {DEFAULT_PREFIX})")
    p.add_argument("--include", action="append", default=[], metavar="GLOB", help="force files matching GLOB to be treated as code")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="force files matching GLOB to be treated as documentation")
    p.add_argument("--keep-docstrings", action="store_true", help="do not strip Python docstrings")
    p.add_argument("--keep-directives", action="store_true", help="keep tool directives (noqa, type:, go:build, eslint, ...)")
    p.add_argument("--dry-run", action="store_true", help="report what would happen; create nothing")
    p.add_argument("--force", action="store_true", help="replace the target branch if it exists")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        cwd = os.path.abspath(args.dir)
        if not git_ok("rev-parse", "--is-inside-work-tree", cwd=cwd):
            raise Fail(f"{cwd} is not inside a git repository")
        root = git("rev-parse", "--show-toplevel", cwd=cwd)
        cfg = load_config(root)
        prefix = args.prefix or cfg.get("prefix") or DEFAULT_PREFIX
        include = list(cfg.get("include", [])) + args.include
        exclude = list(cfg.get("exclude", [])) + args.exclude
        keep_docstrings = args.keep_docstrings or bool(cfg.get("keep_docstrings"))
        keep_directives = args.keep_directives or bool(cfg.get("keep_directives"))

        branch = git("symbolic-ref", "-q", "--short", "HEAD", cwd=root, check=False)
        if not branch:
            raise Fail("HEAD is detached; check out the feature branch first")
        base_ref, base_short = resolve_base(root, args.base or cfg.get("base"))
        if branch == base_short:
            raise Fail(f"you are on the default branch {branch!r}; check out the feature branch first")
        if branch.startswith(prefix):
            raise Fail(f"{branch!r} is already a {prefix} branch")
        head = git("rev-parse", "HEAD", cwd=root)
        mb = git("merge-base", base_ref, head, cwd=root)
        if mb == head:
            raise Fail(f"{branch!r} has no commits beyond {base_ref}")
        target = prefix + branch

        items = plan(root, mb, head, include, exclude, keep_docstrings, keep_directives)
        report(items, branch, target, base_ref, base_short, mb, head)
        effective = [i for i in items if i.kind == "code" and not i.unchanged]
        if not effective:
            print("nothing to do: no code changes remain after stripping comments")
            return 3
        if args.dry_run:
            print(f"dry run: would create {target} from merge-base {mb[:8]} with {len(effective)} file(s)")
            return 0
        sha = build_branch(root, items, target, mb, head, branch, base_ref, args.force)
        stat = git("diff", "--shortstat", f"{mb}..{sha}", cwd=root)
        print(f"created {target} @ {sha[:8]}  ({stat or 'no textual changes'})")
        print(f"  view:       git diff {base_short}..{target}")
        print(f"  regenerate: nocomment --force")
        print(f"  your checkout ({branch}) was not touched")
        return 0
    except Fail as exc:
        print(f"nocomment: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
