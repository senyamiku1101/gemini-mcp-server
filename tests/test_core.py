"""Unit tests for gemini_core shared primitives.

Run with: pytest tests/
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the parent dir importable when running pytest from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gemini_core as core


# ---------- sanitize_for_prompt ------------------------------------------

class TestSanitizeForPrompt:
    def test_preserves_code_shape(self):
        code = (
            "def f():\n"
            "    # ### header in comment\n"
            "    sep = '---'\n"
            "    return \"```fenced```\"\n"
        )
        out = core.sanitize_for_prompt(code)
        # Sanitizer must not mangle code/markdown punctuation.
        assert out == code

    def test_truncates_at_max_length(self):
        big = "x" * 500
        out = core.sanitize_for_prompt(big, max_length=100)
        assert len(out) == 100

    def test_strips_nul_and_esc(self):
        dirty = "hello\x00world\x1bnasty"
        out = core.sanitize_for_prompt(dirty)
        assert "\x00" not in out
        assert "\x1b" not in out
        assert out == "helloworldnasty"

    def test_non_string_returns_empty(self):
        assert core.sanitize_for_prompt(None) == ""  # type: ignore[arg-type]
        assert core.sanitize_for_prompt(123) == ""  # type: ignore[arg-type]


# ---------- redact --------------------------------------------------------

class TestRedact:
    def test_redacts_google_api_key(self):
        text = "Error: invalid key AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ012345 — denied"
        out = core.redact(text)
        assert "AIzaSy" not in out
        assert "[API_KEY_REDACTED]" in out

    def test_redacts_sk_token(self):
        text = "Bearer sk-abcdefghijklmnopqrstuvwxyz0123456789 was used"
        out = core.redact(text)
        # Bearer pattern also matches; either redaction is acceptable.
        assert "sk-abcdef" not in out

    def test_passes_clean_text_through(self):
        text = "no credentials here"
        assert core.redact(text) == text


# ---------- is_secret_filename --------------------------------------------

@pytest.mark.parametrize("name,expected", [
    (".env", True),
    (".env.local", True),
    (".env.production", True),
    (".envrc", False),  # similar but not a secret
    ("app.py", False),
    ("id_rsa", True),
    ("id_ed25519", True),
    ("cert.pem", True),
    ("private.key", True),
    ("server.crt", True),
    ("keystore.jks", True),
    ("foo.txt", False),
    (".npmrc", True),
    ("README.md", False),
    ("credentials.json", True),
    ("credentials", True),
])
def test_is_secret_filename(name, expected):
    assert core.is_secret_filename(name) is expected


# ---------- build_codebase_context ----------------------------------------

class TestBuildCodebaseContext:
    def test_redacts_secret_file_contents(self, tmp_path):
        (tmp_path / "app.py").write_text('print("hello")', encoding="utf-8")
        (tmp_path / ".env").write_text("SECRET=abcdef1234567890", encoding="utf-8")
        (tmp_path / "private.key").write_text("-----BEGIN-----", encoding="utf-8")

        ctx = core.build_codebase_context(tmp_path)

        assert ".env" in ctx  # appears in tree
        assert "SECRET=abcdef" not in ctx  # content not leaked
        assert "content redacted" in ctx  # redaction marker present
        assert "-----BEGIN-----" not in ctx  # private.key content not leaked
        assert 'print("hello")' in ctx  # regular file still included

    def test_prunes_ignored_dirs(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1", encoding="utf-8")
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "pkg.js").write_text("// huge dep", encoding="utf-8")
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

        ctx = core.build_codebase_context(tmp_path)

        assert "node_modules" not in ctx
        assert "pkg.js" not in ctx
        assert ".git" not in ctx
        assert "x = 1" in ctx

    def test_tree_truncation_kicks_in(self, tmp_path):
        for i in range(100):
            (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")

        ctx = core.build_codebase_context(tmp_path, max_tree_entries=20)
        assert "[tree truncated]" in ctx

    def test_nested_paths_use_forward_slashes(self, tmp_path):
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)
        (sub / "main.py").write_text("x = 1", encoding="utf-8")

        ctx = core.build_codebase_context(tmp_path)
        # Should use forward slashes regardless of platform.
        assert "src/pkg/main.py" in ctx
        assert "src\\pkg\\main.py" not in ctx


# ---------- get_allowed_roots / path_within_allowed / validate_path -------

class TestPathSecurity:
    def test_default_root_is_cwd(self, monkeypatch):
        monkeypatch.delenv("GEMINI_MCP_ALLOWED_ROOTS", raising=False)
        roots = core.get_allowed_roots()
        assert roots == [Path.cwd().resolve()]

    def test_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_MCP_ALLOWED_ROOTS", str(tmp_path))
        roots = core.get_allowed_roots()
        assert roots == [tmp_path.resolve()]

    def test_multiple_roots_with_pathsep(self, monkeypatch, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv("GEMINI_MCP_ALLOWED_ROOTS", f"{a}{os.pathsep}{b}")
        roots = core.get_allowed_roots()
        assert len(roots) == 2
        assert a.resolve() in roots
        assert b.resolve() in roots

    def test_path_within_allowed_inside(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_MCP_ALLOWED_ROOTS", str(tmp_path))
        sub = tmp_path / "project"
        sub.mkdir()
        assert core.path_within_allowed(sub.resolve()) is True

    def test_path_within_allowed_outside(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_MCP_ALLOWED_ROOTS", str(tmp_path))
        elsewhere = Path("C:\\Windows" if os.name == "nt" else "/etc").resolve()
        assert core.path_within_allowed(elsewhere) is False

    def test_validate_path_security_inside(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_MCP_ALLOWED_ROOTS", str(tmp_path))
        sub = tmp_path / "project"
        sub.mkdir()
        ok, msg, resolved = core.validate_path_security(str(sub))
        assert ok is True
        assert resolved == sub.resolve()

    def test_validate_path_security_traversal_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_MCP_ALLOWED_ROOTS", str(tmp_path))
        sub = tmp_path / "project"
        sub.mkdir()
        # ../../ traversal must be neutralised by resolve()
        bad = str(sub / ".." / ".." / "evil")
        ok, msg, resolved = core.validate_path_security(bad)
        assert ok is False
        assert "allowed roots" in msg.lower()

    def test_validate_path_security_outside(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_MCP_ALLOWED_ROOTS", str(tmp_path))
        elsewhere = "C:\\Windows" if os.name == "nt" else "/etc"
        ok, msg, _ = core.validate_path_security(elsewhere)
        assert ok is False
        assert "allowed roots" in msg.lower()

    def test_validate_empty_path(self):
        ok, msg, _ = core.validate_path_security("")
        assert ok is False
        ok, _, _ = core.validate_path_security("   ")
        assert ok is False


# ---------- MODEL_ASSIGNMENTS sanity --------------------------------------

class TestModelAssignments:
    def test_mcp_tool_names_present(self):
        for name in ("gemini_quick_query", "gemini_analyze_code", "gemini_codebase_analysis"):
            assert name in core.MODEL_ASSIGNMENTS
            assert core.MODEL_ASSIGNMENTS[name] in core.GEMINI_MODELS

    def test_helper_short_names_present(self):
        for name in ("quick_query", "analyze_code", "analyze_codebase"):
            assert name in core.MODEL_ASSIGNMENTS

    def test_dead_keys_removed(self):
        # pre_edit / pre_commit / session_summary were never exposed by
        # list_tools(); they should no longer be in the dict.
        for dead in ("pre_edit", "pre_commit", "session_summary"):
            assert dead not in core.MODEL_ASSIGNMENTS
