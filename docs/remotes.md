# Remote setup and recovery

An SSH profile addresses the API from the remote host. The default origin, `http://127.0.0.1:8080`, therefore stays private on that host. `evoctl` sends structured JSON over SSH to a small bundled worker. The worker resolves the API credential locally and returns sanitized results.

Install `uv` on both machines using its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/). The SSH account needs permission to use Docker when selecting a container credential. `remote connect` installs the worker under `~/.local/share/evoctl/workers/`; it does not need the full evoctl package on the remote. Reconnect after an evoctl upgrade to install the matching worker. Both Linux and macOS are supported.

```bash
evoctl remote add office --ssh user@host.example --docker --instance Default
evoctl remote connect office
evoctl --profile office doctor
```

Use an existing SSH alias to carry ports, jump hosts, VPN names, and other OpenSSH settings. `.local` hostnames usually depend on local-network discovery; use a reachable DNS or VPN name when away from that network. The worker adds `~/.local/bin`, `/opt/homebrew/bin`, and `/usr/local/bin` to its PATH for common `uv` and Docker installations.

## Authentication

`remote login NAME` is interactive. It allows the user to verify the server's host key and enter a password. The connection can remain multiplexed for ten minutes after exit. `remote key-setup NAME` then installs a dedicated public key through existing access and independently verifies that key before saving its path in the profile.

The dedicated private key has no passphrase and lives in the private state directory. To use a passphrase-protected or hardware-backed key instead, configure it through your SSH agent or `--identity-file` when adding the profile. The tool never disables host-key verification or rewrites global SSH configuration. Disconnect closes only evoctl's own connection and GUI forwards.

For Docker, `--docker` discovers a unique image containing `evolution-api`. `--api-container NAME` selects one explicitly. The worker captures `AUTHENTICATION_API_KEY` from that container in memory; the value is never returned to the caller. For `--key-env NAME`, the variable must exist in the environment of the process running the API request. An SSH profile therefore needs it in the remote worker environment, not merely in the local shell.

## Services and pairing

`services start` starts existing Compose siblings, with the API container last. `services restart` restarts them. An explicit `--runtime colima` profile permits starting a stopped Colima VM. Other Docker runtimes remain externally managed. Without Compose labels, the profile can name additional existing `service_containers` through the structured `remote_add` tool or configuration file.

Service startup cannot restore a missing deployment. Review your installation and backups before creating containers or instances. To create an intended new instance, inspect `evoctl api schema instance.create`, then call it with a reviewed JSON body and a stable request ID. Pair it using `evoctl pair --open`. QR files are private local authentication material; remove them after pairing.

`evoctl ui --open` creates a loopback tunnel for a remote loopback HTTP API. Evolution Manager still requires its usual API credential, supplied separately through your credential manager. The credential is never placed in the URL. For direct HTTPS profiles, the command returns that deployment's Manager URL.

## Files

| Purpose | Default location |
| --- | --- |
| Profiles | `~/.config/evoctl/config.json` |
| Receipts, owned SSH sockets, dedicated keys, pairing images | `~/.local/state/evoctl/` |
| Remote worker versions | `~/.local/share/evoctl/workers/` on the SSH host |

XDG configuration/state locations are honored. `EVOCTL_CONFIG_DIR` and `EVOCTL_STATE_DIR` override them. Keep CLI and MCP on the same state directory if they must suppress each other's duplicate sends. Protect and back up the receipt database if that guarantee must survive a machine rebuild.
