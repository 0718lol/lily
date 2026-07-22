from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


TASK_FIELDS = {
    "plan",
    "implementation",
    "review",
    "verification",
    "diff",
    "test_output",
    "execution_log",
    "executor_mode",
    "runtime_requested",
    "runtime_session_id",
    "runtime_provider",
    "runtime_model",
    "cost_usd",
    "worktree_path",
    "codex_session_id",
    "lease_owner",
    "heartbeat_at",
    "verification_status",
    "error",
    "input_tokens",
    "output_tokens",
    "status",
    "started_at",
    "finished_at",
}


class LeaseLostError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    repository TEXT NOT NULL DEFAULT '',
                    repository_path TEXT NOT NULL DEFAULT '',
                    issue_url TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 2,
                    risk TEXT NOT NULL DEFAULT 'low',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    plan TEXT NOT NULL DEFAULT '',
                    implementation TEXT NOT NULL DEFAULT '',
                    review TEXT NOT NULL DEFAULT '',
                    verification TEXT NOT NULL DEFAULT '',
                    diff TEXT NOT NULL DEFAULT '',
                    test_output TEXT NOT NULL DEFAULT '',
                    execution_log TEXT NOT NULL DEFAULT '',
                    executor_mode TEXT NOT NULL DEFAULT '',
                    runtime_requested TEXT NOT NULL DEFAULT 'auto',
                    runtime_session_id TEXT NOT NULL DEFAULT '',
                    runtime_provider TEXT NOT NULL DEFAULT '',
                    runtime_model TEXT NOT NULL DEFAULT '',
                    cost_usd REAL NOT NULL DEFAULT 0,
                    worktree_path TEXT NOT NULL DEFAULT '',
                    codex_session_id TEXT NOT NULL DEFAULT '',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT,
                    verification_status TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT OR IGNORE INTO settings(key, value) VALUES ('paused', 'false');
                """
            )
            existing = {
                row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()
            }
            migrations = {
                "repository_path": "TEXT NOT NULL DEFAULT ''",
                "diff": "TEXT NOT NULL DEFAULT ''",
                "test_output": "TEXT NOT NULL DEFAULT ''",
                "execution_log": "TEXT NOT NULL DEFAULT ''",
                "executor_mode": "TEXT NOT NULL DEFAULT ''",
                "runtime_requested": "TEXT NOT NULL DEFAULT 'auto'",
                "runtime_session_id": "TEXT NOT NULL DEFAULT ''",
                "runtime_provider": "TEXT NOT NULL DEFAULT ''",
                "runtime_model": "TEXT NOT NULL DEFAULT ''",
                "cost_usd": "REAL NOT NULL DEFAULT 0",
                "worktree_path": "TEXT NOT NULL DEFAULT ''",
                "codex_session_id": "TEXT NOT NULL DEFAULT ''",
                "lease_owner": "TEXT NOT NULL DEFAULT ''",
                "heartbeat_at": "TEXT",
                "verification_status": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in migrations.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def add_event(
        self,
        kind: str,
        message: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(task_id, kind, message, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, kind, message, json.dumps(metadata or {}, ensure_ascii=False), utc_now()),
            )

    def create_task(self, payload: dict[str, Any], max_attempts: int) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO tasks(
                    id, title, repository, repository_path, issue_url, description,
                    priority, risk, runtime_requested, max_attempts, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    payload["title"].strip(),
                    payload.get("repository", "").strip(),
                    payload.get("repository_path", "").strip(),
                    payload.get("issue_url", "").strip(),
                    payload["description"].strip(),
                    payload.get("priority", 2),
                    payload.get("risk", "low"),
                    payload.get("runtime_requested", "auto"),
                    max_attempts,
                    now,
                    now,
                ),
            )
        self.add_event("task.created", f"任务已进入队列：{payload['title']}", task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._row(db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM tasks
                ORDER BY
                    CASE status
                        WHEN 'running' THEN 0 WHEN 'queued' THEN 1
                        WHEN 'awaiting_approval' THEN 2 ELSE 3
                    END,
                    priority ASC,
                    created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next_task(self, worker_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE status = 'queued' ORDER BY priority ASC, created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = utc_now()
            updated = db.execute(
                """
                UPDATE tasks
                SET status = 'running', attempts = attempts + 1, started_at = ?,
                    updated_at = ?, error = '', lease_owner = ?, heartbeat_at = ?,
                    finished_at = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, worker_id, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
        self.add_event(
            "task.started",
            "执行器已领取任务",
            row["id"],
            {"worker_id": worker_id},
        )
        return self.get_task(row["id"])

    def heartbeat_task(self, task_id: str, worker_id: str) -> bool:
        now = utc_now()
        with self.connect() as db:
            updated = db.execute(
                """
                UPDATE tasks SET heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (now, now, task_id, worker_id),
            )
        return updated.rowcount == 1

    def recover_stale_tasks(self, stale_before: str) -> int:
        recovered: list[tuple[str, str]] = []
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT id, attempts, max_attempts FROM tasks
                WHERE status = 'running'
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (stale_before,),
            ).fetchall()
            for row in rows:
                retry = row["attempts"] < row["max_attempts"]
                status = "queued" if retry else "failed"
                error = "执行器心跳超时，任务已回收"
                db.execute(
                    """
                    UPDATE tasks SET status = ?, lease_owner = '', heartbeat_at = NULL,
                        error = ?, updated_at = ?, finished_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (status, error, now, None if retry else now, row["id"]),
                )
                recovered.append((row["id"], status))
        for task_id, status in recovered:
            self.add_event(
                "task.recovered" if status == "queued" else "task.failed",
                "执行器心跳超时，任务已重新排队"
                if status == "queued"
                else "执行器心跳超时，已达到重试上限",
                task_id,
            )
        return len(recovered)

    def update_task(
        self,
        task_id: str,
        *,
        expected_lease_owner: str | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        safe_values = {key: value for key, value in values.items() if key in TASK_FIELDS}
        if not safe_values:
            task = self.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            return task
        safe_values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in safe_values)
        where = "id = ?"
        parameters = [*safe_values.values(), task_id]
        if expected_lease_owner is not None:
            where += " AND status = 'running' AND lease_owner = ?"
            parameters.append(expected_lease_owner)
        with self.connect() as db:
            updated = db.execute(
                f"UPDATE tasks SET {assignments} WHERE {where}",
                parameters,
            )
        if expected_lease_owner is not None and updated.rowcount != 1:
            raise LeaseLostError(f"任务 {task_id} 的执行租约已失效")
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def complete_task(
        self,
        task_id: str,
        ready_for_review: bool = True,
        lease_owner: str | None = None,
    ) -> dict[str, Any]:
        status = "awaiting_approval" if ready_for_review else "needs_revision"
        task = self.update_task(
            task_id,
            expected_lease_owner=lease_owner,
            status=status,
            finished_at=utc_now(),
            lease_owner="",
            heartbeat_at=None,
        )
        self.add_event(
            "task.completed",
            "执行完成，等待人工审批"
            if ready_for_review
            else "验证未通过，需要修改后重试",
            task_id,
            {"verification_status": task["verification_status"]},
        )
        return task

    def fail_task(
        self,
        task_id: str,
        message: str,
        lease_owner: str | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        retry = task["attempts"] < task["max_attempts"]
        status = "queued" if retry else "failed"
        result = self.update_task(
            task_id,
            expected_lease_owner=lease_owner,
            status=status,
            error=message[:2000],
            finished_at=utc_now() if not retry else None,
            lease_owner="",
            heartbeat_at=None,
        )
        event_message = "执行失败，已重新排队" if retry else "执行失败，已达到重试上限"
        self.add_event("task.failed", event_message, task_id, {"error": message[:500]})
        return result

    def set_decision(self, task_id: str, approved: bool) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] != "awaiting_approval":
            raise ValueError("只有等待审批的任务可以作出决定")
        status = "approved" if approved else "rejected"
        result = self.update_task(task_id, status=status, finished_at=utc_now())
        self.add_event(
            f"task.{status}",
            "人工审批通过" if approved else "人工审批驳回",
            task_id,
        )
        return result

    def retry_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task["status"] not in {"failed", "rejected", "needs_revision"}:
            raise ValueError("当前任务状态不允许重试")
        with self.connect() as db:
            db.execute(
                """
                UPDATE tasks SET status = 'queued', attempts = 0, error = '',
                    plan = '', implementation = '', review = '', verification = '',
                    diff = '', test_output = '', execution_log = '', executor_mode = '',
                    worktree_path = '', codex_session_id = '', runtime_session_id = '',
                    runtime_provider = '', runtime_model = '', cost_usd = 0,
                    input_tokens = 0, output_tokens = 0,
                    verification_status = '', lease_owner = '',
                    heartbeat_at = NULL, started_at = NULL, finished_at = NULL,
                    updated_at = ? WHERE id = ?
                """,
                (utc_now(), task_id),
            )
        self.add_event("task.retried", "任务已重新进入队列", task_id)
        return self.get_task(task_id)

    def get_events(self, limit: int = 80) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        events = [dict(row) for row in rows]
        for event in events:
            event["metadata"] = json.loads(event["metadata"] or "{}")
        return events

    def is_paused(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = 'paused'").fetchone()
        return bool(row and row["value"] == "true")

    def set_paused(self, paused: bool) -> bool:
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES ('paused', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("true" if paused else "false",),
            )
        self.add_event("system.paused" if paused else "system.resumed", "任务循环已暂停" if paused else "任务循环已恢复")
        return paused

    def dashboard(self) -> dict[str, Any]:
        with self.connect() as db:
            counts = {
                row["status"]: row["count"]
                for row in db.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
            }
            usage = db.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) AS input,
                       COALESCE(SUM(output_tokens), 0) AS output,
                       COALESCE(SUM(cost_usd), 0) AS cost
                FROM tasks
                """
            ).fetchone()
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "active": counts.get("running", 0) + counts.get("queued", 0),
            "awaiting_approval": counts.get("awaiting_approval", 0),
            "approved": counts.get("approved", 0),
            "failed": counts.get("failed", 0),
            "input_tokens": usage["input"],
            "output_tokens": usage["output"],
            "cost_usd": usage["cost"],
            "paused": self.is_paused(),
        }
