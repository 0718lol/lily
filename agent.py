from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx


SYSTEM_PROMPT = """你是 Lily OpenMaintainer 的软件维护智能体。
你的输出会进入人工审批区，不会被自动应用。你必须：
1. 只处理用户明确描述的仓库和任务，不猜测密钥或访问未授权资源；
2. 优先提出小范围、可测试、可回滚的修改；
3. 不声称运行过实际未运行的命令或测试；
4. 清楚区分已知事实、假设和建议；
5. 遇到高风险、信息不足或需要生产权限的操作时明确停止并请求人工确认。
使用简洁中文输出，代码、文件路径和命令保持原样。"""


STAGES = (
    (
        "plan",
        "规划 Agent",
        "分析任务目标、约束、风险和验收条件。给出最多 6 步的执行计划，并列出需要确认的假设。",
    ),
    (
        "implementation",
        "实现 Agent",
        "基于规划提出具体实现方案。列出可能修改的文件、关键代码或统一 diff 草案，并给出验证命令。不要声称已经实际执行。",
    ),
    (
        "review",
        "审查 Agent",
        "以严格代码审查者身份检查方案，优先寻找正确性、安全性、兼容性和测试缺口。按严重程度输出问题；没有阻塞项时明确说明。",
    ),
    (
        "verification",
        "验证 Agent",
        "依据任务描述、计划、实现方案和审查结果给出最终验证清单。结论必须是 READY_FOR_HUMAN_REVIEW 或 NEEDS_REVISION，并解释原因。",
    ),
)


@dataclass
class StageResult:
    key: str
    label: str
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


def extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts: list[str] = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "\n\n".join(parts).strip()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


async def run_process(
    *args: str,
    cwd: Path | None = None,
    timeout: float = 60,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        raise RuntimeError(f"命令执行超过 {int(timeout)} 秒，已终止") from exc
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


class OpenAIResponsesClient:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def respond(self, prompt: str) -> tuple[str, int, int]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": prompt,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=body,
            )
        if response.is_error:
            detail = response.text[:800]
            raise RuntimeError(f"OpenAI API {response.status_code}: {detail}")
        payload = response.json()
        text = extract_output_text(payload)
        if not text:
            raise RuntimeError("模型响应中没有可用文本")
        usage = payload.get("usage") or {}
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


class CodexCliExecutor:
    def __init__(
        self,
        codex_path: str,
        allowed_repo_root: Path,
        worktree_root: Path,
        schema_path: Path,
        timeout: int,
    ):
        self.codex_path = codex_path
        self.allowed_repo_root = allowed_repo_root.resolve()
        self.worktree_root = worktree_root
        self.schema_path = schema_path
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.codex_path and Path(self.codex_path).is_file())

    async def run(self, task: dict[str, Any]) -> AsyncIterator[StageResult]:
        repo = await self._resolve_repo(task["repository_path"])
        worktree = await self._create_worktree(repo, task)
        result_file = self.worktree_root / f"codex-last-message-{task['id']}.json"
        prompt = self._prompt(task)
        command = [
            self.codex_path,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(worktree),
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(result_file),
            prompt,
        ]
        environment = dict(os.environ)
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("CODEX_API_KEY", None)
        code, stdout, stderr = await run_process(
            *command,
            cwd=worktree,
            timeout=self.timeout,
            env=environment,
        )
        events = self._parse_events(stdout)
        execution_log = self._execution_log(events, stderr, code)
        if code != 0:
            detail = execution_log[-4000:] or f"codex exec 退出码 {code}"
            raise RuntimeError(detail)

        structured = self._read_result(result_file, events)
        try:
            result_file.unlink()
        except OSError:
            pass
        diff, status, diff_check = await self._collect_diff(worktree)
        usage = self._usage(events)
        thread_id = self._thread_id(events)

        plan = "\n".join(
            f"{index}. {item}" for index, item in enumerate(structured.get("plan", []), 1)
        ) or "Codex 未返回独立计划。"
        yield StageResult(
            "plan",
            "Codex 规划",
            plan,
            metadata={
                "executor_mode": "codex-cli",
                "worktree_path": str(worktree),
                "codex_session_id": thread_id,
            },
        )

        files = structured.get("files_changed", [])
        implementation = structured.get("summary", "Codex 未返回实现摘要。")
        if files:
            implementation += "\n\n### 修改文件\n" + "\n".join(f"- `{item}`" for item in files)
        implementation += f"\n\n### Git 状态\n```text\n{status or '工作树无变化'}\n```"
        yield StageResult(
            "implementation",
            "Codex 实现",
            implementation,
            metadata={"diff": diff, "execution_log": execution_log},
        )

        findings = structured.get("review_findings", [])
        review = "\n".join(f"- {item}" for item in findings) or "Codex 未报告阻塞性审查问题。"
        yield StageResult("review", "Codex 审查", review)

        tests = structured.get("tests", [])
        test_output = self._test_output(tests, events, diff_check)
        verdict = structured.get("verification_status", "NEEDS_REVISION")
        if not diff.strip():
            verdict = "NEEDS_REVISION"
        verification = f"{verdict}\n\n{structured.get('verification_notes', '')}".strip()
        yield StageResult(
            "verification",
            "Codex 验证",
            verification,
            input_tokens=usage[0],
            output_tokens=usage[1],
            metadata={"test_output": test_output},
        )

    async def _resolve_repo(self, value: str) -> Path:
        if not value.strip():
            raise ValueError("真实执行需要填写本地仓库路径")
        requested = Path(value).expanduser().resolve(strict=True)
        if not is_within(requested, self.allowed_repo_root):
            raise ValueError(f"仓库必须位于允许目录 {self.allowed_repo_root} 内")
        code, stdout, stderr = await run_process(
            "git", "-C", str(requested), "rev-parse", "--show-toplevel", timeout=20
        )
        if code != 0:
            raise ValueError(f"路径不是 Git 仓库：{stderr.strip() or requested}")
        root = Path(stdout.strip()).resolve()
        if not is_within(root, self.allowed_repo_root):
            raise ValueError("Git 仓库根目录不在允许范围内")
        return root

    async def _create_worktree(self, repo: Path, task: dict[str, Any]) -> Path:
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex[:6]
        worktree = self.worktree_root / f"{task['id']}-a{task['attempts']}-{suffix}"
        code, _, stderr = await run_process(
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "--detach",
            str(worktree),
            "HEAD",
            timeout=60,
        )
        if code != 0:
            raise RuntimeError(f"创建临时 Git 工作树失败：{stderr.strip()}")
        return worktree

    async def _collect_diff(self, worktree: Path) -> tuple[str, str, str]:
        await run_process("git", "-C", str(worktree), "add", "-N", ".", timeout=30)
        _, diff, _ = await run_process(
            "git", "-C", str(worktree), "diff", "--no-ext-diff", "--unified=3", "--", timeout=60
        )
        _, status, _ = await run_process(
            "git", "-C", str(worktree), "status", "--short", timeout=30
        )
        check_code, check_out, check_err = await run_process(
            "git", "-C", str(worktree), "diff", "--check", timeout=30
        )
        check = "PASS" if check_code == 0 else (check_out + check_err).strip()
        return diff[:200000], status.strip(), check[:10000]

    def _prompt(self, task: dict[str, Any]) -> str:
        return f"""你正在 Lily 创建的隔离 Git worktree 中执行维护任务。

任务标题：{task['title']}
风险等级：{task['risk']}
Issue：{task.get('issue_url') or '未提供'}
任务描述：
{task['description']}

要求：
1. 先阅读仓库说明、AGENTS.md、相关代码和现有测试。
2. 只做完成任务所需的最小修改，不修改无关文件。
3. 在当前 worktree 内直接编辑文件；不要提交、推送、创建 PR 或访问生产环境。
4. 运行仓库已有的聚焦测试和静态检查。网络不可用时，不要尝试安装依赖。
5. 自己审查最终 diff，明确说明真实运行过的命令、退出状态和剩余风险。
6. 如果需求不安全、信息不足或无法验证，停止扩大修改并返回 NEEDS_REVISION。
7. 最终严格按照输出 schema 返回结构化结果。
"""

    @staticmethod
    def _parse_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    @staticmethod
    def _read_result(path: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        for event in reversed(events):
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                try:
                    return json.loads(item.get("text", ""))
                except json.JSONDecodeError:
                    continue
        raise RuntimeError("Codex 未生成有效的结构化最终结果")

    @staticmethod
    def _thread_id(events: list[dict[str, Any]]) -> str:
        for event in events:
            if event.get("type") == "thread.started":
                return str(event.get("thread_id", ""))
        return ""

    @staticmethod
    def _usage(events: list[dict[str, Any]]) -> tuple[int, int]:
        for event in reversed(events):
            usage = event.get("usage") or {}
            if usage:
                return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        return 0, 0

    @staticmethod
    def _command_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        for event in events:
            item = event.get("item") or {}
            if item.get("type") == "command_execution" and event.get("type") == "item.completed":
                commands.append(item)
        return commands

    def _execution_log(self, events: list[dict[str, Any]], stderr: str, exit_code: int) -> str:
        lines: list[str] = []
        for item in self._command_events(events):
            command = item.get("command", "")
            status = item.get("status", "completed")
            exit_code = item.get("exit_code")
            lines.append(f"$ {command}\nstatus={status} exit_code={exit_code}")
            output = item.get("aggregated_output") or item.get("output") or ""
            if output:
                lines.append(str(output)[-3000:])
        if exit_code != 0 and stderr.strip():
            lines.append("[codex stderr]\n" + stderr[-5000:])
        return "\n\n".join(lines)[-50000:]

    def _test_output(
        self,
        tests: list[dict[str, Any]],
        events: list[dict[str, Any]],
        diff_check: str,
    ) -> str:
        lines = [f"git diff --check: {diff_check}"]
        for test in tests:
            lines.append(
                f"$ {test.get('command', 'unknown')}\n"
                f"status={test.get('status', 'unknown')}\n"
                f"{test.get('output', '')}".strip()
            )
        if len(lines) == 1:
            for item in self._command_events(events):
                command = str(item.get("command", ""))
                if any(token in command for token in ("test", "pytest", "ruff", "lint", "check")):
                    lines.append(
                        f"$ {command}\nstatus={item.get('status', 'completed')} "
                        f"exit_code={item.get('exit_code')}"
                    )
        return "\n\n".join(lines)[:50000]


class TaskExecutor:
    def __init__(self, settings: Any):
        self.model = settings.openai_model
        self.client = (
            OpenAIResponsesClient(settings.openai_api_key, settings.openai_model)
            if settings.openai_api_key
            else None
        )
        self.codex = CodexCliExecutor(
            settings.codex_path,
            settings.allowed_repo_root,
            settings.worktree_root,
            settings.root / "codex-result.schema.json",
            settings.codex_timeout,
        ) if settings.codex_enabled else None

    @property
    def mode(self) -> str:
        if self.codex and self.codex.available:
            return "codex-cli"
        return "openai" if self.client else "demo"

    @property
    def model_label(self) -> str:
        return "Codex saved login" if self.mode == "codex-cli" else self.model

    def resolve_mode(self, task: dict[str, Any]) -> str:
        if task.get("repository_path") and self.codex and self.codex.available:
            return "codex-cli"
        if self.client:
            return "openai"
        return "demo"

    async def run(self, task: dict[str, Any]) -> AsyncIterator[StageResult]:
        if self.resolve_mode(task) == "codex-cli":
            async for result in self.codex.run(task):
                yield result
            return

        context: dict[str, str] = {}
        for key, label, instruction in STAGES:
            prompt = self._build_prompt(task, instruction, context)
            if self.client:
                content, input_tokens, output_tokens = await self.client.respond(prompt)
            else:
                await asyncio.sleep(0.45)
                content = self._demo_response(key, task)
                input_tokens = max(80, len(prompt) // 3)
                output_tokens = max(60, len(content) // 3)
            context[key] = content
            yield StageResult(key, label, content, input_tokens, output_tokens)

    def _build_prompt(
        self,
        task: dict[str, Any],
        instruction: str,
        context: dict[str, str],
    ) -> str:
        prior = "\n\n".join(
            f"## {name}\n{content}" for name, content in context.items()
        ) or "暂无前序阶段输出。"
        return f"""# 当前任务
标题：{task['title']}
仓库：{task.get('repository') or '未提供'}
Issue：{task.get('issue_url') or '未提供'}
风险等级：{task.get('risk', 'low')}
任务描述：
{task['description']}

# 前序阶段
{prior}

# 本阶段要求
{instruction}
"""

    def _demo_response(self, key: str, task: dict[str, Any]) -> str:
        title = task["title"]
        repository = task.get("repository") or "目标仓库"
        if key == "plan":
            return f"""### 目标
在不扩大修改范围的前提下完成“{title}”，并保留清晰的验证证据。

### 执行计划
1. 阅读 `{repository}` 的项目说明、目录结构和现有测试约定。
2. 将需求拆成可独立验证的最小修改单元。
3. 补充或调整实现，并避免改变无关公共接口。
4. 添加覆盖正常路径、边界情况和失败路径的测试。
5. 运行项目既有的格式检查、静态检查和测试命令。
6. 汇总 diff、验证结果与剩余风险，交给人工审批。

### 待确认假设
- 当前为演示模式，尚未读取或修改真实仓库。
- 填写本地 Git 仓库路径后，将自动切换到 Codex CLI 真实执行。"""
        if key == "implementation":
            return f"""### 建议修改
- 定位与“{title}”直接相关的模块，保持单一职责。
- 对外部输入执行类型、长度和权限校验。
- 为成功、失败与边界路径补充自动化测试。

### 补丁策略
当前没有本地仓库路径，因此不生成伪造 diff。"""
        if key == "review":
            return """### 审查结论
存在阻塞项：当前未提供本地 Git 仓库路径，无法生成或验证真实修改。"""
        return """NEEDS_REVISION

规划流程完整，但没有真实仓库上下文。填写本地仓库路径后重新创建任务。"""
