#!/usr/bin/env python3
"""Tests for the nocomment script: comment scanners, the diff-aware merge, and the git flow.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "plugins", "nocomment", "skills", "nocomment", "scripts", "nocomment.py")
sys.path.insert(0, os.path.dirname(SCRIPT))
import nocomment as nc  # noqa: E402


def d(s: str) -> str:
    return textwrap.dedent(s).lstrip("\n")


def strip(text: str, lang, nested=None, keep_docstrings=False, keep_directives=False) -> str:
    """Whole-file strip: treat every line as newly added."""
    out, _ = nc.transform("", text, lang, nested, keep_docstrings, keep_directives)
    return out


class ScannerTests(unittest.TestCase):
    def test_python_comments_docstrings_and_directives(self):
        src = d('''
            #!/usr/bin/env python3
            # -*- coding: utf-8 -*-
            """Module docstring."""
            import os  # noqa: E402

            # a full-line comment
            x = "not # a comment"  # trailing


            def f():
                """Doc."""
                return os.sep  # type: ignore


            def stub():
                """Only a docstring: must stay or the def is empty."""
        ''')
        out = strip(src, nc.PYTHON)
        self.assertEqual(out, d('''
            #!/usr/bin/env python3
            # -*- coding: utf-8 -*-
            import os

            x = "not # a comment"


            def f():
                return os.sep


            def stub():
                """Only a docstring: must stay or the def is empty."""
        '''))
        kept = strip(src, nc.PYTHON, keep_directives=True)
        self.assertIn("import os  # noqa: E402", kept)
        self.assertIn("return os.sep  # type: ignore", kept)
        self.assertNotIn("# a full-line comment", kept)
        docs = strip(src, nc.PYTHON, keep_docstrings=True)
        self.assertIn('"""Module docstring."""', docs)
        self.assertIn('    """Doc."""', docs)

    def test_python_fallback_on_syntax_error(self):
        src = "def broken(:\n    # comment\n    x = 1  # trailing\n"
        out = strip(src, nc.PYTHON)
        self.assertEqual(out, "def broken(:\n    x = 1\n")

    def test_yaml(self):
        src = d('''
            # header
            all:
              vars:
                # explain
                key: value            # trailing
                url: "http://x/#frag" # quoted hash is data
                plain: value#notcomment
                quote: 'it''s # here'
                lst:
                  - "a#b"   # c
        ''')
        self.assertEqual(strip(src, nc.YAML), d('''
            all:
              vars:
                key: value
                url: "http://x/#frag"
                plain: value#notcomment
                quote: 'it''s # here'
                lst:
                  - "a#b"
        '''))

    def test_jinja_with_nested_ruby(self):
        src = d('''
            {# header block
               spanning lines #}
            gitlab_rails['x'] = 1
            {#- dashed
                block -#}
            {% if a %}
            # ruby full-line comment
            gitlab_rails['y'] = '{{ v }}' {# inline #}
            {% endif %}
        ''')
        lang, nested = nc.detect_lang("files/gitlab_rails.rb.j2")
        self.assertIs(lang, nc.JINJA)
        self.assertIs(nested, nc.RUBY)
        self.assertEqual(strip(src, lang, nested), d('''
            gitlab_rails['x'] = 1
            {% if a %}
            gitlab_rails['y'] = '{{ v }}'
            {% endif %}
        '''))

    def test_shell(self):
        src = d('''
            #!/usr/bin/env bash
            # comment
            set -euo pipefail   # trailing
            echo "$#" "${#arr[@]}" '#literal'
            cat <<'EOF'
            # inside heredoc, not a comment
            EOF
            cat << EOF
            # also data
            EOF
            x=1 # done
        ''')
        self.assertEqual(strip(src, nc.SHELL), d('''
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$#" "${#arr[@]}" '#literal'
            cat <<'EOF'
            # inside heredoc, not a comment
            EOF
            cat << EOF
            # also data
            EOF
            x=1
        '''))

    def test_ruby(self):
        src = d('''
            # frozen_string_literal: true
            # comment
            =begin
            block
            =end
            s = "#{x} # not a comment"  # trailing
            h = <<~EOS
              # heredoc data
            EOS
        ''')
        self.assertEqual(strip(src, nc.RUBY), d('''
            # frozen_string_literal: true
            s = "#{x} # not a comment"
            h = <<~EOS
              # heredoc data
            EOS
        '''))

    def test_hcl(self):
        src = d('''
            # hash
            // slash
            /* block
               comment */
            resource "x" "y" {
              name = "a // b # c"  // trailing
              doc  = <<-EOT
                # heredoc data
              EOT
              n = 1 /* inline */ + 2
            }
        ''')
        self.assertEqual(strip(src, nc.HCL), d('''
            resource "x" "y" {
              name = "a // b # c"
              doc  = <<-EOT
                # heredoc data
              EOT
              n = 1  + 2
            }
        '''))

    def test_go_and_clike(self):
        src = d('''
            //go:build linux
            // Package x does things.
            package x

            /* multi
               line */
            const u = "http://x//y" // trailing
            const r = `raw // not
            // still raw`
        ''')
        out = strip(src, nc.GO)
        self.assertEqual(out, d('''
            package x

            const u = "http://x//y"
            const r = `raw // not
            // still raw`
        '''))
        self.assertIn("//go:build linux", strip(src, nc.GO, keep_directives=True))

    def test_full_line_only_languages(self):
        docker = "# syntax=docker/dockerfile:1\n# comment\nRUN echo '#' # not stripped\n"
        self.assertEqual(strip(docker, nc.DOCKER), "# syntax=docker/dockerfile:1\nRUN echo '#' # not stripped\n")
        ini = "; comment\n# comment\n[s]\nk = v ; kept\n"
        self.assertEqual(strip(ini, nc.INI), "[s]\nk = v ; kept\n")

    def test_misc_languages(self):
        self.assertEqual(strip("-- c\nSELECT 1; -- t\n/* b */\n", nc.SQL), "SELECT 1;\n")
        self.assertEqual(strip("<a/>\n<!-- c\n -->\n<b/>\n", nc.XML), "<a/>\n<b/>\n")
        self.assertEqual(strip("# c\nk = 'v # x' # t\n", nc.TOML), "k = 'v # x'\n")
        self.assertEqual(strip("--[[ block\n]]\nx = 1 -- t\n", nc.LUA), "x = 1\n")
        self.assertEqual(strip("x <- 1 # t\n# c\n", nc.RLANG), "x <- 1\n")

    def test_blank_lines_around_removed_comments_collapse(self):
        src = "a\n\n# c\n\nb\n"
        self.assertEqual(strip(src, nc.PYTHON), "a\n\nb\n")
        src = "a\n# c\n\nb\n"
        self.assertEqual(strip(src, nc.PYTHON), "a\n\nb\n")


class ClassifyTests(unittest.TestCase):
    def test_defaults(self):
        doc = ["README.md", "docs/plans/x.md", "ansible/environments/CLAUDE.md", "a/secret-vars.yml.example",
               "LICENSE", "CHANGELOG", "notes.txt", "AGENTS.md", "doc/x.py"]
        code = ["a/vars.yml", "x.py", "t.rb.j2", "Dockerfile", "requirements.txt", "main.tf", "Makefile", "x.json"]
        for p in doc:
            self.assertEqual(nc.classify(p, [], [])[0], "doc", p)
        for p in code:
            self.assertEqual(nc.classify(p, [], [])[0], "code", p)

    def test_overrides(self):
        self.assertEqual(nc.classify("a/x.yml.example", ["*.example"], [])[0], "code")
        self.assertEqual(nc.classify("tests/x.py", [], ["tests/*"])[0], "doc")
        self.assertEqual(nc.classify("tests/x.py", ["*.py"], ["tests/*"])[0], "doc")  # exclude wins

    def test_detect_by_shebang(self):
        self.assertIs(nc.detect_lang("bin/tool", "#!/usr/bin/env python3")[0], nc.PYTHON)
        self.assertIs(nc.detect_lang("bin/tool", "#!/bin/bash")[0], nc.SHELL)
        self.assertIsNone(nc.detect_lang("bin/tool", "plain")[0])


class MergeTests(unittest.TestCase):
    def test_added_comments_dropped_deleted_comments_restored(self):
        base = "a = 1\n# old comment\nb = 2\nc = 3\n"
        head = "a = 1\nb = 2\n# new comment\nc = 4\n"
        out, st = nc.transform(base, head, nc.PYTHON, None, False, False)
        self.assertEqual(out, "a = 1\n# old comment\nb = 2\nc = 4\n")
        self.assertEqual((st.comments_dropped, st.restored, st.code_added), (1, 1, 1))
        self.assertEqual(nc.residual_comment_lines(base, out, nc.PYTHON, None, False, False), 0)

    def test_trailing_comment_change_is_reverted(self):
        base = "x = 1  # old\ny = 2\n"
        head = "x = 1  # new\ny = 2  # added\nz = 3  # new line\n"
        out, st = nc.transform(base, head, nc.PYTHON, None, False, False)
        self.assertEqual(out, "x = 1  # old\ny = 2\nz = 3\n")
        self.assertEqual(st.reverted, 2)  # both base lines re-emitted verbatim
        self.assertEqual(nc.residual_comment_lines(base, out, nc.PYTHON, None, False, False), 0)

    def test_only_comment_changes_yield_identical_file(self):
        base = "k: v\n"
        head = "# added\nk: v  # trailing\n"
        out, _ = nc.transform(base, head, nc.YAML, None, False, False)
        self.assertEqual(out, base)

    def test_new_file_and_crlf_preserved(self):
        head = "# c\r\nk: v\r\n"
        out, _ = nc.transform("", head, nc.YAML, None, False, False)
        self.assertEqual(out, "k: v\r\n")

    def test_no_trailing_newline_preserved(self):
        out, _ = nc.transform("a = 1\n", "a = 1\nb = 2  # t", nc.PYTHON, None, False, False)
        self.assertEqual(out, "a = 1\nb = 2")


class GitFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nocomment-test-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.git("config", "commit.gpgsign", "false")
        self.write("app.py", "x = 1\n# keep me\ny = 2\n")
        self.write("vars.yml", "a: 1\n")
        self.write("README.md", "# readme\n")
        self.write("old.sh", "#!/bin/sh\necho hi\n")
        self.write("gone.py", "obsolete = True\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp])

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def write(self, path, content):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(content)

    def run_tool(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, capture_output=True, text=True)

    def make_feature(self):
        self.git("checkout", "-q", "-b", "feat/x")
        self.write("app.py", "x = 1\ny = 2  # trailing\n\n# explain\nz = 3\n")           # deleted + added comments
        self.write("vars.yml", "a: 1\n# new\nb: 2\n")
        self.write("README.md", "# readme\nmore\n")
        self.write("docs/plan.md", "plan\n")
        self.write("new.rb.j2", "{# hdr #}\nk = 1\n")
        self.git("mv", "old.sh", "new.sh")
        self.write("new.sh", "#!/bin/sh\n# c\necho hi # t\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feature")
        self.git("rm", "-q", "gone.py")
        self.git("commit", "-q", "-m", "drop")

    def test_refuses_on_default_branch(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("default branch", r.stderr)

    def test_dry_run_creates_nothing(self):
        self.make_feature()
        r = self.run_tool("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry run", r.stdout)
        self.assertEqual(self.git("branch", "--list", "nocomment/*"), "")
        self.assertIn("docs/plan.md  (under docs/)", r.stdout)
        self.assertIn("README.md  (.md file)", r.stdout)

    def test_creates_code_only_branch(self):
        self.make_feature()
        head_before = self.git("rev-parse", "HEAD")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("created nocomment/feat/x", r.stdout)
        # current checkout untouched
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), "feat/x")
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)  # temp worktree removed
        # one commit on top of main
        self.assertEqual(self.git("rev-list", "--count", "main..nocomment/feat/x"), "1")
        files = self.git("diff", "--name-only", "--no-renames", "main..nocomment/feat/x").splitlines()
        self.assertEqual(sorted(files), ["app.py", "gone.py", "new.rb.j2", "new.sh", "old.sh", "vars.yml"])
        show = lambda p: self.git("show", f"nocomment/feat/x:{p}")  # noqa: E731
        self.assertEqual(show("app.py"), "x = 1\n# keep me\ny = 2\n\nz = 3".rstrip("\n"))
        self.assertEqual(show("vars.yml"), "a: 1\nb: 2")
        self.assertEqual(show("new.rb.j2"), "k = 1")
        self.assertEqual(show("new.sh"), "#!/bin/sh\necho hi")
        self.assertFalse(subprocess.run(["git", "cat-file", "-e", "nocomment/feat/x:old.sh"], cwd=self.repo, capture_output=True).returncode == 0)
        self.assertFalse(subprocess.run(["git", "cat-file", "-e", "nocomment/feat/x:gone.py"], cwd=self.repo, capture_output=True).returncode == 0)
        # the diff against main carries no comment lines at all
        diff = self.git("diff", "main..nocomment/feat/x")
        for line in diff.splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                self.assertNotRegex(line[1:].lstrip(), r"^#(?!!)|^\{#", line)
        # second run refuses, --force replaces
        r2 = self.run_tool()
        self.assertEqual(r2.returncode, 2)
        self.assertIn("already exists", r2.stderr)
        r3 = self.run_tool("--force")
        self.assertEqual(r3.returncode, 0, r3.stderr)

    def test_nothing_to_commit(self):
        self.git("checkout", "-q", "-b", "feat/docs")
        self.write("app.py", "x = 1\n# keep me\n# extra\ny = 2  # t\n")
        self.write("README.md", "changed\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "docs only")
        r = self.run_tool()
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.git("branch", "--list", "nocomment/*"), "")

    def test_include_exclude_and_config(self):
        self.make_feature()
        self.write(".nocomment.toml", 'include = ["*.md"]\nprefix = "clean/"\n')
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "cfg")
        r = self.run_tool("--dry-run", "--exclude", "vars.yml")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-> clean/feat/x", r.stdout)
        self.assertIn("M  README.md", r.stdout)
        self.assertIn("vars.yml  (--exclude vars.yml)", r.stdout)


if __name__ == "__main__":
    unittest.main()
