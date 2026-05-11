# Gemini MCP Server

A Model Context Protocol (MCP) server that integrates Google Gemini AI models into Claude Code, enabling dual-AI collaboration.

将 Google Gemini AI 模型集成到 Claude Code 的 MCP 服务器，实现双 AI 协作。

## Why This Exists / 为什么需要这个项目

The original `claude-gemini-mcp-slim` and similar projects have critical issues on **Windows**:

1. `asyncio.create_subprocess_exec` causes pipe transport errors in the MCP event loop (anyio)
2. The Gemini CLI subprocess inherits MCP's stdin, causing both processes to hang
3. `cmd.exe` argument escaping breaks long prompts with newlines
4. Proxy settings (`HTTP_PROXY`/`HTTPS_PROXY`) are not passed to the subprocess

This project solves all of them with:
- **`anyio.to_thread.run_sync` dispatch** — subprocess runs on a worker thread so the MCP event loop never blocks
- **`stdin=DEVNULL` / stdin pipe for long prompts** — short prompts pass via `-p`, prompts >8 KB are piped through a dedicated stdin so we sidestep the Windows ~32 KB CreateProcess limit without ever inheriting MCP's stdin
- **Direct `node gemini.js` invocation** — bypasses `cmd.exe` argument escaping on Windows
- **Full environment inheritance** — proxy, OAuth credentials, home directory all available
- **Per-task timeouts** — quick query 60 s, code analysis 180 s, codebase analysis 300 s; each overridable via env

## Features

- **3 MCP Tools**: quick query, code analysis, codebase analysis
- **Smart model selection**: flash for speed, pro for deep analysis
- **CLI-first, API-fallback**: uses Gemini CLI by default, falls back to Google Generative AI SDK if CLI fails
- **Prompt-injection fences**: user-supplied input is wrapped with `<<<USER_DATA>>>` markers and the model is told to treat the contents as data — no keyword stripping that would mangle code
- **Credential filename denylist**: the codebase scanner skips contents of `.env*` / `*.pem` / `*.key` / `id_rsa*` / `.npmrc` and similar
- **Allowed-roots path policy**: `GEMINI_MCP_ALLOWED_ROOTS` env var declares accessible directories explicitly; falls back to CWD when unset
- **Windows support**: proxy configuration, proper subprocess management

## Prerequisites

- Python 3.10+
- Node.js (for Gemini CLI)
- `@google/gemini-cli` installed globally: `npm install -g @google/gemini-cli`
- Gemini CLI authenticated (run `gemini` and follow OAuth flow)

## Installation

```bash
# Clone the repository
git clone https://github.com/senyamiku1101/gemini-mcp-server.git
cd gemini-mcp-server

# Create virtual environment
python -m venv .venv

# Install dependencies
.venv/Scripts/pip install -r requirements.txt   # Windows
# source .venv/bin/pip install -r requirements.txt  # Linux/macOS
```

## Configuration

Add to your `~/.claude.json` (Claude Code MCP settings):

```json
{
  "mcpServers": {
    "gemini-mcp": {
      "command": "/path/to/.venv/Scripts/python",
      "args": ["/path/to/gemini_mcp_server.py"],
      "env": {
        "GEMINI_FLASH_MODEL": "gemini-2.5-flash",
        "GEMINI_PRO_MODEL": "gemini-2.5-pro",
        "GEMINI_MCP_ALLOWED_ROOTS": "/path/to/projects",
        "HTTP_PROXY": "http://127.0.0.1:7897",
        "HTTPS_PROXY": "http://127.0.0.1:7897"
      },
      "type": "stdio"
    }
  }
}
```

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_FLASH_MODEL` | No | `gemini-2.5-flash` | Model for quick queries |
| `GEMINI_PRO_MODEL` | No | `gemini-2.5-pro` | Model for deep analysis |
| `GOOGLE_API_KEY` | No | - | API key for fallback API calls (optional, CLI is used by default) |
| `GEMINI_MCP_ALLOWED_ROOTS` | No | server CWD | Directories the server may read from (separator: `:` on Linux/macOS, `;` on Windows) |
| `GEMINI_TIMEOUT_QUICK` | No | `60` | Timeout (s) for `gemini_quick_query` |
| `GEMINI_TIMEOUT_ANALYZE` | No | `180` | Timeout (s) for `gemini_analyze_code` |
| `GEMINI_TIMEOUT_CODEBASE` | No | `300` | Timeout (s) for `gemini_codebase_analysis` |
| `GEMINI_CLI_PATH` | No | auto-detected | Override path to gemini.js / gemini executable |
| `HTTP_PROXY` | No | - | HTTP proxy address |
| `HTTPS_PROXY` | No | - | HTTPS proxy address |

## Tools Reference

### `gemini_quick_query`
Quick Q&A using the flash model.
```json
{ "query": "How does async/await work in Python?", "context": "Optional context" }
```

### `gemini_analyze_code`
Deep code analysis using the pro model. Max 80KB / 800 lines.
```json
{ "code_content": "def hello(): ...", "analysis_type": "security" }
```
Analysis types: `comprehensive`, `security`, `performance`, `architecture`

### `gemini_codebase_analysis`
Directory-level analysis using the pro model.
```json
{ "directory_path": "./src", "analysis_scope": "all" }
```
Scopes: `structure`, `security`, `performance`, `patterns`, `all`

## Windows-Specific Notes / Windows 注意事项

### Why call `node gemini.js` directly?
The Gemini CLI npm package installs as a `.cmd` wrapper. On Windows, `cmd /c gemini.cmd -p "long prompt\nwith newlines"` breaks due to `cmd.exe` argument parsing. Calling `node.exe` with the `gemini.js` entry point directly avoids this issue.

### Why `anyio.to_thread.run_sync`?
The MCP Python SDK uses `anyio` for async. On Windows, `asyncio.create_subprocess_exec` causes `ProactorBasePipeTransport` errors in the anyio event loop. This project wraps the blocking `Popen.communicate` in a worker thread via `anyio.to_thread.run_sync` — it sidesteps the proactor issue without introducing the 300 ms tail of a polling loop.

### Why `stdin=DEVNULL` (or a stdin pipe)?
The Gemini CLI subprocess inherits the parent process's stdin by default. Since MCP communicates via stdio, the subprocess would intercept MCP's stdin stream, causing both processes to hang. We always supply a fresh stdin: `DEVNULL` for short prompts, a dedicated pipe (carrying the prompt itself) for prompts above the long-prompt threshold — the latter also dodges the Windows ~32 KB command-line limit that would otherwise break large `gemini_codebase_analysis` calls.

## proxy-wrapper/

An alternative Node.js approach that wraps `@rlabs-inc/gemini-mcp` with proxy support. See [proxy-wrapper/README.md](proxy-wrapper/README.md) for details. This approach is archived — the Python server is the recommended solution.

## Development

```bash
# Install pytest
pip install -r requirements-dev.txt

# Run the test suite
pytest tests/
```

## License

MIT
