# Design and acceptance contract

evoctl gives people and agent clients one interface to Evolution API, whether it runs locally, behind HTTPS, or on an SSH host. A named profile carries connection settings. Credentials remain in environment variables or inside the API container; they never appear in profile files or normal output.

## Decisions

- One service layer backs the CLI and MCP server. Commands use noun–verb names; results have stable JSON envelopes and error codes.
- Common tasks have dedicated tools: status, contact lookup, message reading, sending, delivery checks, and pairing. A searchable, versioned API catalog covers the remaining endpoints. Read and mutation tools are separate; the server can expose read, write, or admin capabilities.
- SSH uses the user's OpenSSH configuration and host-key verification. A small bundled Python worker executes API requests on the remote host, so a container-held API key never crosses SSH. Remote setup, interactive login, key installation, and service startup are explicit operations.
- A status report distinguishes SSH, runtime, containers, API authentication, WhatsApp pairing, and delivery. Starting services does not recreate missing data volumes or instances.
- Message sends use a durable idempotency ledger. Reusing a key with the same payload returns the first result. Reusing it with another payload is an error. An uncertain network outcome cannot trigger a second send.
- MCP uses stdio, structured results, tool annotations, and explicit profile arguments. It does not expose an unauthenticated network listener or arbitrary shell execution.

## Acceptance

- A clean installation can add a profile, inspect status, bootstrap a remote connection, establish key access, connect an instance, search contacts, read messages, send once, and inspect delivery through short commands.
- The API catalog is searchable offline, exposes request schemas, and covers all REST routes in the pinned Evolution API source. Route coverage is checked by the catalog build script.
- CLI and MCP return equivalent results. Invalid input, timeout, missing service, authentication failure, ambiguous contact, and unknown send outcome have distinct actionable errors.
- Tests exercise real CLI subprocesses, a local HTTP protocol fixture, SQLite idempotency, and an actual MCP client/server session. Read-only smoke checks cover a real SSH host and Evolution deployment.
- The public repository contains installation, remote setup, security, API, troubleshooting, and contributor documentation, a license, a reproducible lockfile, and a pull request ready for human review.

## Sources

- [MCP tools specification, 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/server/tools): structured results, tool annotations, explicit handles, input validation, and actionable execution errors.
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk): the current stable SDK is v2; v1 examples are not the implementation contract for this project.
- [Anthropic tool design guidance](https://github.com/anthropics/claude-plugins-official/blob/main/plugins/mcp-server-dev/skills/build-mcp-server/references/tool-design.md): compact workflow tools, bounded output, distinct reads and writes, and search/execute for a large API surface.
- [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents): evaluate tools on realistic tasks and return useful identifiers and concise results.
- [Evolution API 2.3.7 source](https://github.com/evolution-foundation/evolution-api/tree/2.3.7) and [API documentation](https://docs.evolutionfoundation.com.br/en/evolution-api/create-instance): the deployed API contract. Source takes precedence where the documentation differs from runtime behavior.

Verified against upstream sources and package metadata on 2026-09-07. Evolution API is a separate service with its own license; evoctl does not bundle or relicense it.
