# Contributing

Open a focused pull request. Keep CLI and MCP contracts in the shared service layer, preserve semantic read/write/admin checks, and add a regression test for changed behavior. Never use a live recipient as a test fixture. Every function needs a docstring, and Python 3.12 remains supported.

```bash
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv build
```

Tests use real loopback HTTP, CLI and MCP subprocesses, and SQLite. They do not contact WhatsApp. MCP checks cover both current discovery and legacy initialization. The lockfile records the exact tested dependency set; update it deliberately and verify new SDK contracts against official source.

Dependency updates use the seven-day package-age window declared in `pyproject.toml`. Keeping that resolver setting in the project makes `uv sync --locked` consistent between developer machines and CI.

For a separately configured live deployment, `uv run scripts/smoke_live.py --profile NAME` checks status, bounded contacts/chats, and the GUI tunnel. Optional `--contact`, `--chat`, and `--message-id` inputs exercise a specific read workflow. Output contains counts/readiness rather than private content. It sends no messages and does not restart services.

The hosted workflow runs the same checks. When paid runner access or account billing alone blocks CI, run equivalent local verification and state that hosted CI was waived for billing. Do not retry paid jobs or change billing settings to unblock them.

See [design](docs/design.md) for the acceptance contract and source research. Catalog route coverage must match the pinned upstream checkout before proposing an API version update.
