"""gmail-tui command line.

``list`` reads the local index only. ``sync`` prints that index first, then
runs history pull, new-mail download, mutation push, and body backfill.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from gmail_tui.accounts import cli_store_dir, discover, resolve_account_dir
from gmail_tui.gmail import GmailError, NullGmail, Offline
from gmail_tui.http_gmail import HttpGmail
from gmail_tui.oauth import (
    OAuthError,
    PendingLogin,
    current_access_token,
    exchange_code,
    load,
    read_client_file,
    store_tokens,
)
from gmail_tui import oauth_tpm
from gmail_tui.settings import (
    Settings,
    SettingsError,
    config_path,
    default_data,
    load as load_settings,
    resolve_config_path,
    resolve_data_dir,
    resolve_open_command,
)
from gmail_tui.store import MessageNotFound, Store, ThreadCursor
from gmail_tui import profile
from gmail_tui.sync import Engine
from gmail_tui.tui import run_tui


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gmail-tui", description="Online-first Gmail client")
    parser.add_argument(
        "--config",
        type=Path,
        help="settings file or directory (or set GMAIL_TUI_CONFIG; default: ~/.config/gmail-tui/config)",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="directory for the index and body cache (default: config data, or ~/.local/share/gmail-tui)",
    )
    parser.add_argument("--token", help="Gmail OAuth access token (or set GMAIL_TUI_TOKEN)")
    parser.add_argument(
        "--keys",
        type=Path,
        help="keyboard bindings file (or set GMAIL_TUI_KEYS; default: beside --config, or ~/.config/gmail-tui/keys)",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help=(
            "use <data>/NAME/ for the mail index and tokens "
            "(or set GMAIL_TUI_PROFILE); config and keys stay shared"
        ),
    )
    parser.add_argument(
        "--perf",
        action="store_true",
        help="append CPU, memory, and per-message timings to <data>/profile.log",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="open the three-pane terminal interface")
    sub.add_parser("init", help="create the local index")
    login = sub.add_parser(
        "login",
        help="open Google's consent page; tokens go to the mailbox for that Google account",
    )
    _add_client_args(login)
    extra = sub.add_parser(
        "add-account",
        help="same as login (kept for older scripts); routes by the signed-in email",
    )
    _add_client_args(extra)

    listing = sub.add_parser("list", help="list threads from the local index")
    listing.add_argument("--label", help="Gmail label id (default: last opened label, or INBOX)")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--before-date", type=int, help="keyset cursor: internal date in milliseconds")
    listing.add_argument("--before-id", type=int, help="keyset cursor: local thread id")

    sub.add_parser("sync", help="refresh from Gmail, then push queued label changes")

    search = sub.add_parser("search", help="search local mail and Gmail, without downloading bodies")
    search.add_argument("query", nargs="+")
    search.add_argument("--limit", type=int, default=50)

    open_cmd = sub.add_parser("open", help="show a message, downloading its body if needed")
    open_cmd.add_argument("gmail_id")

    label = sub.add_parser("label", help="change labels locally and push them if a token is set")
    label.add_argument("gmail_id")
    label.add_argument("--add", action="append", default=[])
    label.add_argument("--remove", action="append", default=[])

    args = parser.parse_args(argv)
    settings_file = resolve_config_path(args.config)
    try:
        settings = load_settings(settings_file)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.data is not None:
        data = Path(args.data).expanduser()
        if not data.is_absolute():
            data = Path.cwd() / data
    elif settings.data is not None:
        data = settings.data
    else:
        data = default_data()
    profile_name = args.profile if args.profile is not None else os.environ.get("GMAIL_TUI_PROFILE")
    try:
        data = resolve_data_dir(data, profile_name)
        opener = resolve_open_command(settings, settings_file, profile_name)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        if args.perf:
            profile.start(data / "profile.log")
            profile.attach()
        _setup_oauth_tpm(settings, settings_file, data, args.command)
        return _run(
            args,
            data,
            download=settings.download,
            settings_path=settings_file,
            open_command=opener,
        )
    except (GmailError, MessageNotFound, ValueError, OAuthError) as exc:
        print(exc, file=sys.stderr)
        return 1
    finally:
        profile.stop()


def _setup_oauth_tpm(
    settings: Settings,
    settings_file: Path,
    data: Path,
    command: str | None,
) -> None:
    """Ask for the TPM PIN once, then seal/load oauth tokens under that key."""
    if not settings.oauth_tpm:
        oauth_tpm.reset()
        return
    identity = settings.oauth_tpm_identity or oauth_tpm.default_identity_path(settings_file)
    oauth_tpm.prepare(identity)
    # Login creates tokens; other commands may need to decrypt existing ones.
    if command in {"login", "add-account", "init"}:
        return
    roots = [account.root for account in discover(data)]
    oauth_tpm.unlock_roots(roots)


def _keys_path(args: argparse.Namespace, settings_file: Path) -> Path | None:
    if args.keys is not None:
        path = args.keys.expanduser()
        return path if path.is_absolute() else Path.cwd() / path
    if os.environ.get("GMAIL_TUI_KEYS"):
        return None  # keys.config_path() honors the env var
    try:
        if settings_file.resolve() != config_path().resolve():
            return settings_file.parent / "keys"
    except OSError:
        if settings_file != config_path():
            return settings_file.parent / "keys"
    return None


def _run(
    args: argparse.Namespace,
    data: Path,
    *,
    download: Path | None = None,
    settings_path: Path | None = None,
    open_command: tuple[str, ...] = ("xdg-open",),
) -> int:
    if args.command == "login":
        return _login(args, data)
    if args.command == "add-account":
        return _add_account(args, data)
    if args.command in (None, "tui"):
        token = args.token or os.environ.get("GMAIL_TUI_TOKEN") or None
        keys = _keys_path(args, settings_path or config_path())
        run_tui(
            data,
            token=token,
            keys_path=keys,
            download_dir=download,
            settings_path=settings_path,
            open_command=open_command,
        )
        return 0
    settings = load_settings(settings_path) if settings_path is not None else None
    order = settings.accounts if settings is not None else ()
    mailbox = cli_store_dir(data, order)
    if args.command == "init":
        Store.open(mailbox).close()
        print(mailbox)
        return 0
    if args.command == "list":
        store = Store.open(mailbox)
        try:
            return _list(store, args)
        finally:
            store.close()
    gmail = _gmail(args, mailbox)
    if args.command == "sync" and isinstance(gmail, NullGmail):
        print("run gmail-tui login, or set --token / GMAIL_TUI_TOKEN", file=sys.stderr)
        return 1
    engine = Engine.open(mailbox, gmail)
    try:
        if args.command == "sync":
            label, threads = engine.local_threads()
            _print_threads(label, threads)
            report = engine.startup()
            print(
                "synced "
                f"history={report.history_records} "
                f"metadata={report.metadata_upserted} "
                f"bodies={report.bodies_downloaded} "
                f"pushed={report.mutations_pushed} "
                f"rebased={report.mutations_rebased} "
                f"backfill={report.backfill_downloaded}"
            )
            return 0
        if args.command == "search":
            response = engine.search(" ".join(args.query), args.limit)
            _print_hits(response.hits)
            if response.remote_error:
                print(f"remote search skipped: {response.remote_error}", file=sys.stderr)
            return 0
        if args.command == "open":
            try:
                opened = engine.open_message(args.gmail_id)
            except Offline as exc:
                print(exc, file=sys.stderr)
                return 1
            _print_opened(opened)
            return 0
        if args.command == "label":
            engine.store.apply_local_labels(args.gmail_id, args.add, args.remove)
            if not isinstance(gmail, NullGmail):
                pushed, rebased = engine.flush_queue()
                print(f"pushed={pushed} rebased={rebased}")
            else:
                print("queued")
            return 0
    finally:
        engine.store.close()
    print(f"unknown command {args.command}", file=sys.stderr)
    return 1


def _gmail(args: argparse.Namespace, data: Path):
    token = args.token or os.environ.get("GMAIL_TUI_TOKEN") or current_access_token(data)
    return HttpGmail(token) if token else NullGmail()


def _add_client_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--client-json", type=Path, help="Desktop OAuth client JSON from Google Cloud")
    parser.add_argument("--client-id", help="OAuth client id (or set GMAIL_TUI_CLIENT_ID)")
    parser.add_argument("--client-secret", help="OAuth client secret (or set GMAIL_TUI_CLIENT_SECRET)")
    parser.add_argument("--no-browser", action="store_true", help="print the link without opening a browser")


def _login(args: argparse.Namespace, data: Path) -> int:
    return _sign_in(args, data, label="signed in")


def _add_account(args: argparse.Namespace, data: Path) -> int:
    return _sign_in(args, data, label="added")


def _sign_in(args: argparse.Namespace, data: Path, *, label: str) -> int:
    """Authorize, then store tokens under the mailbox that matches the Google account."""
    authorized = _authorize(args, data)
    if authorized is None:
        return 1
    client_id, client_secret, token = authorized
    try:
        profile = HttpGmail(token.access_token).get_profile()
    except GmailError as exc:
        # Without a profile we cannot route to the right mailbox.
        print(exc, file=sys.stderr)
        return 1
    dest = resolve_account_dir(data, profile.email)
    store = Store.open(dest)
    try:
        current = store.account_email()
        if current and current.casefold() != profile.email.casefold():
            print(
                f"refusing to put {profile.email} tokens in a mailbox for {current}",
                file=sys.stderr,
            )
            return 1
        store_tokens(dest, client_id, client_secret, token)
        if not current:
            if store.bootstrap_phase() == "need_profile":
                store.set_profile(profile.email, profile.history_id, profile.messages_total)
            else:
                store.set_email(profile.email)
    finally:
        store.close()
    print(f"{label} as {profile.email}")
    print(dest)
    return 0


def _authorize(args: argparse.Namespace, data: Path):
    client_id, client_secret = _client_credentials(args, data)
    if not client_id or not client_secret:
        print(
            "Google requires a Desktop OAuth client from your Cloud project.\n"
            "Enable the Gmail API, add your account as a test user, create a Desktop\n"
            "client, and download its JSON. Then run:\n"
            "  gmail-tui login --client-json client_secret.json",
            file=sys.stderr,
        )
        return None
    pending = PendingLogin.start(client_id)
    print("Open this link to sign in:")
    print(pending.url)
    if not args.no_browser:
        try:
            webbrowser.open(pending.url)
        except webbrowser.Error:
            pass
    code = pending.wait(180)
    token = exchange_code(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=pending.redirect_uri,
        verifier=pending.verifier,
    )
    return client_id, client_secret, token


def _client_credentials(args: argparse.Namespace, data: Path) -> tuple[str, str]:
    if args.client_json:
        return read_client_file(args.client_json)
    client_id = args.client_id or os.environ.get("GMAIL_TUI_CLIENT_ID") or ""
    client_secret = args.client_secret or os.environ.get("GMAIL_TUI_CLIENT_SECRET") or ""
    if client_id and client_secret:
        return client_id, client_secret
    stored = load(data)
    if stored is None:
        from gmail_tui.accounts import discover

        for account in discover(data):
            stored = load(account.root)
            if stored is not None:
                break
    if stored is None:
        return client_id, client_secret
    return client_id or stored.client_id, client_secret or stored.client_secret


def _list(store: Store, args: argparse.Namespace) -> int:
    if args.label:
        store.set_open_label(args.label)
        label = args.label
    else:
        label = store.open_label()
    cursor = None
    if args.before_date is not None or args.before_id is not None:
        if args.before_date is None or args.before_id is None:
            print("--before-date and --before-id are a pair", file=sys.stderr)
            return 1
        cursor = ThreadCursor(args.before_date, args.before_id)
    threads = store.list_threads(label, cursor, args.limit)
    _print_threads(label, threads)
    return 0


def _print_threads(label: str, threads) -> None:
    print(f"# {label}")
    if not threads:
        print("(no messages)")
        return
    for thread in threads:
        flag = "U" if thread.unread_count else " "
        star = "*" if thread.starred else " "
        when = _format_ms(thread.latest_internal_date_ms)
        sender = (thread.from_addr or "")[:28]
        print(f"{flag}{star} {when}  {sender:<28}  {thread.subject}  [{thread.latest_message_gmail_id}]")


def _print_hits(hits) -> None:
    if not hits:
        print("(no matches)")
        return
    for hit in hits:
        mark = "*" if hit.body_cached else "."
        when = _format_ms(hit.internal_date_ms)
        print(f"{mark} {when}  {hit.from_addr[:28]:<28}  {hit.subject}  [{hit.gmail_id}]")


def _print_opened(opened) -> None:
    print(f"From: {opened.from_addr}")
    print(f"To: {opened.to_addr}")
    if opened.cc_addr:
        print(f"Cc: {opened.cc_addr}")
    print(f"Subject: {opened.subject}")
    print(f"Labels: {' '.join(opened.label_ids)}")
    print(f"Date: {_format_ms(opened.internal_date_ms)}")
    print()
    if opened.body_cached:
        print(opened.body_text)
    else:
        print("(body not cached)")


def _format_ms(value: int) -> str:
    from gmail_tui.formats import format_millis

    return format_millis(value)


if __name__ == "__main__":
    raise SystemExit(main())
