# Command reference

Select a deployment once with `evoctl remote use NAME`, or put `--profile NAME` before a command. Profiles and receipts are shared with MCP.

| Task | Command |
| --- | --- |
| Inspect readiness | `evoctl status` or `evoctl doctor` |
| Monitor continuously | `evoctl status --watch --interval 5` |
| List profiles | `evoctl remote list` |
| Find contacts | `evoctl contacts search "Alex" --limit 10` |
| Continue a contact search | `evoctl contacts search "Alex" --cursor '1:10'` |
| List conversations | `evoctl chats list --limit 20` |
| Read one conversation | `evoctl messages read 15550000001 --limit 20` |
| Preview exact text | `evoctl messages send 15550000001 --text 'Hello' --dry-run` |
| Send a prepared file | `evoctl messages send 15550000001 --text-file message.txt --request-id greeting` |
| Recover a send receipt | `evoctl messages request greeting` |
| Inspect delivery | `evoctl messages status MESSAGE_ID` |
| Show a pairing QR | `evoctl pair --open` |
| Open Evolution Manager | `evoctl ui --open` |
| Start existing services | `evoctl services start` |
| Restart existing services | `evoctl services restart` |
| Close owned connections and GUI forwards | `evoctl remote disconnect NAME` |
| Discover a REST operation | `evoctl api list --query group` |
| Inspect its arguments | `evoctl api schema group.fetch_all_groups` |
| Print MCP client configuration | `evoctl mcp config --mode write` |

Numbers and cursors above are examples; use exact returned identifiers and continuation values. Contact scans are bounded and report whether they are complete. History supports `--page` and an ISO timestamp with timezone through `--since`.

Each command prints a JSON envelope: `{"ok":true,"data":...,"error":null}`. Errors have codes, recovery hints, and nonzero exit statuses. Monitoring emits one envelope per line; `--count` bounds a watch. Global `--output-file result.json` writes a private result file. `--text-file -` reads message text from stdin, while generic `--body` accepts inline JSON, `@filename`, or `-`.

For instance creation, media, groups, integrations, and the rest of the REST surface, use [the API catalog](api.md). For remote authentication and service setup, see [remotes](remotes.md).
