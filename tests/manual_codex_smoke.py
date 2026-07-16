"""Manual smoke test for the saved-login Codex CLI executor."""

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import TaskExecutor
from config import settings


async def main():
    task = {
        "id": "smoke-" + uuid.uuid4().hex[:8],
        "title": "Reject reversed clamp bounds",
        "description": (
            "Change clamp so minimum greater than maximum raises ValueError, "
            "add focused regression coverage, and run the existing tests."
        ),
        "repository": "local/clamp-fixture",
        "repository_path": "/Users/wanganchang/Documents/Codex/2026-07-13/ni/work/codex-fixture",
        "issue_url": "",
        "risk": "low",
        "attempts": 1,
    }
    executor = TaskExecutor(settings)
    results = [result async for result in executor.run(task)]
    print(json.dumps({
        "mode": executor.resolve_mode(task),
        "stages": [result.key for result in results],
        "tokens": {
            "input": sum(result.input_tokens for result in results),
            "output": sum(result.output_tokens for result in results),
        },
        "metadata": {key: value for result in results for key, value in result.metadata.items()},
        "verification": results[-1].content,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
