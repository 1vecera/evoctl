# Local backend alongside a remote connection

Evolution API 2.3.7 includes Evolution Manager at `/manager/`. The [Compose example](compose.yaml) runs both from the API image, with separate PostgreSQL, Redis, and WhatsApp-session volumes. Only `127.0.0.1:8080` is published; the database and Redis stay on the Compose network. This follows the [upstream 2.3.7 deployment](https://github.com/evolution-foundation/evolution-api/tree/2.3.7), using its bundled frontend instead of a separately versioned Manager image.

## Install a new local deployment

Use a working Docker Engine with Compose and Docker access for the account running evoctl. On Linux, arrange for Docker to start at boot if this connection must survive a reboot. Keep a new deployment in a private directory outside the repository. Do not reuse an existing deployment directory or copy its WhatsApp authentication volume; another computer should be paired as a separate linked device.

Copy `docs/compose.yaml` to the new deployment directory as `compose.yaml`. Generate two different random hex secrets of at least 32 bytes and save them in an owner-only `.env` file there as `AUTHENTICATION_API_KEY` and `POSTGRES_PASSWORD`. Optionally set `SESSION_CLIENT` to a recognizable device label. Do not print or commit the secrets, or paste expanded `docker compose config` output into logs. `docker compose config --quiet` validates without printing values.

Run these commands from that directory:

```bash
docker compose config --quiet
docker compose up -d --wait
evoctl remote add local --api-container evoctl-api --instance Default
evoctl --profile local api call instance.create \
  --body '{"instanceName":"Default","integration":"WHATSAPP-BAILEYS","qrcode":false,"alwaysOnline":false,"readMessages":false,"readStatus":false,"syncFullHistory":true}' \
  --request-id create-local-default
evoctl --profile local pair --open
evoctl --profile local status
evoctl --profile local ui --open
```

Wait for the API to finish its first database migrations before creating the instance. If an instance-creation request has an uncertain outcome, inspect `instance.fetch_instances` and the original request receipt before trying again. If the intended instance already exists, skip creation and pair it. Scan the pairing QR from WhatsApp → Linked devices → Link a device. Remove the private QR image after pairing.

Manager is available at `http://127.0.0.1:8080/manager/`; log in with this deployment's API key from the private `.env` file. evoctl itself reads that key from the running container, without copying it into a profile or URL. The example disables webhooks and telemetry and does not create an export or message automation. Keep any archive/export jobs on their original host to avoid duplicate processing.

## Use both connections

Keep the existing remote profile, then check and select connections explicitly:

```bash
evoctl status --all
evoctl doctor --all
evoctl remote use local
evoctl --profile mini status
evoctl --profile local chats list --limit 10
```

`status --all` checks profiles concurrently and returns their individual reports plus the effective default. Overall `ready` is true only when at least one profile exists and every profile is ready; the CLI exits with status 1 otherwise. `--watch` and `--count` also work with `--all`. An unavailable remote does not hide the local report or change the default. `--all` cannot be combined with `--profile`.

`remote use` saves the default shared by CLI and unpinned MCP sessions; it does not disconnect either linked device. Explicit `--profile NAME` (or an MCP operation's `profile`) always wins. A server started with `evoctl --profile NAME mcp serve` stays pinned to that profile even after the saved default changes.

Each backend keeps its own synchronized history. Receipts, saved contact names, and search cursors are scoped to the selected target on the client. Never retry an uncertain send on the other connection: its ledger cannot prove whether the first connection sent it. Check the request and delivery receipt on the original profile. evoctl does not automatically fail over reads or writes.

Containers restart automatically unless explicitly stopped. `docker compose stop` preserves the deployment and all volumes. Do not use `down --volumes` to repair pairing.
