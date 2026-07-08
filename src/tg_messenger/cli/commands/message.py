"""CLI message commands (#179 split). Relocated from cli/main.py verbatim; seams and runtime
helpers are dereferenced through the ``main`` module so monkeypatch.setattr(cli_main, ...) reaches
them. Registered onto the root ``cli`` group from main.py via the ``COMMANDS`` list."""

from __future__ import annotations

import os

import click

from tg_messenger.cli import main as cli_main
from tg_messenger.core.client import is_channel_or_megagroup_id


@click.command()
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
@click.option("--groups", is_flag=True, help="List groups/channels/bots instead of DMs.")
@click.option("--find", "find", default=None,
              help="Filter dialogs locally by title/username/id (no network).")
def dialogs(session: str, groups: bool, find: str | None) -> None:
    """List your dialogs (DMs by default; --groups for groups/channels/bots).

    The first (tab-separated) column is the DIALOG_ID other commands (read/send/react/…)
    need. ``--find`` filters the already-fetched list locally (title substring, username
    with/without @, or id) — no extra request.
    """
    from tg_messenger.core.search import filter_dialogs

    async def _do(client):
        return await (client.group_dialogs() if groups else client.dialogs())

    click.echo("Loading dialogs…", err=True)  # #187: a one-line status before the blocking fetch
    items = cli_main._run(cli_main._with_client(session, _do), session=session)
    if find is not None:
        items = filter_dialogs(items, find)
    for d in items:
        unread = f" ({d.unread} unread)" if d.unread else ""
        uname = f" @{d.username}" if d.username else ""
        kind = f" [{d.kind}]" if groups else ""  # одна вкладка смешивает виды — пометить
        # keep the historical raw id<TAB>title contract on stdout (a --porcelain flag +
        # a human-labelled default is deferred to a follow-up, see the PR body).
        click.echo(f"{d.id}\t{d.title}{uname}{kind}{unread}")
    # #187: a count on stderr tells a human the list isn't silently truncated, without
    # touching the stdout format a pipe/script may already parse.
    click.echo(f"{len(items)} dialog(s).", err=True)


@click.command()
@click.argument("dialog_id", type=int)
@click.argument("query")
@click.option("--limit", default=20, help="Max number of messages to return.")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def search(dialog_id: int, query: str, limit: int, session: str) -> None:
    """Search messages inside a dialog (Telegram's own server-side search).

    Get DIALOG_ID from `tg-messenger dialogs`.
    """

    async def _do(client):
        return await client.search_messages(dialog_id, query, limit=limit)

    messages = cli_main._run(cli_main._with_client(session, _do), session=session)
    if not messages:
        # #187: an empty result must say so, not print nothing and exit 0 (can't tell
        # "no matches" from "the command silently failed"); mirrors "No plans."/"No rules."
        click.echo("No matching messages.")
        return
    for m in messages:
        click.echo(cli_main.message_line(m))


@click.command()
@click.argument("dialog_id", type=int)
@click.option("--limit", default=50)
@click.option("--download", "download_dir", default=None,
              help="Download media of each message into this directory.")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def read(dialog_id: int, limit: int, download_dir: str | None, session: str) -> None:
    """Print the message history of a dialog (and optionally download media).

    Get DIALOG_ID from `tg-messenger dialogs`. Each printed line starts with the
    MESSAGE_ID that edit/react/delete take.
    """

    async def _do(client):
        store, storage = cli_main.make_message_store(client, session=session)
        translator = cli_main.make_optional_translator(storage)
        if download_dir:
            os.makedirs(download_dir, exist_ok=True)
        try:
            messages = await store.history(dialog_id, limit=limit)
            if not messages:
                # #187: an empty history must say so, not print nothing and exit 0
                click.echo("No messages.")
                return
            messages = await cli_main._maybe_translate_history(translator, dialog_id, messages)
            for m in messages:
                cli_main._print_message_with_translation(m)
                if download_dir and m.media is not None and m.media.downloadable:
                    dest = os.path.join(download_dir, f"{dialog_id}_{m.id}")
                    saved = await client.download_message_media(dialog_id, m.id, dest)
                    if saved:
                        click.echo(f"  saved: {saved}")
        finally:
            await store.close()

    click.echo("Loading history…", err=True)  # #187: status before the blocking fetch
    cli_main._run(cli_main._with_client(session, _do), session=session)


@click.command()
@click.argument("code", required=False)
@click.option("--clear", "clear", is_flag=True, help="Clear the stored language override.")
@click.option(
    "--mode",
    "mode",
    type=click.Choice(["off", "all_unknown", "skip_known", "only_unknown"]),
    default=None,
    help="Inbound translation mode (off / all_unknown / skip_known / only_unknown).",
)
@click.option("--known", "known", default=None,
              help="Comma/space-separated languages you KNOW (not translated).")
@click.option("--unknown", "unknown", default=None,
              help="Comma/space-separated languages to translate (only_unknown mode).")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
@click.pass_context
def lang(
    ctx: click.Context,
    code: str | None,
    clear: bool,
    mode: str | None,
    known: str | None,
    unknown: str | None,
    session: str,
) -> None:
    """Show or set inbound translation: target language (CODE), mode, and known/unknown lists."""
    from tg_messenger.agent.translate import (
        USER_LANG_KEY,
        get_known_langs,
        get_translate_mode,
        get_unknown_langs,
        get_user_lang,
        set_known_langs,
        set_translate_mode,
        set_unknown_langs,
        set_user_lang,
    )
    from tg_messenger.core.languages import parse_lang_codes, validate_supported_lang_code

    if clear and code is not None:
        raise click.ClickException("CODE and --clear are mutually exclusive")
    language_code = None
    if code is not None:
        try:
            language_code = validate_supported_lang_code(code)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    # parse lists up front so a bad code fails before touching the DB
    try:
        known_codes = parse_lang_codes(known) if known is not None else None
        unknown_codes = parse_lang_codes(unknown) if unknown is not None else None
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    session = cli_main._effective_session(ctx, session)
    mutating = clear or language_code is not None or mode is not None \
        or known_codes is not None or unknown_codes is not None

    async def _do(storage):
        if clear:
            await set_user_lang(storage, None)
            await set_translate_mode(storage, "off")
            click.echo("language override cleared.")
            return
        if mutating:
            if language_code is not None:
                await set_user_lang(storage, language_code)
            if known_codes is not None:
                await set_known_langs(storage, known_codes)
            if unknown_codes is not None:
                await set_unknown_langs(storage, unknown_codes)
            if mode is not None:
                await set_translate_mode(storage, mode)
            click.echo("translation settings updated.")
            return
        stored = await storage.get_value(USER_LANG_KEY)
        effective = await get_user_lang(storage)
        if stored:
            source = "kv"
        elif os.environ.get("TG_USER_LANG"):
            source = "env"
        else:
            source = "unset"
        eff_mode = await get_translate_mode(storage)
        eff_known = await get_known_langs(storage)
        eff_unknown = await get_unknown_langs(storage)
        click.echo(f"{effective or 'unset'}\t{source}")
        click.echo(f"mode\t{eff_mode}")
        click.echo(f"known\t{', '.join(eff_known) or '-'}")
        click.echo(f"unknown\t{', '.join(eff_unknown) or '-'}")

    cli_main._run(cli_main._with_storage(session, lambda storage: None, _do), session=session)


@click.command("dialog-lang")
@click.argument("dialog_id", type=int)
@click.argument("code", required=False)
@click.option("--auto", "auto", is_flag=True, help="Clear manual language override.")
@click.option("--on", "turn_on", is_flag=True, help="Enable outbound translation for this dialog.")
@click.option("--off", "turn_off", is_flag=True, help="Disable outbound translation for this dialog.")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
@click.pass_context
def dialog_lang(
    ctx: click.Context,
    dialog_id: int,
    code: str | None,
    auto: bool,
    turn_on: bool,
    turn_off: bool,
    session: str,
) -> None:
    """Show or override a dialog language and outbound translation flag."""
    from tg_messenger.agent.outbound import (
        get_dialog_lang,
        is_outbound_enabled,
        set_dialog_lang,
        set_outbound_enabled,
    )
    from tg_messenger.core.languages import validate_supported_lang_code

    if sum([code is not None, auto]) > 1:
        raise click.ClickException("CODE and --auto are mutually exclusive")
    if turn_on and turn_off:
        raise click.ClickException("--on and --off are mutually exclusive")
    language_code = None
    if code is not None:
        try:
            language_code = validate_supported_lang_code(code)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    session = cli_main._effective_session(ctx, session)

    async def _do(storage):
        if language_code is not None:
            await set_dialog_lang(storage, dialog_id, language_code, source="manual")
        if auto:
            await set_dialog_lang(storage, dialog_id, None)
        if turn_on:
            await set_outbound_enabled(storage, dialog_id, True)
        if turn_off:
            await set_outbound_enabled(storage, dialog_id, False)
        lang_info = await get_dialog_lang(storage, dialog_id)
        enabled = await is_outbound_enabled(storage, dialog_id)
        if lang_info is None:
            click.echo(f"{dialog_id}\tlang=unset\toutbound={'on' if enabled else 'off'}")
        else:
            click.echo(
                f"{dialog_id}\tlang={lang_info.lang}\tsource={lang_info.source}"
                f"\toutbound={'on' if enabled else 'off'}"
            )

    cli_main._run(cli_main._with_storage(session, lambda storage: None, _do), session=session)


@click.command()
@click.argument("dialog_id", type=int)
@click.argument("text", required=False)
@click.option("--file", "file_path", default=None, help="Send a file/photo instead of text.")
@click.option("--caption", "caption", default=None,
              help="Caption for --file (overrides the positional TEXT).")
@click.option("--voice", "voice", is_flag=True, help="Send --file as a voice note.")
@click.option("--video-note", "video_note", is_flag=True,
              help="Send --file as a round video note.")
@click.option("--as-file", "as_file", is_flag=True,
              help="Send --file as a plain document (no media preview).")
@click.option("--reply-to", "reply_to", type=int, default=None,
              help="Reply to this message id.")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def send(dialog_id: int, text: str | None, file_path: str | None, caption: str | None,
         voice: bool, video_note: bool, as_file: bool,
         reply_to: int | None, session: str) -> None:
    """Send a text message (or a file with --file); --reply-to to quote a message.

    --voice / --video-note / --as-file are mutually exclusive media modifiers.
    Caption comes from --caption or, failing that, the positional TEXT.
    Get DIALOG_ID from `tg-messenger dialogs`.
    """
    if sum([voice, video_note, as_file]) > 1:
        raise click.ClickException(
            "--voice, --video-note and --as-file are mutually exclusive"
        )
    if not file_path and not text:
        # #187: `send 7` with no TEXT and no --file would call send_text(7, "") — Telegram
        # rejects it with an opaque "Unexpected error". Validate up front, before the network.
        raise click.ClickException("provide TEXT or --file")

    async def _do(client):
        # No pre-flight dialog fetch: a one-shot CLI process has a cold cache, so a
        # read-only check would cost a full dialog list every time. send_media's own
        # offline path check runs first; the core SendForbiddenError seam (mapped in
        # _run) is the authoritative net for a read-only chat.
        if file_path:
            return await client.send_media(
                dialog_id, file_path, caption=caption or text,
                voice_note=voice, video_note=video_note, force_document=as_file,
            )
        return await client.send_text(dialog_id, text or "", reply_to=reply_to)

    msg = cli_main._run(cli_main._with_client(session, _do), session=session)
    # #187: echo the returned id so a follow-up edit/react has an id to use without a read
    click.echo(f"sent. [id={msg.id}]" if msg is not None else "sent.")


@click.command()
@click.argument("dialog_id", type=int)
@click.argument("message_id", type=int)
@click.argument("emoticon")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def react(dialog_id: int, message_id: int, emoticon: str, session: str) -> None:
    """React to a message with a standard emoji.

    Get DIALOG_ID from `tg-messenger dialogs` and MESSAGE_ID from `tg-messenger read`.
    """

    async def _do(client):
        # No pre-flight gate: reactions are a separate capability from posting, and a
        # one-shot CLI has a cold cache. Telegram rejects (→ SendForbiddenError) if the
        # channel truly forbids reactions. Proper per-message reaction UI: issue #86.
        await client.send_reaction(dialog_id, message_id, emoticon)

    cli_main._run(cli_main._with_client(session, _do), session=session)
    click.echo(f"reacted to [id={message_id}].")  # #187: name the affected message


@click.command()
@click.argument("from_peer", type=int)
@click.argument("ids")
@click.argument("to_peer", type=int)
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def forward(from_peer: int, ids: str, to_peer: int, session: str) -> None:
    """Forward messages (comma-separated IDS) from FROM_PEER to TO_PEER.

    Get FROM_PEER/TO_PEER from `tg-messenger dialogs` and the IDS from `tg-messenger read`.
    """
    message_ids = cli_main._parse_ids(ids)

    async def _do(client):
        return await client.forward(from_peer, message_ids, to_peer)

    forwarded = cli_main._run(cli_main._with_client(session, _do), session=session)
    # #187: client.forward returns only the messages Telegram actually forwarded — a
    # partial drop must not be reported as full success. Report N of M; when some were
    # dropped, surface the requested id set on stderr (the returned messages carry NEW
    # destination ids, so which SOURCE ids dropped can't be mapped reliably — say how
    # many, not a guessed list).
    n, m = len(forwarded or []), len(message_ids)
    click.echo(f"forwarded {n} of {m} to {to_peer}.")
    if n < m:
        click.echo(
            f"{m - n} of {m} not forwarded (requested ids: "
            f"{', '.join(str(i) for i in message_ids)}) — see the log for details.",
            err=True,
        )


@click.command()
@click.argument("dialog_id", type=int)
@click.argument("message_id", type=int)
@click.argument("text")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def edit(dialog_id: int, message_id: int, text: str, session: str) -> None:
    """Edit the text of one of your messages.

    Get DIALOG_ID from `tg-messenger dialogs` and MESSAGE_ID from `tg-messenger read`.
    """

    async def _do(client):
        return await client.edit_text(dialog_id, message_id, text)

    cli_main._run(cli_main._with_client(session, _do), session=session)
    click.echo(f"edited. [id={message_id}]")  # #187: name the affected message


@click.command()
@click.argument("dialog_id", type=int)
@click.argument("ids")
@click.option("--for-me", "for_me", is_flag=True,
              help="Delete only for yourself (don't revoke for everyone).")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def delete(dialog_id: int, ids: str, for_me: bool, yes: bool, session: str) -> None:
    """Delete messages (comma-separated IDS); --for-me to keep them for others.

    Deletes for EVERYONE by default (irreversible). Get DIALOG_ID from
    `tg-messenger dialogs` and the IDS from `tg-messenger read`.
    """
    message_ids = cli_main._parse_ids(ids)
    if for_me and is_channel_or_megagroup_id(dialog_id):
        raise click.ClickException(
            "--for-me is not supported for channels/supergroups; Telegram deletes there for everyone"
        )
    # #187: a destructive delete (everyone by default) gates on a confirm like logout/
    # profiles remove — stating count, peer and scope — unless --yes is passed.
    scope = "for me" if for_me else "for everyone"
    if not yes:
        click.confirm(
            f"Delete {len(message_ids)} message(s) {scope} in {dialog_id}?",
            abort=True,
        )

    async def _do(client):
        return await client.delete_messages(dialog_id, message_ids, revoke=not for_me)

    cli_main._run(cli_main._with_client(session, _do), session=session)
    click.echo(f"deleted {len(message_ids)} message(s) {scope} in {dialog_id}.")


@click.command("mark-read")
@click.argument("dialog_id", type=int)
@click.option("--session", default="default", help=cli_main.SESSION_OPTION_HELP)
def mark_read(dialog_id: int, session: str) -> None:
    """Mark a dialog as read (clears its unread counter)."""

    async def _do(client):
        return await client.mark_read(dialog_id)

    cli_main._run(cli_main._with_client(session, _do), session=session)
    click.echo("marked read.")


COMMANDS = [dialogs, search, read, lang, dialog_lang, send, react, forward, edit, delete, mark_read]
