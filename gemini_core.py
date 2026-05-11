"""Shared primitives for the Gemini MCP server and CLI helper.

Both ``gemini_mcp_server.py`` and ``gemini_helper.py`` import from here so
constants, sanitisers, the credential redactor, the path-validation rules,
the codebase scanner, the CLI resolver, and the subprocess launcher all live
in exactly one place.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------- Limits ----------------------------------------------------------

MAX_FILE_SIZE = 81920  # 80 KB — single-file analysis cap
MAX_LINES = 800

# Prompts longer than this are piped through stdin instead of -p, to stay
# clear of the Windows CreateProcess ~32 KB command-line limit.
LONG_PROMPT_THRESHOLD = 8000


# ---------- Model configuration --------------------------------------------

GEMINI_MODELS = {
    "flash": os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
    "pro": os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro"),
}

# Maps task type -> model tier. The MCP tool names and the CLI helper's
# shorter names both live here so neither caller needs to translate.
MODEL_ASSIGNMENTS = {
    # MCP tool names
    "gemini_quick_query": "flash",
    "gemini_analyze_code": "pro",
    "gemini_codebase_analysis": "pro",
    # CLI helper short names
    "quick_query": "flash",
    "analyze_code": "pro",
    "analyze_codebase": "pro",
}


# ---------- Scanner: ignore lists ------------------------------------------

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".idea", ".vscode", ".next", ".nuxt",
    "target", "bin", "obj", ".tox", ".mypy_cache", ".pytest_cache",
}
IGNORED_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".class", ".jar",
    ".woff", ".woff2", ".ttf", ".eot", ".ico",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg",
    ".mp4", ".mp3", ".wav", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".rar", ".7z", ".pdf",
    ".db", ".sqlite", ".lock", ".wasm",
}

# Filename patterns that commonly hold credentials. Matching files appear in
# the directory tree but their contents are never sent to the model.
SECRET_BASENAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", ".pgpass",
    "credentials", "credentials.json", "secrets.json", "secret.json",
    "id_rsa", "id_dsa", "id_ed25519", "id_ecdsa",
}
SECRET_SUFFIXES = (
    ".pem", ".key", ".pfx", ".p12", ".cer", ".crt",
    ".keystore", ".jks", ".asc",
)


def is_secret_filename(name: str) -> bool:
    """Whether a filename looks like it may contain credentials."""
    lower = name.lower()
    if lower in SECRET_BASENAMES:
        return True
    if lower.startswith(".env."):  # .env.local, .env.production, etc.
        return True
    return lower.endswith(SECRET_SUFFIXES)


# ---------- Credential redaction --------------------------------------------

_REDACT_PATTERNS = [
    (re.compile(r"AIzaSy[A-Za-z0-9_-]{25,}"), "[API_KEY_REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_-]{32,}"), "[API_KEY_REDACTED]"),
    (re.compile(r"Bearer [A-Za-z0-9_.-]{10,}"), "[TOKEN_REDACTED]"),
]


def redact(text: str) -> str:
    """Strip API keys and bearer tokens from arbitrary text."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------- Sanitisation ---------------------------------------------------

def sanitize_for_prompt(text: str, max_length: int = 100000) -> str:
    """Cap length and strip control chars from text destined for a prompt.

    We deliberately do NOT scrub instruction-like keywords (``###``, ``---``,
    triple backticks, ``system:`` etc.) — that approach mangles legitimate
    code, Markdown, and diffs. Callers should fence untrusted text with a
    clear data boundary so the model treats it as data, not instructions.
    """
    if not isinstance(text, str):
        return ""
    if len(text) > max_length:
        text = text[:max_length]
    # Strip NUL and ESC — never legitimate in source, can corrupt logs/terminal.
    return text.replace("\x00", "").replace("\x1b", "")


# ---------- Path validation ------------------------------------------------

def get_allowed_roots() -> List[Path]:
    """Return the directories this process may read from.

    Reads ``GEMINI_MCP_ALLOWED_ROOTS`` (separator = ``os.pathsep``). Falls
    back to the current working directory when unset, which keeps backwards
    compatibility for users who haven't migrated their config.
    """
    env_val = os.environ.get("GEMINI_MCP_ALLOWED_ROOTS")
    if not env_val:
        return [Path.cwd().resolve()]
    roots: List[Path] = []
    for token in env_val.split(os.pathsep):
        token = token.strip()
        if not token:
            continue
        try:
            roots.append(Path(token).expanduser().resolve())
        except Exception:
            logger.warning(
                f"Skipping invalid GEMINI_MCP_ALLOWED_ROOTS entry: {token!r}"
            )
    return roots or [Path.cwd().resolve()]


def path_within_allowed(resolved_path: Path) -> bool:
    """True iff ``resolved_path`` lives inside one of the allowed roots."""
    for root in get_allowed_roots():
        try:
            resolved_path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def validate_path_security(
    file_path: str,
) -> Tuple[bool, str, Optional[Path]]:
    """Validate that ``file_path`` resolves inside an allowed root.

    Returns ``(ok, message, resolved_path_or_None)``. ``Path.resolve()``
    neutralises ``..`` traversal, so a path that escapes every allowed root
    is rejected regardless of how it got written.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return False, "Invalid path", None
    try:
        resolved_path = Path(file_path).expanduser().resolve()
    except Exception as e:
        return False, f"Path validation error: {e}", None

    if path_within_allowed(resolved_path):
        return True, "Valid path", resolved_path

    roots_display = ", ".join(str(r) for r in get_allowed_roots())
    return (
        False,
        f"Path outside allowed roots ({roots_display}). Set "
        "GEMINI_MCP_ALLOWED_ROOTS to expand the server's reach.",
        None,
    )


# ---------- Codebase scanning ----------------------------------------------

def build_codebase_context(
    root_path: Path,
    max_chars: int = 100000,
    max_tree_entries: int = 2000,
) -> str:
    """Build a tree + contents snapshot of ``root_path``.

    - Prunes ``IGNORED_DIRS`` in-place during walk (doesn't recurse into them).
    - Skips reading files whose names match a credential pattern; they still
      appear in the tree with a ``[redacted]`` marker.
    - Caps both content size (``max_chars``) and tree size
      (``max_tree_entries``) so large repos can't blow the prompt budget.
    """
    tree_lines: List[str] = []
    content_parts: List[str] = []
    current_chars = 0
    tree_truncated = False

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune in-place so os.walk never descends into ignored subtrees.
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        filenames = sorted(filenames)

        rel_dir = os.path.relpath(dirpath, root_path).replace(os.sep, "/")
        if rel_dir != ".":
            if len(tree_lines) >= max_tree_entries:
                tree_truncated = True
                break
            tree_lines.append(rel_dir + "/")

        for fname in filenames:
            if len(tree_lines) >= max_tree_entries:
                tree_truncated = True
                break
            rel_path = fname if rel_dir == "." else f"{rel_dir}/{fname}"
            tree_lines.append(rel_path)

            full_path = Path(dirpath) / fname

            if is_secret_filename(fname):
                content_parts.append(
                    f"\n--- {rel_path} ---\n"
                    "[content redacted: filename matches credential pattern]\n"
                )
                continue

            if full_path.suffix.lower() in IGNORED_EXTENSIONS:
                continue
            if current_chars >= max_chars:
                continue

            try:
                text = full_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if len(text) > 20000:
                text = text[:20000] + "\n... [truncated]"
            block = f"\n--- {rel_path} ---\n{text}\n"
            if current_chars + len(block) <= max_chars:
                content_parts.append(block)
                current_chars += len(block)

        if tree_truncated:
            break

    if tree_truncated:
        tree_lines.append("... [tree truncated]")

    tree_str = "\n".join(tree_lines)
    content_str = "".join(content_parts)
    return f"File Structure:\n{tree_str}\n\nFile Contents:{content_str}"


# ---------- CLI resolution -------------------------------------------------

def resolve_gemini_cli() -> List[str]:
    """Resolve the Gemini CLI command across platforms.

    Returns either ``[executable]`` (Linux/macOS) or ``[node_exe, gemini_js]``
    (Windows — avoids the .cmd wrapper, which mangles long/multiline args).
    Raises ``RuntimeError`` with a setup hint when nothing is found.
    """
    # 1) Explicit override wins.
    cli_path = os.environ.get("GEMINI_CLI_PATH")
    if cli_path and os.path.exists(cli_path):
        return [cli_path]

    is_windows = platform.system() == "Windows"

    def _resolve_in(npm_dir: str) -> Optional[List[str]]:
        gemini_js = os.path.join(
            npm_dir, "node_modules", "@google", "gemini-cli", "bundle", "gemini.js"
        )
        if not os.path.exists(gemini_js):
            return None
        node_exe = None
        if is_windows:
            candidate = os.path.join(npm_dir, "node.exe")
            if os.path.exists(candidate):
                node_exe = candidate
        node_exe = node_exe or shutil.which("node") or "node"
        return [node_exe, gemini_js]

    # 2) PATH lookup; on Windows resolve to node + gemini.js if possible.
    found = shutil.which("gemini")
    if found:
        if is_windows:
            resolved = _resolve_in(os.path.dirname(found))
            if resolved:
                return resolved
            # Keep searching; fall back to .cmd only as last resort.
        else:
            return [found]

    # 3) npm prefix lookup — covers global installs on every platform.
    npm_dirs: List[str] = []
    if is_windows:
        for var in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                npm_dirs.append(os.path.join(base, "npm"))

    # On Windows the executable is npm.cmd; shutil.which finds either.
    npm_exe = shutil.which("npm") or (shutil.which("npm.cmd") if is_windows else None)
    if npm_exe:
        try:
            npm_prefix = subprocess.check_output(
                [npm_exe, "config", "get", "prefix"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).strip()
            if npm_prefix:
                npm_dirs.append(npm_prefix)
                # *nix globals live under <prefix>/lib/node_modules.
                if not is_windows:
                    npm_dirs.append(os.path.join(npm_prefix, "lib"))
        except Exception as e:
            logger.debug(f"npm prefix lookup failed: {e}")

    for npm_dir in npm_dirs:
        resolved = _resolve_in(npm_dir)
        if resolved:
            return resolved

    # 4) Last resort on Windows: use the .cmd wrapper. Works for short prompts;
    #    long prompts will fail, but the caller's API fallback can recover.
    if is_windows and found:
        logger.warning(
            "Falling back to gemini.cmd wrapper; long prompts may fail. "
            "Reinstall @google/gemini-cli or set GEMINI_CLI_PATH to gemini.js."
        )
        return [found]

    raise RuntimeError(
        "Could not locate Gemini CLI. Install with "
        "`npm install -g @google/gemini-cli`, or set GEMINI_CLI_PATH "
        "to the gemini.js path."
    )


# ---------- Subprocess execution -------------------------------------------

def run_gemini_subprocess(
    cmd_args: List[str],
    env: Dict[str, str],
    timeout: int,
    stdin_data: Optional[bytes],
) -> Dict[str, Any]:
    """Run the Gemini CLI subprocess synchronously.

    Designed to be dispatched via ``anyio.to_thread.run_sync`` from async
    code so the MCP event loop stays responsive. Returns a dict with raw
    stdout/stderr bytes, the return code, and a ``timed_out`` flag. The
    subprocess is always killed and drained on timeout to avoid orphans.
    """
    stdin_param = subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd_args,
        stdin=stdin_param,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    logger.info(f"Subprocess started: PID={proc.pid}, timeout={timeout}s")
    try:
        out, err = proc.communicate(input=stdin_data, timeout=timeout)
        return {
            "stdout": out,
            "stderr": err,
            "returncode": proc.returncode,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = b"", b""
        return {
            "stdout": out,
            "stderr": err,
            "returncode": -1,
            "timed_out": True,
        }
