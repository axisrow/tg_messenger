"""CLI serve commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import logging
import os

import click

from tg_messenger.cli import main as cli_main

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_local_host(host: str) -> bool:
    """True for loopback-only binds (127.0.0.1 / localhost / ::1)."""
    return host in _LOCAL_HOSTS


@click.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=lambda: int(os.environ.get("TG_WEB_PORT", "8090")), type=int)
@click.option("--session", default="default")
@click.option("--insecure", is_flag=True,
              help="Serve a non-localhost host without TG_WEB_PASS (anyone can connect).")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int, session: str, insecure: bool) -> None:
    """Launch the web interface.

    Set ``TG_WEB_PASS`` to require a password (HMAC-cookie session). Binding to a
    non-localhost host without it is refused unless ``--insecure`` is passed.
    """
    try:
        import uvicorn

        from tg_messenger.web.app import build_app
    except ImportError as exc:
        # base install omits the web stack — point at the extra instead of a raw ImportError
        raise click.ClickException("web UI requires: pip install 'tg-messenger[web]'") from exc

    web_pass = os.environ.get("TG_WEB_PASS") or None
    if not _is_local_host(host) and not web_pass and not insecure:
        raise click.ClickException(
            f"refusing to serve on {host} without TG_WEB_PASS — set it or pass --insecure"
        )
    if not _is_local_host(host) and not web_pass and insecure:
        logger.warning("serving on %s without TG_WEB_PASS (--insecure) — anyone can connect", host)

    session = cli_main._effective_session(ctx, session)
    cli_main._announce_tracing()  # #168: status + fail-fast (the web app traces the translator/suggester)
    client = cli_main.make_client(session_name=session)
    suggester = cli_main.make_optional_suggester(client, session=session)
    translator, outbound, store, _storage = cli_main.make_translation_deps(client, session=session)
    # uvicorn's own banner goes to the file (log_config=None) — announce the URL here
    click.echo(f"Serving on http://{host}:{port} — Ctrl+C to stop.")
    try:
        uvicorn.run(
            build_app(
                client=client,
                session_name=session,
                suggester=suggester,
                web_pass=web_pass,
                store=store,
                translator=translator,
                outbound=outbound,
            ),
            host=host,
            port=port,
            log_config=None,
        )
    finally:
        cli_main.flush_tracers()  # #168: drain buffered LangSmith traces when the server stops


COMMANDS = [serve]
