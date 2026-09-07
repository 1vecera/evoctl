# API catalog

`api list` searches the bundled Evolution API 2.3.7 catalog offline. Its result includes `next_offset` when more matches remain. `api schema OPERATION` returns the HTTP method, route, path and query parameters, request schema, source location, and access level. The profile supplies `instanceName` unless explicitly overridden with `--params`.

```bash
evoctl api list --query openai --limit 20
evoctl api schema instance.connection_state
evoctl api call instance.connection_state --params '{"instanceName":"Default"}'
evoctl api call chat.find_contacts --body @contacts-query.json
```

JSON can be inline, `@filename`, or `-` for stdin. Mutations use `--request-id`; omission generates a new ID, so callers that might retry should supply one themselves. `--dry-run` returns the validated request without contacting Evolution or reserving the ID. The API origin cannot be overridden by a request path.

## Schema fidelity

The source traversal finds 182 REST routes, including mounted integration routers. Static assets and the Manager frontend are excluded. Route facts and access levels come from the pinned source. Optional schema enrichment comes from the official OpenAPI files.

`schema_source: runtime_verified` means evoctl validates the body locally against a source-checked contract. `upstream_openapi` is advisory documentation; `undocumented` means only a general object shape is available. Evolution validates those requests. This distinction matters because some official examples use older body shapes: the deployed text endpoint accepts top-level `number` and `text`, and message/contact lookups use `page` with `offset` as page size. Request schemas do not constitute a guarantee that every integration has been live-tested.

The generic executor covers JSON requests and JSON/text responses. The five routes marked `multipart` also accept JSON media URLs/base64 in Evolution; evoctl does not upload binary multipart files. WebSockets, webhook consumption, streaming media, and exact export of authentication material are outside its scope. Authentication-state responses are redacted by design.

## Credentials in request bodies

For integration setup, an individual secret field can use an environment reference:

```json
{"apiKey": {"$env": "INTEGRATION_API_KEY"}}
```

The worker expands that reference in memory on the API host. The resolved value is redacted if returned by the API. Dry runs show the reference. Set the variable through your credential manager; do not place secret values in command-line arguments or committed JSON files. For SSH profiles, a variable in your local environment is not automatically forwarded.

For endpoints using a separate Authorization header, such as optional metrics Basic authentication, `--authorization-env VARIABLE` reads the complete header value from an API-host environment variable. `server.metrics` does not require the normal Evolution API key; availability and authentication depend on the server's metrics settings.

## Bounds and errors

Generic requests default to a 1 MiB upstream response limit, configurable with `--max-bytes` up to 16 MiB. CLI/MCP text output has a separate 64 KiB budget. Use bounded read queries, pagination, or CLI `--output-file` for larger results. An `OUTPUT_LIMIT` error can occur after a mutation completed; recover its receipt using the original ID instead of sending again.

| Exit status | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Execution failure or deployment not ready |
| `2` | Invalid command or input |
| `3` | Permission/authentication failure |
| `4` | Profile, operation, or receipt not found |
| `5` | Existing profile or conflicting idempotency key |
| `6` | A previous attempt has an unknown send outcome |
| `130` | Interrupted |

Treat the structured `error.code` as the precise contract. The first failed network attempt can return an API/SSH error; a repeated reserved mutation returns `SEND_OUTCOME_UNKNOWN`. No HTTP verb is a retry guarantee: some upstream GET endpoints mutate state.

## Refresh the catalog

Use the pinned upstream source and the checked-in builder:

```bash
git clone --depth 1 --branch 2.3.7 https://github.com/evolution-foundation/evolution-api.git tmp/evolution-api
uv run scripts/build_catalog.py tmp/evolution-api --check
```

Without `--specifications`, checking verifies route coverage, access, source locations, and the source digest while preserving enriched schemas. To rebuild with schemas, download the relevant YAML specifications linked from the [official documentation index](https://docs.evolutionfoundation.com.br/llms.txt) into a local directory and pass `--specifications DIRECTORY`. Review schema changes against runtime source, run the test suite, and update the versioned source link deliberately when upgrading Evolution.
