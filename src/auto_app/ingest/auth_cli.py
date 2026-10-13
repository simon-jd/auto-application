"""Interactive OAuth setup, separate from scheduled mailbox reads."""

from pathlib import Path
from typing import Annotated

import typer

from auto_app.ingest.mail_auth import MailAuthError, authorize_gmail, authorize_outlook

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Authorize read-only Gmail or Outlook access.")
CredentialsOption = Annotated[Path, typer.Option("--credentials", help="Private token JSON file to save; use an ignored credentials directory.")]


def register(parent: typer.Typer) -> None:
    parent.add_typer(app, name="auth")


def _authorize(operation) -> None:
    try:
        path = operation()
    except MailAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Authorized read-only mailbox access. Credentials saved to: {path}")


@app.command("gmail")
def gmail(client: Annotated[Path, typer.Option("--client", help="Downloaded Google Desktop app OAuth client JSON.")],
          credentials: CredentialsOption,
          no_browser: Annotated[bool, typer.Option("--no-browser", help="Print the authorization URL without opening a browser.")] = False,
          port: Annotated[int, typer.Option("--port", min=0, max=65535, help="Loopback callback port; 0 chooses an available port.")] = 0) -> None:
    """Authorize Gmail using a local browser callback (or an SSH forwarded port)."""
    _authorize(lambda: authorize_gmail(client, credentials, no_browser=no_browser, port=port, announce=typer.echo))


@app.command("outlook")
def outlook(client_id: Annotated[str, typer.Option("--client-id", help="Microsoft public-client application ID.")],
            credentials: CredentialsOption,
            tenant: Annotated[str, typer.Option("--tenant", help="common, consumers, organizations or tenant ID.")] = "common") -> None:
    """Authorize Outlook using a device code that can be entered on another computer."""
    _authorize(lambda: authorize_outlook(client_id, credentials, tenant=tenant, announce=typer.echo))
