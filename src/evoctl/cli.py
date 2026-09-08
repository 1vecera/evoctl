"""Short, JSON-first CLI commands for people and agent clients."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
from pydantic import ValidationError
from typer._click.exceptions import ClickException

from evoctl import __version__
from evoctl.catalog import Mode
from evoctl.config import ConfigStore, Profile, atomic_write
from evoctl.mcp_server import serve
from evoctl.models import Envelope
from evoctl.service import Service
from evoctl.worker import EvoError

app = typer.Typer(
    no_args_is_help=True, help="Evolution API messaging and remote control. Results are JSON; diagnostics go to stderr."
)
remote_app = typer.Typer(no_args_is_help=True, help="Save, connect, inspect, and authenticate named deployments.")
messages_app = typer.Typer(
    no_args_is_help=True, help="Read history, send exact text once, and inspect delivery receipts."
)
contacts_app = typer.Typer(no_args_is_help=True, help="Find exact recipient JIDs before sending.")
chats_app = typer.Typer(no_args_is_help=True, help="Search people and groups together, or list conversations.")
api_app = typer.Typer(no_args_is_help=True, help="Discover and call the complete versioned REST catalog.")
services_app = typer.Typer(no_args_is_help=True, help="Manage existing service containers without recreating data.")
mcp_app = typer.Typer(no_args_is_help=True, help="Serve structured tools through the official MCP protocol.")
app.add_typer(remote_app, name="remote")
app.add_typer(messages_app, name="messages")
app.add_typer(contacts_app, name="contacts")
app.add_typer(chats_app, name="chats")
app.add_typer(api_app, name="api")
app.add_typer(services_app, name="services")
app.add_typer(mcp_app, name="mcp")


@dataclass
class Options:
    """Explicit invocation settings shared by all subcommands."""

    profile: str
    output_file: Path | None


def emit(result: Envelope, output_file: Path | None = None) -> None:
    """Write one JSON envelope and use a nonzero exit status for failures."""
    if output_file is not None and result.ok:
        atomic_write(output_file, result.model_dump_json(indent=2) + "\n")
        result = Envelope(ok=True, data={"output_file": str(output_file)})
    typer.echo(result.model_dump_json())
    if not result.ok:
        code = result.error["code"] if result.error else "ERROR"
        exits = {
            "INVALID_INPUT": 2,
            "INVALID_RECIPIENT": 2,
            "INVALID_BODY": 2,
            "INVALID_PARAMETERS": 2,
            "CAPABILITY_DENIED": 3,
            "API_UNAUTHORIZED": 3,
            "SSH_AUTH_REQUIRED": 3,
            "PROFILE_NOT_FOUND": 4,
            "OPERATION_NOT_FOUND": 4,
            "REQUEST_NOT_FOUND": 4,
            "IDEMPOTENCY_CONFLICT": 5,
            "PROFILE_EXISTS": 5,
            "SEND_OUTCOME_UNKNOWN": 6,
        }
        raise SystemExit(exits.get(code, 1))


def invoke(context: typer.Context, tool: str, **arguments: Any) -> None:
    """Invoke the shared service and preserve the stdout contract."""
    options: Options = context.obj
    service = Service()
    if "profile" in service.definitions[tool].input_model.model_fields and "profile" not in arguments:
        arguments["profile"] = options.profile
    result = service.invoke(tool, arguments, max_output_bytes=20_000_000 if options.output_file else 65_536)
    emit(result, options.output_file)


def version_callback(value: bool) -> None:
    """Print the package version without constructing a service connection."""
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    context: typer.Context,
    profile: Annotated[
        str, typer.Option("--profile", "-p", help="Saved profile; defaults to the configured default.")
    ] = "",
    output_file: Annotated[
        Path | None, typer.Option(help="Write a private JSON result file and print its path.")
    ] = None,
    version: Annotated[bool, typer.Option("--version", callback=version_callback, is_eager=True)] = False,
) -> None:
    """Select a deployment once, then use short task commands."""
    context.obj = Options(profile, output_file)


@remote_app.command("add")
def remote_add(
    context: typer.Context,
    name: str,
    ssh: Annotated[str, typer.Option(help="OpenSSH alias or user@host. Omit for direct HTTP.")] = "",
    url: Annotated[str, typer.Option(help="Evolution API origin as seen from its host.")] = "http://127.0.0.1:8080",
    instance: str = "Default",
    key_env: str = "EVOLUTION_API_KEY",
    docker: Annotated[bool, typer.Option(help="Read the key inside the unique Evolution API container.")] = False,
    api_container: Annotated[str, typer.Option(help="Explicit API container name when discovery is ambiguous.")] = "",
    runtime: Annotated[
        Literal["external", "colima"], typer.Option(help="Runtime managed by services start.")
    ] = "external",
    identity_file: Path | None = None,
    timeout: float = 20,
    allow_http: bool = False,
    replace: bool = False,
) -> None:
    """Save a profile. Example: evoctl remote add mini --ssh user@mini --docker."""
    settings = Profile(
        transport="ssh" if ssh else "http",
        ssh_host=ssh,
        api_url=url,
        instance=instance,
        key_env=key_env,
        api_container=api_container or ("auto" if docker else ""),
        runtime=runtime,
        identity_file=str(identity_file) if identity_file else "",
        timeout=timeout,
        allow_http=allow_http,
    )
    invoke(context, "remote_add", name=name, settings=settings.model_dump(), replace=replace)


@remote_app.command("list")
def remote_list(context: typer.Context) -> None:
    """List named profiles; no credentials are returned."""
    invoke(context, "remotes_list")


@remote_app.command("use")
def remote_use(name: str) -> None:
    """Set the default profile for later CLI and MCP calls."""
    store = ConfigStore()
    name, _ = store.resolve(name)
    configuration = store.load()
    configuration.default = name
    store.save(configuration)
    emit(Envelope(ok=True, data={"default": name}))


@remote_app.command("connect")
def remote_connect(context: typer.Context, name: Annotated[str, typer.Argument()] = "") -> None:
    """Verify SSH and install the credential-preserving remote worker."""
    invoke(context, "remote_connect", profile=name or context.obj.profile)


@remote_app.command("login")
def remote_login(context: typer.Context, name: Annotated[str, typer.Argument()] = "") -> None:
    """Open an interactive SSH login for host verification or one-time password entry."""
    typer.echo(
        "Enter credentials directly in SSH. Exit the shell when ready; the shared connection remains briefly.", err=True
    )
    Service().transport(name or context.obj.profile).login()


@remote_app.command("key-setup")
def remote_key_setup(context: typer.Context, name: Annotated[str, typer.Argument()] = "") -> None:
    """Install a dedicated key through existing access and verify it without a shared session."""
    invoke(context, "remote_key_setup", profile=name or context.obj.profile)


@remote_app.command("disconnect")
def remote_disconnect(context: typer.Context, name: Annotated[str, typer.Argument()] = "") -> None:
    """Close only evoctl's SSH connection and local GUI forwards."""
    invoke(context, "remote_disconnect", profile=name or context.obj.profile)


@app.command("status")
def status(
    context: typer.Context,
    watch: bool = False,
    interval: Annotated[float, typer.Option(min=1, max=3600)] = 5,
    count: Annotated[int, typer.Option(min=0, help="Watch samples; zero continues until interrupted.")] = 0,
) -> None:
    """Inspect readiness; --watch emits one JSON snapshot per line."""
    service = Service()
    iteration = 0
    while True:
        result = service.invoke("status", {"profile": context.obj.profile})
        emit(result, context.obj.output_file)
        iteration += 1
        if not watch or (count and iteration >= count):
            if result.ok and not result.data["ready"]:
                raise typer.Exit(1)
            return
        time.sleep(interval)


@app.command("doctor")
def doctor(context: typer.Context) -> None:
    """Inspect the same readiness stages and actionable failure hints as status."""
    status(context)


@contacts_app.command("search")
def contacts_search(
    context: typer.Context,
    query: Annotated[str, typer.Argument()] = "",
    limit: int = 20,
    page: int = 1,
    scan_pages: int = 5,
    cursor: str = "",
) -> None:
    """Legacy contacts-only lookup. Use chats search to find people and groups together."""
    invoke(context, "contacts_search", query=query, limit=limit, page=page, scan_pages=scan_pages, cursor=cursor)


@chats_app.command("search")
def chats_search(
    context: typer.Context,
    query: Annotated[str, typer.Argument()] = "",
    kind: str = "all",
    limit: int = 20,
    scan_pages: int = 5,
    group_timeout: float = 120.0,
    cursor: str = "",
) -> None:
    """Find people and groups by name, subject, JID or phone; follow next_cursor while incomplete."""
    invoke(
        context,
        "chats_search",
        query=query,
        kind=kind,
        limit=limit,
        scan_pages=scan_pages,
        group_timeout=group_timeout,
        cursor=cursor,
    )


@chats_app.command("list")
def chats_list(context: typer.Context, limit: int = 20, offset: int = 0) -> None:
    """Read a page of conversations without marking them read."""
    invoke(context, "chats_list", limit=limit, offset=offset)


@messages_app.command("read")
def messages_read(context: typer.Context, chat: str, limit: int = 20, page: int = 1, since: str = "") -> None:
    """Read bounded history for one exact JID or international number."""
    invoke(context, "messages_read", chat=chat, limit=limit, page=page, since=since)


@messages_app.command("send")
def messages_send(
    context: typer.Context,
    to: str,
    text: Annotated[str | None, typer.Option(help="Exact message text.")] = None,
    text_file: Annotated[str | None, typer.Option(help="Read exact text from a file; '-' reads stdin.")] = None,
    request_id: Annotated[str, typer.Option(help="Stable idempotency key; generated once if omitted.")] = "",
    dry_run: bool = False,
) -> None:
    """Send one message. Accepted or pending does not mean delivered."""
    if (text is None) == (text_file is None):
        raise EvoError("INVALID_INPUT", "Provide exactly one of --text or --text-file.")
    if text_file is not None:
        text = sys.stdin.read() if text_file == "-" else Path(text_file).read_text()
    invoke(context, "message_send", to=to, text=text, request_id=request_id or str(uuid.uuid4()), dry_run=dry_run)


@messages_app.command("status")
def messages_status(context: typer.Context, message_id: str) -> None:
    """Check delivery and read evidence for a WhatsApp message ID."""
    invoke(context, "message_status", message_id=message_id)


@messages_app.command("request")
def messages_request(context: typer.Context, request_id: str) -> None:
    """Recover a local send receipt without resending."""
    invoke(context, "request_status", request_id=request_id)


@app.command("pair")
def pair(
    context: typer.Context,
    open_image: Annotated[bool, typer.Option("--open", help="Open the QR in the default image viewer.")] = False,
) -> None:
    """Prepare an instance QR; scan it through WhatsApp's Linked devices screen."""
    result = Service().invoke("instance_pair", {"profile": context.obj.profile})
    if result.ok and open_image and result.data["path"]:
        webbrowser.open(Path(result.data["path"]).as_uri())
    emit(result, context.obj.output_file)


@app.command("ui")
def ui(context: typer.Context, open_browser: Annotated[bool, typer.Option("--open")] = False) -> None:
    """Return the management GUI URL, with a private SSH forward when needed."""
    result = Service().invoke("manager_url", {"profile": context.obj.profile})
    if result.ok and open_browser:
        webbrowser.open(result.data["url"])
    emit(result, context.obj.output_file)


@services_app.command("start")
def services_start(context: typer.Context) -> None:
    """Start existing containers; this never creates a new empty deployment."""
    invoke(context, "services_manage", operation="start")


@services_app.command("restart")
def services_restart(context: typer.Context) -> None:
    """Restart only the configured deployment's existing containers."""
    invoke(context, "services_manage", operation="restart")


@api_app.command("list")
def api_list(context: typer.Context, query: str = "", limit: int = 20, offset: int = 0) -> None:
    """Search the full REST catalog offline. Follow next_offset to continue."""
    invoke(context, "api_search", query=query, limit=limit, offset=offset)


@api_app.command("schema")
def api_schema(context: typer.Context, operation: str) -> None:
    """Inspect an operation's request body, parameters, and capability requirement."""
    invoke(context, "api_schema", operation=operation)


def read_json(value: str, object_only: bool = True) -> Any:
    """Parse inline JSON, @file, or stdin without shell interpolation."""
    raw = sys.stdin.read() if value == "-" else Path(value[1:]).read_text() if value.startswith("@") else value
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise EvoError("INVALID_INPUT", "Expected valid JSON, @filename, or '-' for stdin.") from None
    if object_only and not isinstance(result, dict):
        raise EvoError("INVALID_INPUT", "Expected a JSON object.")
    return result


@api_app.command("call")
def api_call(
    context: typer.Context,
    operation: str,
    body: Annotated[str | None, typer.Option(help="JSON object, @file, or '-' for stdin.")] = None,
    params: Annotated[str, typer.Option(help="JSON object containing named path parameters.")] = "{}",
    query: Annotated[str, typer.Option(help="JSON object containing query parameters.")] = "{}",
    authorization_env: Annotated[str, typer.Option(help="API-host variable containing an Authorization header.")] = "",
    request_id: str = "",
    dry_run: bool = False,
    max_bytes: int = 1_048_576,
) -> None:
    """Call an exact catalog operation; mutations use a durable request ID."""
    service = Service()
    access = service.catalog.operation(operation)["access"]
    invoke(
        context,
        f"api_{access}",
        operation=operation,
        body=read_json(body) if body is not None else None,
        path_parameters=read_json(params),
        query=read_json(query),
        authorization_env=authorization_env,
        request_id=request_id or (str(uuid.uuid4()) if access != "read" else ""),
        dry_run=dry_run,
        max_bytes=max_bytes,
    )


@mcp_app.command("serve")
def mcp_serve(
    context: typer.Context,
    mode: Annotated[
        Mode, typer.Option(help="Expose read tools, messaging writes, or administrative operations.")
    ] = "read",
) -> None:
    """Run the MCP stdio server. stdout contains protocol messages only."""
    asyncio.run(serve(Service(mode=mode, default_profile=context.obj.profile)))


@mcp_app.command("config")
def mcp_config(context: typer.Context, mode: Mode = "read") -> None:
    """Print a credential-free MCP client configuration snippet."""
    arguments = ["--profile", context.obj.profile] if context.obj.profile else []
    arguments += ["mcp", "serve", "--mode", mode]
    emit(Envelope(ok=True, data={"mcpServers": {"evoctl": {"command": "evoctl", "args": arguments}}}))


def main() -> None:
    """Keep usage and execution failures structured and prevent credential-bearing tracebacks."""
    try:
        result = app(standalone_mode=False)
        if isinstance(result, int):
            raise SystemExit(result)
    except typer.Exit as error:
        raise SystemExit(error.exit_code) from None
    except EvoError as error:
        emit(Envelope(ok=False, error=error.as_dict()))
    except ValidationError as error:
        fields = [".".join(map(str, item["loc"])) for item in error.errors(include_input=False)]
        emit(Envelope(ok=False, error=EvoError("INVALID_INPUT", "Invalid settings: " + ", ".join(fields)).as_dict()))
    except ClickException:
        emit(
            Envelope(
                ok=False,
                error=EvoError(
                    "INVALID_INPUT", "Invalid command arguments.", "Use --help for the command contract."
                ).as_dict(),
            )
        )
    except (OSError, ValueError):
        emit(
            Envelope(
                ok=False, error=EvoError("LOCAL_IO", "A local file or executable could not be accessed.").as_dict()
            )
        )
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
