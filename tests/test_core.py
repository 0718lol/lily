import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent import CodexCliExecutor, TaskExecutor, extract_output_text
from database import Database


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
        claimed = self.db.claim_next_task()
        self.assertEqual(claimed["id"], created["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["repository_path"], "/tmp/demo-project")

        self.db.update_task(created["id"], plan="plan result")
        completed = self.db.complete_task(created["id"])
        self.assertEqual(completed["status"], "awaiting_approval")

        approved = self.db.set_decision(created["id"], True)
        self.assertEqual(approved["status"], "approved")

    def test_pause_state_is_persistent(self):
        self.assertFalse(self.db.is_paused())
        self.db.set_paused(True)
        self.assertTrue(self.db.is_paused())


if __name__ == "__main__":
    unittest.main()
