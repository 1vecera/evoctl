# Recipient search

Use `evoctl chats search "Alex"` or MCP `evoctl_read` with `{"action":"chats_search","arguments":{"query":"Alex"}}`. Discover the shared schema with `evoctl_discover` and `{"operation":"chats_search"}`. This adds a read action to the existing three-tool surface; it does not add a fourth MCP tool. Legacy `contacts search` / `contacts_search` and `chats list` remain available with their existing contracts.

The search combines stored contacts, message-backed conversations and participating groups. It matches all available display names and group subjects, case and diacritic insensitively, plus exact/partial JIDs and personal phone numbers (including formatting such as `+1 (555) 000-0001`). Group subjects take display precedence over stale chat/contact names; those older names still match. Contact display names take precedence over chat names. An explicitly saved local name takes precedence over all upstream names while keeping those names searchable. Every result has `jid`, `name` and `kind`. Unnamed recipients retain an empty name and their exact JID. Device suffixes and legacy `@c.us` phone aliases deduplicate to the canonical phone JID; `@lid` identities remain distinct because a phone mapping cannot safely be inferred. System notifications (`0@s.whatsapp.net`), broadcasts, newsletters and bots such as Meta AI (`@bot`) are outside this person/group search. Their presence must not abort the remaining scan.

No name match causes a send. Multiple matches stay separate, and sending still requires an exact recipient JID or international digits. A unique result from an incomplete scan does not establish that the name is unambiguous. Search never sends messages or read receipts and never searches message text. The chat endpoint includes last-message data, but evoctl discards it before matching, caching or returning results. Group descriptions and participant data are also discarded.

## Names missing from Evolution

The WhatsApp app can show an address-book name that Evolution does not expose. In 2.3.7, contact search returns a single `pushName` field, which may contain only a profile first name; the chat endpoint can also return `null` because its SQL selects two columns called `pushName`. Ignoring accents cannot recover a surname absent from both responses. These are upstream data limitations, not evidence that the person is absent from WhatsApp.

After confirming the exact number/JID and full name, save the mapping locally:

```bash
evoctl contacts name 15550000001 "Alex Novák"
evoctl chats search "Alex Novak"
evoctl contacts search "novak"
evoctl contacts name 15550000001 --clear
```

This stores one display name per canonical person/group JID in owner-only `contact-names.sqlite3`, shared by CLI and MCP in the evoctl state directory and scoped to the selected profile and actual deployment target. It never changes WhatsApp or Evolution, and it is not automatic phone address-book synchronization. An upstream name update does not erase the local name. Local names are applied only to recipients observed in the current remote scan; an old mapping cannot manufacture a current recipient. Use `--clear` to restore upstream naming. Contact search and chat listing use the same name and identifier rules as combined search, including skipping non-recipient system rows.

MCP exposes the reversible local write as `evoctl_write` action `contacts_name`, with `{"jid":"15550000001","name":"Alex Novák"}`; an explicit empty `name` clears it. Read-only mode can search saved names but cannot modify them. Names are not included in `remote list`. Changing saved names invalidates an existing combined-search cursor; start a fresh search rather than mixing cached old names with new ones.

## Bounds and continuation

Each scan calls the bulk group endpoint once per search (`getParticipants=false`) and at most `scan_pages` pages from each of contacts and chats. The default is 5 pages per source; the allowed range is 1–20, each page holding 100 records. Person-only filtering skips the group endpoint. Group-only filtering still checks contacts and chats for additional stored groups. Every request has a 1 MiB response limit. Contact/chat requests use the profile timeout. The single bulk group call uses `group_timeout` (CLI `--group-timeout`), defaulting to 120 seconds with a maximum of 120; it can take substantially longer than a chat page because Evolution fetches pictures internally. This does not modify the saved profile. MCP clients need a tool-call timeout long enough for the bulk call plus the bounded contact/chat scans. Evoctl performs no per-group fallback or automatic failed-source retry.

`limit` bounds returned matches to 1–100 (default 20), independently of the scan budget. Unreturned matches are buffered and drained before further upstream scans. `next_cursor` continues at saved source positions, and a canonical JID is returned only once in that continuation chain. Use the exact returned cursor with the same query, kind, limit, scan_pages, group_timeout and target; CLI and MCP can exchange cursors if they share the state directory. Replaying an already consumed cursor returns its identical cached response without another network call. Results are sorted by JID within each buffered batch; later scans can discover additional matches.

| Field | Meaning |
| --- | --- |
| `chats` | Available matches in this response, never an automatic recipient selection. |
| `scanned` | Source records inspected on this call; duplicates count, buffered result pages report zero. |
| `matches_in_scan` | Matching, not-yet-emitted recipients buffered before this response was selected. |
| `sources` | Cumulative rows scanned and `pending`, `complete`, `failed` or `excluded` status per source; failures retain structured upstream error codes. |
| `has_more` / `next_cursor` | More buffered results or unfinished source scans are available. An empty page can still have a continuation. |
| `complete` | Every included source reached its observed end without failure, and buffered results have been returned. |
| `partial` | A source failed or reached a hard bound; true also produces the `SEARCH_PARTIAL` envelope error. |
| `consistency` | `observed`: coverage of a changing live directory, not a transactional snapshot. |

An empty page is definitive only for the successfully completed observed scan. Never report “no matches” or “only one match” from a truncated prefix. If a source fails, CLI exits 1 and MCP marks the result as an error while retaining partial data. Continue any remaining cursor, inspect `sources`, and start a new search after fixing the cause to retry failed sources. `next_cursor: null` with `complete: false` means this search cannot advance further; it does not mean no matches exist.

A search stops a paginated source after 100 pages (10,000 records) or a repeated full page, and bounds retained recipient metadata to 8 MiB. These conditions report explicit source errors. The local cache admits at most 32 unfinished/replayable sessions. Cursors expire 30 minutes after search creation; expired metadata and cached pages are purged on the next search access. Storage is owner-only (`searches.sqlite3` in the evoctl state directory), shared across CLI and MCP. Already complete single-call searches do not retain a session. Concurrent cache advancement can return `SEARCH_CACHE_BUSY`; retry the same read-only cursor.

Upstream contacts have no explicit ordering, and chats are ordered by recent activity. Neither endpoint offers a snapshot token, so activity during later remote scans may move rows across offsets. Local buffered pages and cursor replay are stable, but a multi-call scan cannot promise a point-in-time inventory; start a fresh search when freshness matters. Newly paired instances may still be syncing.

## Upstream contract

Verified against Evolution API 2.3.7, commit [`cd800f2`](https://github.com/evolution-foundation/evolution-api/tree/cd800f2976e1e5b682fbf86a01ee4d85ae61f370).

| Source | Verified request and response |
| --- | --- |
| Contacts | `POST /chat/findContacts/{instanceName}` with `where: {}`, `offset: 100`, `page: N`; returns an array with `remoteJid` and `pushName`. `offset` is page size, not a row offset. [Implementation](https://github.com/evolution-foundation/evolution-api/blob/cd800f2976e1e5b682fbf86a01ee4d85ae61f370/src/api/services/channel.service.ts#L500). |
| Chats | `POST /chat/findChats/{instanceName}` with `where: {}`, `take: 100`, `skip: N`; returns message-backed chat metadata, including groups without a matching Contact row. Group names come from Chat/Contact data and the result includes `pushName`. `orderBy` is not implemented; activity controls the order. [Implementation](https://github.com/evolution-foundation/evolution-api/blob/cd800f2976e1e5b682fbf86a01ee4d85ae61f370/src/api/services/channel.service.ts#L719). |
| Groups | `GET /group/fetchAllGroups/{instanceName}?getParticipants=false`; returns an array with `id` and `subject` from participating group metadata. This includes groups absent from contacts or message-backed chat rows. [Implementation](https://github.com/evolution-foundation/evolution-api/blob/cd800f2976e1e5b682fbf86a01ee4d85ae61f370/src/api/integrations/channel/whatsapp/whatsapp.baileys.service.ts#L4448) and [required string enum](https://github.com/evolution-foundation/evolution-api/blob/cd800f2976e1e5b682fbf86a01ee4d85ae61f370/src/validate/group.schema.ts#L57). |

The group endpoint has no upstream pagination. Evolution itself loops through groups and fetches pictures, even with participants disabled. Evoctl bounds its one bulk request by timeout and response bytes, but cannot bound server-side work or make it cancellable. An oversized response, unavailable WhatsApp session or timeout is an explicit partial-source failure, not an empty group directory. Avoiding that internal upstream work requires an Evolution API change.

Evolution also [excludes the system identity from personal chat imports](https://github.com/evolution-foundation/evolution-api/blob/cd800f2976e1e5b682fbf86a01ee4d85ae61f370/src/api/integrations/chatbot/chatwoot/utils/chatwoot-import-helper.ts#L566); its presence in `findChats` must not invalidate usable people and groups.
