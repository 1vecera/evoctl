# Command reference

Select a deployment once with `evoctl remote use NAME`, or put `--profile NAME` before a command. Profiles and receipts are shared with MCP.

| Task | Command |
| --- | --- |
| Inspect readiness | `evoctl status` or `evoctl doctor` |
| Monitor continuously | `evoctl status --watch --interval 5` |
| List profiles | `evoctl remote list` |
| Find people and groups | `evoctl chats search "Alex" --limit 10` |
| Continue the same search | `evoctl chats search "Alex" --limit 10 --cursor "$NEXT_CURSOR"` |
| Search only groups | `evoctl chats search "Study" --kind group` |
| Legacy contacts-only lookup | `evoctl contacts search "Alex" --limit 10` |
| Save a confirmed name locally | `evoctl contacts name 15550000001 "Alex Novák"` |
| Preview an exported address book | `evoctl contacts import contacts.csv --dry-run` |
| Import local contact names | `evoctl contacts import contacts.csv` |
| Search local contacts offline | `evoctl contacts list --query "novak"` |
| Remove the saved local name | `evoctl contacts name 15550000001 --clear` |
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

Numbers and cursors above are examples; use exact returned identifiers and continuation values. Recipient scans are bounded and report whether they are complete; see [search behavior and limits](recipient-search.md). History supports `--page` and an ISO timestamp with timezone through `--since`.

Each command prints a JSON envelope: `{"ok":true,"data":...,"error":null}`. Errors have codes, recovery hints, and nonzero exit statuses. Monitoring emits one envelope per line; `--count` bounds a watch. Global `--output-file result.json` writes a private result file. `--text-file -` reads message text from stdin, while generic `--body` accepts inline JSON, `@filename`, or `-`.

For instance creation, media, groups, integrations, and the rest of the REST surface, use [the API catalog](api.md). For remote authentication and service setup, see [remotes](remotes.md).

## Search recipients

`chats search` searches personal contact/display names, group names/subjects and JID/phone substrings together, ignoring case and diacritics. It returns `data.chats` with exact `jid`, `name` and `kind` (`person` or `group`). A blank query lists discovered recipients; `--kind person` or `--kind group` is optional. This is recipient discovery, not full-text message search. Multiple name matches remain separate: select and review the exact recipient before sending.

If WhatsApp shows a full name but Evolution returns only a first name or no name, export **All contacts** from Outlook's People → Manage contacts → Export contacts, or export **Google CSV** from Google Contacts. Preview with `contacts import contacts.csv --dry-run`, then run the import without `--dry-run`. See [export instructions, formats and conflict handling](recipient-search.md#export-and-import-an-address-book). For a single confirmed recipient, `contacts name` saves the mapping directly.

`contacts list --query "novak"` searches the local directory offline, with `--limit` and `--offset` pagination. Imported names also appear in remote searches and chat listing; `Alex Novak` matches saved `Alex Novák`. These commands do not rename or synchronize the phone address book. Start a fresh combined search after changing local names. Use `--region CZ` only if national numbers in the export should be interpreted as Czech numbers; the default requires `+` or `00` international notation. Existing local names are retained unless `--replace` is explicit. `contacts import -` reads CSV from stdin.

Follow `next_cursor` using the same query, kind, limit, scan_pages, group_timeout, profile and state directory. `--scan-pages` defaults to 5 and allows 1–20 pages of 100 rows per paginated source per call. An empty `chats` array with `complete: false` does not establish that no recipient matches. `next_cursor: null` can also accompany an incomplete result when a source failed or reached a hard bound.

A source failure returns a nonzero exit status and `error.code: SEARCH_PARTIAL`, with available matches and the original source errors retained in `data`. Inspect `data.sources`; continue remaining scans with `next_cursor`, then start a fresh search to retry failed sources. `contacts search` retains its existing contacts-only behavior and page/row cursor for compatibility; use `chats search` for the combined directory.
