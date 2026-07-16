# Lily Development Guide

- Keep the human approval gate mandatory.
- Never expose API keys through API responses, logs, events, or frontend state.
- Do not execute model-generated commands without a strict allowlist and timeout.
- Keep Codex CLI runs in a detached worktree with `workspace-write`; never add a danger-full-access path.
- Never commit, push, merge, or copy a generated patch into the source repository automatically.
- Preserve the task lifecycle and SQLite persistence when changing the worker.
- Run `python -m pytest -q` and `python -m compileall .` after backend changes.
- Keep the browser UI usable at 360px and 1440px widths.
