# Lily OpenMaintainer

Lily 是一个持续运行、人工审批的多运行时软件维护 Agent 控制台。它可以调度 Codex CLI、Claude Code、OpenAI Responses 或演示执行器，将任务交给规划、实现、审查和验证阶段，保存真实 diff、命令记录、测试证据、Token、费用和运行事件。

填写本地 Git 仓库路径后，Lily 会创建 detached worktree，并按任务选择调用本机已经登录的 Coding Agent。它不会修改原工作区、自动提交、推送、创建 PR、合并代码或部署生产环境。

## 已实现

- 持久化任务队列，服务重启后仍保留任务与执行记录
- 自动后台工作循环，支持暂停、恢复和失败重试
- 复用本机 Codex 登录，无需 OpenAI API Key
- Claude Code 可选运行时，支持保存登录和 `stream-json`
- 任务级运行时选择与自动优先级
- 统一 Runtime Adapter、session、Token 和费用记录
- Claude 配置解析、Provider/模型识别和脱敏运行时诊断
- 每个任务创建独立 Git worktree，原仓库保持不变
- `codex exec --json --ephemeral --sandbox workspace-write` 真实执行
- 规划、实现、审查、验证四阶段 Agent 流程
- 真实 diff、Git 状态、命令记录和测试证据
- OpenAI Responses API 接入与无 Key 演示模式
- 人工审批、驳回和重新排队
- Token 使用统计与实时事件流
- WebSocket 自动刷新运营控制台
- SQLite WAL 模式和原子任务领取，避免重复执行
- 任务租约、运行心跳和崩溃后的自动回收
- 验证结论闸门，未通过验证的任务不能进入审批区
- Codex 子进程环境变量白名单，默认不继承业务密钥
- 单任务重试上限和全局停止开关

## 快速启动

需要 Python 3.9 或更高版本。

```bash
cd lily
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload --port 8010
```

浏览器访问 <http://127.0.0.1:8010>。

不配置 API Key 也可以运行。Lily 会检测本机 Codex CLI 和 Claude Code；任务填写本地 Git 仓库路径时按所选运行时真实执行，没有路径时使用 Responses API 或确定性的演示流程。

## 无 API Key 使用 Codex

Lily 默认查找 ChatGPT 桌面应用附带的 Codex CLI：

```text
/Applications/ChatGPT.app/Contents/Resources/codex
```

先确认本机已经登录：

```bash
codex --version
codex login status
```

创建任务时填写本地 Git 仓库绝对路径，例如：

```text
/Users/you/projects/example-repository
```

Lily 的执行边界：

1. 验证路径位于 `LILY_ALLOWED_REPO_ROOT` 内并且是 Git 仓库。
2. 从仓库当前 `HEAD` 创建 detached worktree。
3. 使用 Codex 保存的登录和 `workspace-write` 沙盒执行任务。
4. 只向 Codex 传递登录和命令执行所需的基础环境变量；额外变量必须通过 `LILY_CODEX_ENV_ALLOWLIST` 显式授权。
5. 收集 JSONL 事件、Token、命令、测试、Git 状态和 diff。
6. 停在人工审批区，不把修改写回原仓库。

## 使用 Claude Code

安装并登录 Claude Code 后，Lily 会自动检测 `claude`：

```bash
npm install -g @anthropic-ai/claude-code
claude
```

也可以在 `.env` 中配置 `LILY_CLAUDE_PATH`。Claude 以非交互 `stream-json` 模式运行，使用 `acceptEdits` 文件编辑权限；可执行工具由 `LILY_CLAUDE_ALLOWED_TOOLS` 精确控制。Lily 不使用 `--dangerously-skip-permissions`。

Lily 会从 `LILY_CLAUDE_CONFIG_PATH`、`CLAUDE_CONFIG_DIR/settings.json` 或 `~/.claude/settings.json` 读取安全元数据，识别 Anthropic、Bedrock、Vertex 或自定义 Anthropic-compatible API。只展示 Provider、模型、API 主机名和配置来源；Token 与完整 API URL 不进入 API、数据库或前端。

也可以手动覆盖展示信息和执行模型：

```dotenv
LILY_CLAUDE_PROVIDER=智谱
LILY_CLAUDE_MODEL=glm-example
```

默认只允许读取、编辑、`git status` 和 `git diff`。项目测试命令必须显式加入，例如：

```dotenv
LILY_CLAUDE_ALLOWED_TOOLS=Read,Glob,Grep,Edit,Write,Bash(git status:*),Bash(git diff:*),Bash(pytest:*),Bash(npm test:*)
```

自动模式默认按 `codex-cli,claude-code` 选择，可通过 `LILY_RUNTIME_PRIORITY` 调整。明确指定的运行时不可用时，任务会失败并说明原因，不会静默切换。

## 可选：启用 OpenAI API

编辑 `.env`：

```dotenv
OPENAI_API_KEY=你的_API_Key
OPENAI_MODEL=gpt-5.4-mini
```

当 Codex CLI 被禁用或任务没有本地仓库路径时，可以使用 Responses API 生成只读维护方案。默认模型适合高频维护任务，也可以按账户权限替换。

Lily 通过 `POST https://api.openai.com/v1/responses` 调用 Responses API。API Key 仅从服务端环境变量读取，不会发送给浏览器或写入数据库。

## 工作流

```text
queued
  -> detached worktree
  -> selected Runtime Adapter
  -> codex exec / claude stream-json
  -> plan / implementation / review / verification
  -> collect diff / tests / JSONL
  -> READY_FOR_HUMAN_REVIEW -> awaiting_approval -> approved | rejected
  -> NEEDS_REVISION -> needs_revision -> retry
```

模型请求失败时，任务会自动回到队列；达到 `LILY_MAX_ATTEMPTS` 后转为 `failed`，必须由人类决定是否重试。运行中的任务按 `LILY_HEARTBEAT_INTERVAL` 刷新租约，超过 `LILY_LEASE_TIMEOUT` 无心跳时会被安全回收。

## 测试

```bash
python -m pytest -q
```

测试覆盖 Responses API 文本解析、Codex 与 Claude JSONL、运行时选择、环境变量隔离、四阶段演示执行、数据库迁移、租约恢复、验证闸门、任务生命周期和暂停状态持久化。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 执行器与循环状态 |
| `GET` | `/api/dashboard` | 指标汇总 |
| `GET` | `/api/runtimes` | 运行时安装与配置诊断 |
| `GET` | `/api/tasks` | 任务列表 |
| `POST` | `/api/tasks` | 创建任务 |
| `POST` | `/api/tasks/{id}/approve` | 人工批准 |
| `POST` | `/api/tasks/{id}/reject` | 人工驳回 |
| `POST` | `/api/tasks/{id}/retry` | 重新排队 |
| `POST` | `/api/control/pause` | 暂停或恢复循环 |
| `GET` | `/api/events` | 运行事件 |
| `WS` | `/ws` | 实时刷新通知 |

## 安全边界

- Lily 主进程不直接执行模型返回的命令；命令由所选 Coding Agent 在隔离 worktree 中执行
- Codex 命令运行在 `workspace-write` 沙盒中，网络默认关闭
- 只写入 detached worktree，不写入原仓库工作区
- 默认不创建或合并 Pull Request
- 默认不接触生产密钥和部署环境
- 每个 Runtime Adapter 使用独立环境变量白名单
- API Key 不进入前端、SQLite 或日志
- 只有通过验证闸门的结果可以进入人工审批

下一阶段应增加工作树清理、patch 导出、可配置验证命令、任务取消和 GitHub App。即使增加这些能力，自动合并与生产部署也应保持关闭。
