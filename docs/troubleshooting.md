# Troubleshooting

Start with `evoctl doctor`. A successful SSH connection does not imply Docker, API authentication, or WhatsApp pairing is ready. The returned stages and error hints identify the failed layer.

| Error or state | Recovery |
| --- | --- |
| `PROFILE_NOT_FOUND` | Add a profile or select one with `remote use`. |
| `SSH_DNS` | Check the SSH alias, VPN connectivity, and reachable hostname. |
| `SSH_HOST_KEY` | Verify the host identity through `remote login`; investigate a changed key before trusting it. |
| `SSH_AUTH_REQUIRED` | Use interactive `remote login`, then `remote key-setup` through that existing access. |
| `REMOTE_UV_MISSING` | Install uv on the remote host. |
| `REMOTE_WORKER_MISSING` | Run `remote connect` after installation or an upgrade. |
| `DOCKER_UNAVAILABLE` | Start the Docker runtime; `services start` can start Colima only when configured. |
| `CONTAINER_AMBIGUOUS` | Select the intended existing API container with `--api-container`. |
| `CONTAINER_NOT_FOUND` | Inspect the saved deployment and backups before restoring anything. |
| `CREDENTIAL_MISSING` | Supply the named variable on the API host or select the API container. |
| `API_UNAUTHORIZED` | Check that the configured credential belongs to this Evolution deployment. |
| WhatsApp `close` or `connecting` | Check the instance, then run `pair --open` and scan with the phone. |
| `SEND_OUTCOME_UNKNOWN` | Inspect `messages request`, message history, or delivery receipts; preserve the original request ID. |
| `OUTPUT_LIMIT` or `RESPONSE_TOO_LARGE` | Narrow/paginate the query or use a private output file and an appropriate byte limit. |

`messages status` reports `pending` when the message exists without receipt records. Evolution may not retain every delivery update; absence of a receipt is not proof of either delivery or non-delivery. The tool never upgrades acceptance into delivery confirmation.

For MCP startup failures, verify the executable's absolute path and run `evoctl mcp serve` from a terminal to inspect stderr. The server waits for protocol input; it is not an interactive chat shell. Configure environment references in the actual process environment and restart the MCP client after installation or configuration changes.
