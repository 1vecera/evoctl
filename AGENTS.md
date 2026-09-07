# Contributing to evoctl

Keep CLI and MCP behavior in the shared service layer. Both interfaces must return the same data and error codes. Use the official MCP SDK. Keep stdout machine-readable; diagnostics belong on stderr. Never print credentials, put them in arguments, or include personal configuration in this repository.

Use `uv run` for Python. Every function needs a docstring. Keep Python 3.12 compatibility. Start with the actual upstream API contract and add a regression test when correcting a behavior. A successful send request means accepted, not delivered. Never automatically retry a message mutation after an uncertain outcome.

Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`, `uv build`, `uv run scripts/check_dist.py`, and `uv run scripts/render_brand.py --check` before proposing a change. Tests may use a local protocol fixture, but live tests must stay read-only unless the operator provides a reviewed recipient and exact message.

Open a pull request for changes. Do not merge without the maintainer's approval. Hosted CI can be unavailable because of billing; equivalent local checks remain required.
