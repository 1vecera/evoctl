# Recipient search

Use `evoctl chats search "Alex"` or MCP `evoctl_read` with `{"action":"chats_search","arguments":{"query":"Alex"}}`. Discover the shared schema with `evoctl_discover` and `{"operation":"chats_search"}`. This adds a read action to the existing three-tool surface; it does not add a fourth MCP tool. Legacy `contacts search` / `contacts_search` and `chats list` remain available with their existing contracts.

The search combines stored contacts, message-backed conversations and participating groups. It matches all available display names and group subjects, case and diacritic insensitively, plus exact/partial JIDs and personal phone numbers (including formatting such as `+1 (555) 000-0001`). Group subjects take display precedence over stale chat/contact names; those older names still match. Contact display names take precedence over chat names. An explicitly saved local name takes precedence over all upstream names while keeping those names searchable. Every result has `jid`, `name` and `kind`. Unnamed recipients retain an empty name and their exact JID. Device suffixes and legacy `@c.us` phone aliases deduplicate to the canonical phone JID; `@lid` identities remain distinct because a phone mapping cannot safely be inferred. System notifications (`0@s.whatsapp.net`), broadcasts, newsletters and bots such as Meta AI (`@bot`) are outside this person/group search. Their presence must not abort the remaining scan.

No name match causes a send. Multiple matches stay separate, and sending still requires an exact recipient JID or international digits. A unique result from an incomplete scan does not establish that the name is unambiguous. Search never sends messages or read receipts and never searches message text. The chat endpoint includes last-message data, but evoctl discards it before matching, caching or returning results. Group descriptions and participant data are also discarded.

## Names missing from Evolution

The WhatsApp app can show an address-book name that Evolution does not expose. In 2.3.7, contact search returns a single `pushName` field, which may contain only a profile first name; the chat endpoint can also return `null` because its SQL selects two columns called `pushName`. Ignoring accents cannot recover a surname absent from both responses. These are upstream data limitations, not evidence that the person is absent from WhatsApp.

Use the built-in local contact list to supply the missing names. Export your address book, preview the import, then save it locally. No Google or Microsoft login is needed in evoctl, and there is no automatic cloud synchronization.

### Export and import an address book

- **Outlook.com / Outlook on the web:** open **People → Manage contacts → Export contacts**, choose **All contacts**, then **Export**. Save the downloaded CSV. [Microsoft's export guide](https://support.microsoft.com/en-us/office/export-contacts-from-outlook-com-or-outlook-on-the-web-578cca22-3550-4c73-b3f0-9978cfeac83f).
- **Google Contacts:** select all contacts, choose **More actions → Export**, select **Google CSV**, then **Export**. [Google's export guide](https://support.google.com/contacts/answer/7199294).

```bash
evoctl contacts import ~/Downloads/contacts.csv --dry-run
evoctl contacts import ~/Downloads/contacts.csv
evoctl contacts list --query "novak"
evoctl chats search "Alex Novak"
```

`contacts list` searches only the local directory and works while WhatsApp or SSH is unavailable. It returns `contacts`, a matching-entry `total`, and `next_offset`; continue with the same query and `--offset`. Results are sorted by accent-insensitive name then JID. Local entries have not been checked for WhatsApp registration (`source: "local"`, `whatsapp_verified: false`). Use `chats search` to find recipients observed by Evolution. Both paths ignore case and diacritics. No local match selects or messages a recipient automatically.

The importer recognizes English Outlook and Google CSV name/phone columns, including several phone numbers per person. It stores one name per phone JID. Email-only contacts, blank names, invalid numbers and numbers shared by different names are skipped and reported. Names differing only in case or diacritics collapse within the same file. A person with several numbers produces several entries. Email addresses, notes and other export fields are not copied into the local directory. Keep your original export if you need those fields.

By default, phone numbers must begin with `+` or `00`. Add `--region CZ` only when unprefixed numbers in that file should be interpreted as Czech numbers (or specify the appropriate two-letter country). The machine's locale is never used to guess a country. Numbers are normalized to E.164 after a phone-length check; this does not verify ownership, active service or WhatsApp membership. Extensions, short codes and ambiguous combined numbers are excluded. Google CSV's ` ::: ` separator between numbers is supported.

UTF-8 and UTF-16 files with a BOM are recognized; UTF-8 without a BOM also works. For a legacy export, pass an explicit encoding such as `--encoding cp1250`. Comma, semicolon and tab delimiters are recognized. Localized or custom column headers need explicit mappings:

```bash
evoctl contacts import kontakty.csv --name-column "Jméno" --name-column "Příjmení" --phone-column "Mobilní telefon" --region CZ --dry-run
```

Repeat `--name-column` to join name components and `--phone-column` for additional phone columns. Import accepts at most 4 MiB and 20,000 nonblank contact rows; malformed CSV fails before any names are saved. `rows` counts nonblank records, `skipped_rows` counts records without any usable unambiguous number, and `usable_phones` counts unique import candidates. `inserted`, `updated`, `unchanged` and `conflicts` describe those candidates relative to existing local names. `issues` contains up to 20 row/reason examples, `issue_counts` counts all problems by reason, and `issues_omitted` reports the remaining diagnostics. CSV record numbering starts at 2 after the header. One row can have several phone issues; a row with at least one usable number can still be imported. A successful import can include skipped rows; inspect these counts.

Existing local names are preserved when an export disagrees (`conflicts`). Review a `--dry-run --replace` preview before using `--replace` to update those names. Numbers shared by different names inside the CSV stay excluded even with `--replace`. Re-importing an identical export makes no name changes; contacts absent from a later export are not deleted. Import writes are atomic. To refresh names later, export again and rerun the import.

### Save or remove a single name

After confirming the exact number/JID and full name, save or remove a mapping directly:

```bash
evoctl contacts name 15550000001 "Alex Novák"
evoctl chats search "Alex Novak"
evoctl contacts search "novak"
evoctl contacts name 15550000001 --clear
```

Imported and individually saved names share owner-only `contact-names.sqlite3` in the evoctl state directory, scoped to the selected profile and actual deployment target. CLI and MCP use the same storage. It never changes WhatsApp or Evolution. An upstream name update does not erase the local name. In remote searches, local names are applied only to recipients observed in the current remote scan; an old mapping cannot manufacture a current recipient. Use `--clear` to remove a saved or imported entry and restore upstream naming. Contact search and chat listing use the same name and identifier rules as combined search, including skipping non-recipient system rows.

MCP exposes `evoctl_write` actions `contacts_import` (CSV text and import options) and `contacts_name` (`{"jid":"15550000001","name":"Alex Novák"}`; an explicit empty `name` clears it). `evoctl_read` action `contacts_list` searches the local directory. Read-only mode can search saved names but cannot modify them. Names are not included in `remote list`. Changing saved names invalidates an existing combined-search cursor; start a fresh search rather than mixing cached old names with new ones.

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
