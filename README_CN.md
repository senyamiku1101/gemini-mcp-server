# Gemini MCP Server

将 Google Gemini AI 模型集成到 Claude Code 的 MCP 服务器，实现双 AI 协作。

## 为什么需要这个项目

类似的 `claude-gemini-mcp-slim` 等项目在 **Windows** 上存在以下致命问题：

1. `asyncio.create_subprocess_exec` 在 MCP 事件循环（anyio）中导致管道传输错误
2. Gemini CLI 子进程继承了 MCP 的 stdin，导致两个进程互相卡死
3. `cmd.exe` 的参数转义机制会破坏包含换行符的长 prompt
4. 代理设置（`HTTP_PROXY`/`HTTPS_PROXY`）没有传递给子进程

本项目的解决方案：

- **`anyio.to_thread.run_sync` 分发** — 子进程在工作线程中执行，MCP 事件循环不阻塞
- **`stdin=DEVNULL` / 长 prompt 走 stdin** — 防止子进程抢占 MCP 的 stdin；超过 8KB 的 prompt 自动改走 stdin，绕开 Windows 32KB argv 上限
- **直接调用 `node gemini.js`** — 绕过 Windows 下 `cmd.exe` 的参数转义问题
- **完整环境变量继承** — 代理、OAuth 凭据、用户目录等全部可用
- **任务分级 timeout** — 快速问答 60s、代码分析 180s、代码库分析 300s，均可通过环境变量覆盖

## 功能特性

- **3 个 MCP 工具**：快速问答、代码分析、代码库分析
- **智能模型选择**：flash 模型用于快速响应，pro 模型用于深度分析
- **CLI 优先、API 回退**：默认使用 Gemini CLI，CLI 失败时回退到 Google Generative AI SDK
- **数据围栏 prompt**：用 `<<<USER_DATA>>>` 标记将用户输入与指令隔离，对抗 prompt injection 而不破坏代码/Markdown
- **凭据文件名 denylist**：扫描时跳过 `.env*` / `*.pem` / `*.key` / `id_rsa*` / `.npmrc` 等文件内容
- **allowed-roots 路径策略**：通过 `GEMINI_MCP_ALLOWED_ROOTS` 显式声明可读目录，未设置时回退到 CWD
- **Windows 支持**：代理配置、正确的子进程管理

## 前置要求

- Python 3.10+
- Node.js（运行 Gemini CLI）
- 全局安装 `@google/gemini-cli`：`npm install -g @google/gemini-cli`
- Gemini CLI 已完成 OAuth 认证（运行 `gemini` 按提示完成登录）

## 安装

```bash
# 克隆仓库
git clone https://github.com/senyamiku1101/gemini-mcp-server.git
cd gemini-mcp-server

# 创建虚拟环境
python -m venv .venv

# 安装依赖
.venv/Scripts/pip install -r requirements.txt   # Windows
# source .venv/bin/pip install -r requirements.txt  # Linux/macOS
```

## 配置

在 `~/.claude.json` 中添加 MCP 服务器配置：

```json
{
  "mcpServers": {
    "gemini-mcp": {
      "command": "虚拟环境中 python 的完整路径",
      "args": ["gemini_mcp_server.py 的完整路径"],
      "env": {
        "GEMINI_FLASH_MODEL": "gemini-2.5-flash",
        "GEMINI_PRO_MODEL": "gemini-2.5-pro",
        "GEMINI_MCP_ALLOWED_ROOTS": "你的项目根目录路径",
        "HTTP_PROXY": "http://127.0.0.1:7897",
        "HTTPS_PROXY": "http://127.0.0.1:7897"
      },
      "type": "stdio"
    }
  }
}
```

### 环境变量说明

| 变量 | 是否必需 | 默认值 | 说明 |
|---|---|---|---|
| `GEMINI_FLASH_MODEL` | 否 | `gemini-2.5-flash` | 快速问答使用的模型 |
| `GEMINI_PRO_MODEL` | 否 | `gemini-2.5-pro` | 深度分析使用的模型 |
| `GOOGLE_API_KEY` | 否 | - | API Key，CLI 失败时的回退方案（可选，默认使用 CLI） |
| `GEMINI_MCP_ALLOWED_ROOTS` | 否 | 服务器 CWD | 允许读取的目录（分隔符：Linux/macOS 用 `:`，Windows 用 `;`） |
| `GEMINI_TIMEOUT_QUICK` | 否 | `60` | `gemini_quick_query` 超时秒数 |
| `GEMINI_TIMEOUT_ANALYZE` | 否 | `180` | `gemini_analyze_code` 超时秒数 |
| `GEMINI_TIMEOUT_CODEBASE` | 否 | `300` | `gemini_codebase_analysis` 超时秒数 |
| `GEMINI_CLI_PATH` | 否 | 自动检测 | 手动指定 gemini.js / gemini 可执行文件路径 |
| `HTTP_PROXY` | 否 | - | HTTP 代理地址 |
| `HTTPS_PROXY` | 否 | - | HTTPS 代理地址 |

## 工具说明

### `gemini_quick_query` — 快速问答
使用 flash 模型快速回答问题。
```json
{ "query": "Python 的 async/await 是怎么工作的？", "context": "可选的上下文信息" }
```

### `gemini_analyze_code` — 代码分析
使用 pro 模型对代码片段进行深度分析。最大支持 80KB / 800 行。
```json
{ "code_content": "def hello(): ...", "analysis_type": "security" }
```
分析类型：`comprehensive`（综合）、`security`（安全）、`performance`（性能）、`architecture`（架构）

### `gemini_codebase_analysis` — 代码库分析
使用 pro 模型对整个目录进行分析。
```json
{ "directory_path": "./src", "analysis_scope": "all" }
```
分析范围：`structure`（结构）、`security`（安全）、`performance`（性能）、`patterns`（模式）、`all`（全部）

## Windows 注意事项

### 为什么直接调用 `node gemini.js`？
Gemini CLI 通过 npm 安装后会生成 `.cmd` 包装器。在 Windows 上，`cmd /c gemini.cmd -p "包含\n换行的长 prompt"` 会因为 `cmd.exe` 的参数解析机制而失败。直接用 `node.exe` 调用 `gemini.js` 入口文件可以避免此问题。

### 为什么用 `anyio.to_thread.run_sync`？
MCP Python SDK 使用 `anyio` 作为异步后端。在 Windows 上，`asyncio.create_subprocess_exec` 会在 anyio 事件循环中引发 `ProactorBasePipeTransport` 错误。本项目把阻塞的 `Popen.communicate` 包到一个工作线程里通过 `anyio.to_thread.run_sync` 等待——既避开 ProactorBasePipeTransport，又不会损失短 prompt 的延迟。

### 为什么需要 `stdin=DEVNULL` 或 stdin 路径？
Gemini CLI 子进程默认继承父进程的 stdin。而 MCP 通过 stdio 进行通信，子进程会截获 MCP 的 stdin 流，导致两个进程同时卡死。短 prompt 时显式 `stdin=DEVNULL`；长 prompt（>8KB）时则创建独立 stdin 管道把 prompt 喂进去，同时绕开 Windows CreateProcess 的 32KB 命令行上限。

## proxy-wrapper/

一种备选的 Node.js 方案，通过包装 `@rlabs-inc/gemini-mcp` 实现代理支持。详见 [proxy-wrapper/README.md](proxy-wrapper/README.md)。此方案已归档，推荐使用 Python 服务器方案。

## 开发

```bash
# 安装开发依赖（含 pytest）
pip install -r requirements-dev.txt

# 运行测试
pytest tests/
```

## 致谢

- 参考项目：[claude-gemini-mcp-slim](https://github.com/cmdaltctr/claude-gemini-mcp-slim)
- MCP 协议：[Model Context Protocol](https://modelcontextprotocol.io/)
- Gemini CLI：[@google/gemini-cli](https://www.npmjs.com/package/@google/gemini-cli)

## 许可证

MIT
