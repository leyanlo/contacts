from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import messages_histogram as messages

from . import actions, store


def index_html() -> str:
    return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


class ContactsCleanupHandler(BaseHTTPRequestHandler):
    contacts_db_paths: list[Path] = []
    messages_db: Path = Path.home() / "Library/Messages/chat.db"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(index_html())
            return
        if parsed.path == "/api/contacts":
            params = parse_qs(parsed.query)
            raw_years = params.get("years", ["10"])[0]
            years = None if raw_years == "all" else int(raw_years)
            refresh = params.get("refresh", ["0"])[0] == "1"
            try:
                payload = store.cached_payload(self.contacts_db_paths, self.messages_db, years, refresh)
            except Exception as exc:
                self.send_json({"warnings": [html.escape(str(exc))], "contacts": [], "summary": {}}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/delete", "/api/prune", "/api/merge"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            if parsed.path in ("/api/delete", "/api/prune"):
                identifiers = payload.get("identifiers", [])
                if not isinstance(identifiers, list) or not all(isinstance(item, str) for item in identifiers):
                    raise ValueError("identifiers must be a list of strings")
                dry_run = bool(payload.get("dryRun"))
                action = "prune" if parsed.path == "/api/prune" else "delete"
                backup_path = actions.write_backup(f"{action}-dry-run" if dry_run else action, payload.get("contacts", []), payload)
                result = actions.delete_contacts_with_contacts_app(identifiers, dry_run=dry_run)
                result["backupPath"] = backup_path
            else:
                if not isinstance(payload, dict):
                    raise ValueError("merge payload must be an object")
                dry_run = bool(payload.get("dryRun"))
                backup_path = actions.write_backup("merge-dry-run" if dry_run else "merge", payload.get("contacts", []), payload)
                result = actions.merge_contacts_with_contacts_app(payload, dry_run=dry_run)
                result["backupPath"] = backup_path
        except Exception as exc:
            self.send_json({"requested": 0, "deleted": 0, "missing": [], "stillPresent": [], "errors": [str(exc)]}, HTTPStatus.BAD_REQUEST)
            return
        self.send_json(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--messages-db",
        type=Path,
        default=Path.home() / "Library/Messages/chat.db",
    )
    parser.add_argument(
        "--contacts-db",
        action="append",
        type=Path,
        help="Optional Contacts AddressBook-v*.abcddb path. Repeat to pass more than one.",
    )
    parser.add_argument("--open", action="store_true", help="Open the app in your default browser.")
    args = parser.parse_args()

    ContactsCleanupHandler.contacts_db_paths = args.contacts_db or messages.default_contacts_dbs()
    ContactsCleanupHandler.messages_db = args.messages_db

    server = ThreadingHTTPServer((args.host, args.port), ContactsCleanupHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Contacts cleanup app: {url}")
    print("Read-only mode. Press Ctrl-C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
