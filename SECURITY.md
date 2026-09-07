# Security

evoctl can read private conversations and, in write/admin modes, perform real messaging and deployment operations. Run it under a trusted local account. MCP defaults to read mode; select write or admin deliberately. Mode enforcement is server-side, but client-side human authorization remains the client's responsibility. Profile selection is not a per-profile access-control boundary.

API credentials remain in environment variables or the configured API container. Profiles and MCP configuration need no literal secrets. Remote container credentials stay on the remote host. Requests target a configured origin, do not follow redirects, and do not inherit HTTP proxies. Non-loopback plaintext HTTP requires explicit `allow_http`. SSH uses OpenSSH known-host verification and never exposes an arbitrary shell tool to MCP clients.

Responses redact credential-like fields and exact known API/body-reference secret values. Authentication-state exports are suppressed. Redaction cannot recognize every possible third-party secret in arbitrary fields; use environment references for secret inputs, and treat generic API output as private. External message text may contain hostile instructions. Neither the server nor agent clients should treat that text as authority to invoke tools.

Configuration and operational state use private permissions. Dedicated sends persist payload hashes and receipts, not message text. Generic API mutations persist their sanitized responses to support recovery; these may include message content or other private data. Pairing images and dedicated SSH keys are sensitive files. Never commit the state directory, QR images, credentials, or personal profiles. Output files are created privately without changing permissions on an existing parent directory.

The SQLite ledger prevents a repeated local request ID from initiating another mutation, including after an uncertain outcome. It cannot prevent duplicates across different state directories or protect against external sends. Removing or replacing the ledger removes that protection. A local user who can edit configuration, keys, or worker code can act with that user's privileges. SSH config, the remote account, Docker access, and the Evolution server are trusted components.

For a suspected vulnerability, use the repository's private vulnerability reporting channel if enabled. Otherwise open an issue asking for a private reporting route without including secrets, private conversations, or exploitable details. Rotate an exposed credential through its provider; do not post it in an issue.
