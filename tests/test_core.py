import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent import (
    ClaudeCodeExecutor,
    CodexCliExecutor,
    TaskExecutor,
    claude_environment,
    codex_environment,
    extract_output_text,
    parse_json_object,
    validate_maintenance_result,
)
from database import Database, LeaseLostError
from config import claude_profile


class ResponseParsingTests(unittest.TestCase):
    def test_extracts_response_output_text(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "first"},
                        {"type": "output_text", "text": "second"},
                    ],
                }
            ]
        }
        self.assertEqual(extract_output_text(payload), "first\n\nsecond")

    def test_demo_executor_runs_all_stages(self):
        task = {
            "title": "补充配置测试",
            "description": "为配置解析器增加空值和非法输入测试。",
            "repository": "demo/project",
            "risk": "low",
        }

        runtime = SimpleNamespace(
            openai_api_key="",
            openai_model="demo",
            codex_path="",
            codex_enabled=False,
            allowed_repo_root=Path(tempfile.gettempdir()),
            worktree_root=Path(tempfile.gettempdir()) / "lily-test-worktrees",
            root=Path(tempfile.gettempdir()),
            codex_timeout=30,
        )

        async def collect():
            return [result async for result in TaskExecutor(runtime).run(task)]

        results = asyncio.run(collect())
        self.assertEqual([result.key for result in results], ["plan", "implementation", "review", "verification"])
        self.assertTrue(all(result.content for result in results))

    def test_parses_codex_jsonl_usage(self):
        raw = "\n".join([
            '{"type":"thread.started","thread_id":"thread-1"}',
            '{"type":"turn.completed","usage":{"input_tokens":120,"output_tokens":30}}',
        ])
        events = CodexCliExecutor._parse_events(raw)
        self.assertEqual(CodexCliExecutor._thread_id(events), "thread-1")
        self.assertEqual(CodexCliExecutor._usage(events), (120, 30))

    def test_codex_environment_uses_allowlist(self):
        source = {
            "HOME": "/tmp/home",
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "LILY_TEST_VALUE": "allowed",
        }
        environment = codex_environment(source, ("LILY_TEST_VALUE",))
        self.assertEqual(
            environment,
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "LILY_TEST_VALUE": "allowed",
            },
        )

    def test_execution_log_keeps_process_failure_stderr(self):
        executor = CodexCliExecutor(
            "/tmp/codex",
            Path("/tmp"),
            Path("/tmp/worktrees"),
            Path("/tmp/schema.json"),
            30,
        )
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "true",
                    "status": "completed",
                    "exit_code": 0,
                },
            }
        ]
        log = executor._execution_log(events, "fatal process error", 1)
        self.assertIn("[codex stderr]", log)
        self.assertIn("fatal process error", log)

    def test_parses_and_validates_claude_result(self):
        value = {
            "plan": ["inspect", "change"],
            "summary": "updated parser",
            "files_changed": ["parser.py"],
            "review_findings": [],
            "verification_status": "READY_FOR_HUMAN_REVIEW",
            "verification_notes": "tests passed",
            "tests": [],
        }
        events = [
            {
                "type": "result",
                "session_id": "claude-session",
                "result": f"```json\n{__import__('json').dumps(value)}\n```",
                "usage": {"input_tokens": 80, "output_tokens": 20},
                "total_cost_usd": 0.04,
            }
        ]
        parsed = ClaudeCodeExecutor._claude_result(events)
        self.assertEqual(validate_maintenance_result(parsed), value)
        self.assertEqual(
            ClaudeCodeExecutor._claude_usage(events),
            (80, 20, 0.04),
        )
        self.assertEqual(
            ClaudeCodeExecutor._claude_session_id(events),
            "claude-session",
        )

    def test_claude_environment_is_isolated(self):
        environment = claude_environment(
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "secret",
                "GITHUB_TOKEN": "secret",
                "CLAUDE_TEST_TOKEN": "allowed",
            },
            ("CLAUDE_TEST_TOKEN",),
        )
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertEqual(environment["CLAUDE_TEST_TOKEN"], "allowed")
        self.assertEqual(environment["DISABLE_AUTOUPDATER"], "1")

    def test_runtime_selection_is_explicit(self):
        runtime = self._runtime_settings()
        runtime.codex_path = "/tmp/missing-codex"
        runtime.claude_path = "/tmp/missing-claude"
        executor = TaskExecutor(runtime)
        task = {
            "repository_path": "/tmp/repository",
            "runtime_requested": "claude-code",
        }
        with self.assertRaisesRegex(ValueError, "当前不可用"):
            executor.resolve_mode(task)
        task["runtime_requested"] = "auto"
        self.assertEqual(executor.resolve_mode(task), "demo")

    def test_claude_runtime_diagnostics_show_provider_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "claude"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            runtime = self._runtime_settings()
            runtime.claude_path = str(executable)
            runtime.claude_provider = "Domestic Gateway"
            runtime.claude_model = "glm-example"
            runtime.claude_api_host = "gateway.example.cn"
            runtime.claude_config_source = "Claude settings.json"
            runtime.claude_auth_configured = True

            diagnostics = TaskExecutor(runtime).runtime_info("claude-code")

        self.assertTrue(diagnostics["available"])
        self.assertEqual(diagnostics["status"], "configured")
        self.assertEqual(diagnostics["provider"], "Domestic Gateway")
        self.assertEqual(diagnostics["model"], "glm-example")
        self.assertNotIn("token", json.dumps(diagnostics).lower())

    def test_reads_safe_metadata_from_claude_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(
                json.dumps({
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://api.example.cn/v1",
                        "ANTHROPIC_AUTH_TOKEN": "super-secret",
                        "ANTHROPIC_MODEL": "qwen-example",
                    }
                }),
                encoding="utf-8",
            )
            environment = {
                "HOME": directory,
                "LILY_CLAUDE_CONFIG_PATH": str(settings_path),
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                profile = claude_profile()

        self.assertEqual(profile["provider"], "api.example.cn")
        self.assertEqual(profile["model"], "qwen-example")
        self.assertEqual(profile["api_host"], "api.example.cn")
        self.assertTrue(profile["auth_configured"])
        self.assertNotIn("super-secret", repr(profile))

    @staticmethod
    def _runtime_settings():
        return SimpleNamespace(
            openai_api_key="",
            openai_model="demo",
            codex_path="",
            codex_enabled=True,
            codex_timeout=30,
            claude_path="",
            claude_enabled=True,
            claude_timeout=30,
            claude_max_turns=3,
            claude_allowed_tools=("Read",),
            runtime_priority=("codex-cli", "claude-code"),
            allowed_repo_root=Path(tempfile.gettempdir()),
            worktree_root=Path(tempfile.gettempdir()) / "lily-test-worktrees",
            root=Path(tempfile.gettempdir()),
        )


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "test.db")
        self.db.init()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_task_lifecycle(self):
        created = self.db.create_task(
            {
                "title": "修复边界条件",
                "description": "处理输入为空时产生的异常并补充测试。",
                "repository": "demo/project",
                "repository_path": "/tmp/demo-project",
                "issue_url": "",
                "priority": 1,
                "risk": "low",
            },
            max_attempts=3,
        )
        claimed = self.db.claim_next_task("worker-a")
        self.assertEqual(claimed["id"], created["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["repository_path"], "/tmp/demo-project")
        self.assertEqual(claimed["runtime_requested"], "auto")

        self.db.update_task(
            created["id"],
            expected_lease_owner="worker-a",
            plan="plan result",
            runtime_provider="OpenAI",
            runtime_model="codex",
        )
        completed = self.db.complete_task(
            created["id"],
            ready_for_review=True,
            lease_owner="worker-a",
        )
        self.assertEqual(completed["status"], "awaiting_approval")
        self.assertEqual(completed["runtime_provider"], "OpenAI")
        self.assertEqual(completed["runtime_model"], "codex")

        approved = self.db.set_decision(created["id"], True)
        self.assertEqual(approved["status"], "approved")

    def test_stale_task_is_recovered_and_old_lease_cannot_write(self):
        created = self._create_task()
        claimed = self.db.claim_next_task("worker-a")
        self.assertEqual(claimed["id"], created["id"])
        self.db.update_task(created["id"], heartbeat_at="2000-01-01T00:00:00+00:00")

        recovered = self.db.recover_stale_tasks("2001-01-01T00:00:00+00:00")
        self.assertEqual(recovered, 1)
        self.assertEqual(self.db.get_task(created["id"])["status"], "queued")
        with self.assertRaises(LeaseLostError):
            self.db.update_task(
                created["id"],
                expected_lease_owner="worker-a",
                plan="late result",
            )

    def test_needs_revision_cannot_be_approved_and_can_retry(self):
        created = self._create_task()
        self.db.claim_next_task("worker-a")
        self.db.update_task(
            created["id"],
            expected_lease_owner="worker-a",
            verification_status="NEEDS_REVISION",
        )
        completed = self.db.complete_task(
            created["id"],
            ready_for_review=False,
            lease_owner="worker-a",
        )
        self.assertEqual(completed["status"], "needs_revision")
        with self.assertRaises(ValueError):
            self.db.set_decision(created["id"], True)
        retried = self.db.retry_task(created["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["verification_status"], "")

    def test_pause_state_is_persistent(self):
        self.assertFalse(self.db.is_paused())
        self.db.set_paused(True)
        self.assertTrue(self.db.is_paused())

    def _create_task(self):
        return self.db.create_task(
            {
                "title": "验证可靠性边界",
                "description": "验证任务租约与审批闸门的数据库行为。",
                "repository": "demo/project",
                "repository_path": "/tmp/demo-project",
                "issue_url": "",
                "priority": 1,
                "risk": "low",
                "runtime_requested": "claude-code",
            },
            max_attempts=3,
        )


if __name__ == "__main__":
    unittest.main()
