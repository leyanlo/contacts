#!/usr/bin/env python3
"""
Generate a local histogram of Messages correspondents over the past N years.

This reads metadata from ~/Library/Messages/chat.db and does not select message
body text. It writes a CSV and a self-contained HTML/SVG bar chart.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


@dataclass
class Stats:
    handle: str
    name: str = ""
    sent_direct: int = 0
    sent_group: int = 0
    received_direct: int = 0
    received_from_them_group: int = 0
    shared_direct_messages: int = 0
    shared_group_messages: int = 0
    direct_chats: set[int] | None = None
    group_chats: set[int] | None = None
    last_sent_ns: int | None = None
    last_message_ns: int | None = None

    def __post_init__(self) -> None:
        if self.direct_chats is None:
            self.direct_chats = set()
        if self.group_chats is None:
            self.group_chats = set()

    @property
    def sent_total(self) -> int:
        return self.sent_direct + self.sent_group

    @property
    def received_from_them_total(self) -> int:
        return self.received_direct + self.received_from_them_group

    @property
    def shared_total(self) -> int:
        return self.shared_direct_messages + self.shared_group_messages

    @property
    def display_label(self) -> str:
        return self.name or self.handle


def years_ago(dt: datetime, years: int) -> datetime:
    try:
        return dt.replace(year=dt.year - years)
    except ValueError:
        return dt.replace(month=2, day=28, year=dt.year - years)


def dt_to_apple_ns(dt: datetime) -> int:
    return int((dt - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def apple_ns_to_dt(value: int | None) -> datetime | None:
    if value is None:
        return None
    # Messages date values are normally nanoseconds since 2001-01-01, but keep
    # this tolerant for older exports or migrated databases.
    seconds = value / 1_000_000_000 if abs(value) > 10_000_000_000 else value
    return datetime.fromtimestamp(APPLE_EPOCH.timestamp() + seconds, tz=timezone.utc)


def format_dt(value: int | None) -> str:
    dt = apple_ns_to_dt(value)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if dt else ""


def connect_messages_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def connect_readonly_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def normalize_email(value: str) -> str | None:
    value = value.strip().lower()
    if "@" not in value or " " in value:
        return None
    return value


def phone_match_keys(value: str) -> set[str]:
    digits = re.sub(r"\D", "", value)
    if not digits:
        return set()

    keys = {digits}
    if len(digits) == 11 and digits.startswith("1"):
        keys.add(digits[1:])
    if len(digits) >= 10:
        keys.add(digits[-10:])
    return keys


def handle_match_keys(value: str) -> set[str]:
    email = normalize_email(value)
    if email:
        return {f"email:{email}"}
    return {f"phone:{key}" for key in phone_match_keys(value)}


def contact_display_name(row: sqlite3.Row) -> str:
    parts = [
        str(row["ZFIRSTNAME"] or "").strip(),
        str(row["ZMIDDLENAME"] or "").strip(),
        str(row["ZLASTNAME"] or "").strip(),
    ]
    full_name = " ".join(part for part in parts if part)
    for candidate in (
        full_name,
        str(row["ZNAME"] or "").strip(),
        str(row["ZNICKNAME"] or "").strip(),
        str(row["ZORGANIZATION"] or "").strip(),
    ):
        if candidate:
            return candidate
    return ""


def default_contacts_dbs() -> list[Path]:
    base = Path.home() / "Library/Application Support/AddressBook"
    candidates = [base / "AddressBook-v22.abcddb"]
    candidates.extend(sorted((base / "Sources").glob("*/AddressBook-v*.abcddb")))
    return [path for path in candidates if path.exists()]


def add_contact_lookup_entry(
    lookup: dict[str, set[str]],
    key: str,
    name: str,
) -> None:
    if not key or not name:
        return
    lookup.setdefault(key, set()).add(name)


def build_contact_lookup(db_paths: list[Path]) -> tuple[dict[str, str], list[str]]:
    lookup: dict[str, set[str]] = {}
    warnings: list[str] = []
    record_columns = """
      r.ZFIRSTNAME,
      r.ZMIDDLENAME,
      r.ZLASTNAME,
      r.ZNAME,
      r.ZNICKNAME,
      r.ZORGANIZATION
    """

    for db_path in db_paths:
        try:
            with connect_readonly_db(db_path) as conn:
                for row in conn.execute(
                    f"""
                    SELECT p.ZFULLNUMBER, p.ZLOCALNUMBER, {record_columns}
                    FROM ZABCDPHONENUMBER p
                    JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER
                    """
                ):
                    name = contact_display_name(row)
                    for value in (row["ZFULLNUMBER"], row["ZLOCALNUMBER"]):
                        if not value:
                            continue
                        for key in phone_match_keys(str(value)):
                            add_contact_lookup_entry(lookup, f"phone:{key}", name)

                for row in conn.execute(
                    f"""
                    SELECT e.ZADDRESS, e.ZADDRESSNORMALIZED, {record_columns}
                    FROM ZABCDEMAILADDRESS e
                    JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER
                    """
                ):
                    name = contact_display_name(row)
                    for value in (row["ZADDRESS"], row["ZADDRESSNORMALIZED"]):
                        if not value:
                            continue
                        email = normalize_email(str(value))
                        if email:
                            add_contact_lookup_entry(lookup, f"email:{email}", name)
        except sqlite3.Error as exc:
            warnings.append(f"Could not read Contacts database {db_path}: {exc}")

    resolved = {}
    for key, names in lookup.items():
        ordered = sorted(names)
        if len(ordered) <= 3:
            resolved[key] = " / ".join(ordered)
        else:
            resolved[key] = " / ".join(ordered[:3]) + f" / +{len(ordered) - 3} more"
    return resolved, warnings


def contact_name_for_handle(handle: str, lookup: dict[str, str]) -> str:
    for key in handle_match_keys(handle):
        if key in lookup:
            return lookup[key]
    return ""


def apply_contact_names(stats: dict[int, Stats], lookup: dict[str, str]) -> None:
    for item in stats.values():
        item.name = contact_name_for_handle(item.handle, lookup)


def one_to_one_chat_handles(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT chat_id, MIN(handle_id) AS handle_id
        FROM chat_handle_join
        GROUP BY chat_id
        HAVING COUNT(DISTINCT handle_id) = 1
        """
    )
    return {int(row["chat_id"]): int(row["handle_id"]) for row in rows}


def group_chat_handles(conn: sqlite3.Connection) -> dict[int, list[int]]:
    rows = conn.execute(
        """
        SELECT chat_id, handle_id
        FROM chat_handle_join
        WHERE chat_id IN (
          SELECT chat_id
          FROM chat_handle_join
          GROUP BY chat_id
          HAVING COUNT(DISTINCT handle_id) > 1
        )
        ORDER BY chat_id, handle_id
        """
    )
    grouped: dict[int, list[int]] = defaultdict(list)
    for row in rows:
        grouped[int(row["chat_id"])].append(int(row["handle_id"]))
    return grouped


def handle_labels(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(row["ROWID"]): str(row["id"])
        for row in conn.execute("SELECT ROWID, id FROM handle")
        if row["id"]
    }


def collect_stats(conn: sqlite3.Connection, cutoff_ns: int) -> dict[int, Stats]:
    labels = handle_labels(conn)
    direct_chats = one_to_one_chat_handles(conn)
    group_chats = group_chat_handles(conn)
    stats: dict[int, Stats] = {
        handle_id: Stats(handle=label) for handle_id, label in labels.items()
    }

    direct_rows = conn.execute(
        """
        SELECT
          cmj.chat_id,
          m.handle_id,
          m.is_from_me,
          m.date
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE m.date >= ?
          AND m.date IS NOT NULL
          AND cmj.chat_id IN (
            SELECT chat_id
            FROM chat_handle_join
            GROUP BY chat_id
            HAVING COUNT(DISTINCT handle_id) = 1
          )
        """,
        (cutoff_ns,),
    )
    for row in direct_rows:
        chat_id = int(row["chat_id"])
        handle_id = direct_chats.get(chat_id)
        if handle_id is None or handle_id not in stats:
            continue
        item = stats[handle_id]
        item.direct_chats.add(chat_id)
        item.shared_direct_messages += 1
        date_ns = int(row["date"])
        item.last_message_ns = max(item.last_message_ns or date_ns, date_ns)
        if int(row["is_from_me"]) == 1:
            item.sent_direct += 1
            item.last_sent_ns = max(item.last_sent_ns or date_ns, date_ns)
        else:
            item.received_direct += 1

    group_rows = conn.execute(
        """
        SELECT
          cmj.chat_id,
          m.handle_id,
          m.is_from_me,
          m.date
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE m.date >= ?
          AND m.date IS NOT NULL
          AND cmj.chat_id IN (
            SELECT chat_id
            FROM chat_handle_join
            GROUP BY chat_id
            HAVING COUNT(DISTINCT handle_id) > 1
          )
        """,
        (cutoff_ns,),
    )
    for row in group_rows:
        chat_id = int(row["chat_id"])
        participants = group_chats.get(chat_id, [])
        if not participants:
            continue
        date_ns = int(row["date"])
        from_me = int(row["is_from_me"]) == 1
        sender_id = int(row["handle_id"] or 0)
        for participant_id in participants:
            if participant_id not in stats:
                continue
            item = stats[participant_id]
            item.group_chats.add(chat_id)
            item.shared_group_messages += 1
            item.last_message_ns = max(item.last_message_ns or date_ns, date_ns)
            if from_me:
                item.sent_group += 1
                item.last_sent_ns = max(item.last_sent_ns or date_ns, date_ns)
            elif sender_id == participant_id:
                item.received_from_them_group += 1

    return {handle_id: item for handle_id, item in stats.items() if item.sent_total > 0}


def write_csv(rows: list[Stats], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "name",
                "handle",
                "sent_total",
                "sent_direct",
                "sent_group",
                "received_from_them_total",
                "received_direct",
                "received_from_them_group",
                "shared_total",
                "shared_direct_messages",
                "shared_group_messages",
                "direct_chat_count",
                "group_chat_count",
                "last_sent",
                "last_message",
            ]
        )
        for item in rows:
            writer.writerow(
                [
                    item.name,
                    item.handle,
                    item.sent_total,
                    item.sent_direct,
                    item.sent_group,
                    item.received_from_them_total,
                    item.received_direct,
                    item.received_from_them_group,
                    item.shared_total,
                    item.shared_direct_messages,
                    item.shared_group_messages,
                    len(item.direct_chats or ()),
                    len(item.group_chats or ()),
                    format_dt(item.last_sent_ns),
                    format_dt(item.last_message_ns),
                ]
            )


def write_html(rows: list[Stats], output_path: Path, title: str, top: int) -> None:
    chart_rows = rows[:top]
    max_count = max((row.sent_total for row in chart_rows), default=1)
    bar_height = 24
    gap = 8
    left = 260
    right = 120
    width = 1100
    chart_height = max(120, len(chart_rows) * (bar_height + gap) + 40)
    svg_rows = []
    for idx, item in enumerate(chart_rows):
        y = 30 + idx * (bar_height + gap)
        bar_width = int((width - left - right) * (item.sent_total / max_count))
        label = html.escape(item.display_label)
        svg_rows.append(
            f'<text x="0" y="{y + 17}" class="label">{label}</text>'
            f'<rect x="{left}" y="{y}" width="{bar_width}" height="{bar_height}" rx="3" />'
            f'<text x="{left + bar_width + 8}" y="{y + 17}" class="count">{item.sent_total:,}</text>'
        )

    table_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(item.display_label)}</td>"
        f"<td>{html.escape(item.handle)}</td>"
        f"<td>{item.sent_total:,}</td>"
        f"<td>{item.sent_direct:,}</td>"
        f"<td>{item.sent_group:,}</td>"
        f"<td>{item.received_from_them_total:,}</td>"
        f"<td>{item.shared_total:,}</td>"
        f"<td>{len(item.direct_chats or ()):}</td>"
        f"<td>{len(item.group_chats or ()):}</td>"
        f"<td>{html.escape(format_dt(item.last_sent_ns))}</td>"
        "</tr>"
        for item in rows
    )

    output_path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{
      color: #1d1d1f;
      font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 32px;
      background: #fbfbfd;
    }}
    h1 {{ font-size: 24px; margin: 0 0 6px; }}
    p {{ color: #515154; margin: 0 0 20px; max-width: 860px; }}
    svg {{ width: 100%; max-width: {width}px; height: auto; background: white; border: 1px solid #e5e5ea; }}
    rect {{ fill: #0a84ff; }}
    .label {{ dominant-baseline: middle; font-size: 12px; fill: #1d1d1f; }}
    .count {{ dominant-baseline: middle; font-size: 12px; fill: #515154; }}
    table {{ border-collapse: collapse; margin-top: 28px; width: 100%; background: white; }}
    th, td {{ border-bottom: 1px solid #e5e5ea; padding: 8px 10px; text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    th {{ position: sticky; top: 0; background: #f5f5f7; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p>Primary histogram metric is messages you sent: one-to-one sent messages plus group messages you sent counted for each participant. Message body text was not read.</p>
  <svg viewBox="0 0 {width} {chart_height}" role="img" aria-label="{html.escape(title)}">
    {''.join(svg_rows)}
  </svg>
  <table>
    <thead>
      <tr>
        <th>Name</th>
        <th>Handle</th>
        <th>Sent total</th>
        <th>Sent direct</th>
        <th>Sent group</th>
        <th>Received from them</th>
        <th>Shared messages</th>
        <th>Direct chats</th>
        <th>Group chats</th>
        <th>Last sent</th>
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--top", type=int, default=75)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / "Library/Messages/chat.db",
        help="Path to Messages chat.db",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / "Desktop/messages_text_histogram",
    )
    parser.add_argument(
        "--contacts-db",
        action="append",
        type=Path,
        help="Optional Contacts AddressBook-v*.abcddb path. Repeat to pass more than one.",
    )
    parser.add_argument(
        "--no-contacts",
        action="store_true",
        help="Skip Contacts lookup and label rows with raw handles only.",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = years_ago(now, args.years)
    cutoff_ns = dt_to_apple_ns(cutoff)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with connect_messages_db(args.db) as conn:
            stats = collect_stats(conn, cutoff_ns)
    except sqlite3.OperationalError as exc:
        print(f"Could not open Messages database: {exc}")
        print("")
        print("macOS usually requires Full Disk Access for this file:")
        print(f"  {args.db}")
        print("")
        print("Grant Full Disk Access to the app running this script, then rerun it.")
        print("For Codex, grant access to Codex. For Terminal, grant access to Terminal.")
        return 1

    contact_warnings: list[str] = []
    contact_lookup: dict[str, str] = {}
    if not args.no_contacts:
        contact_db_paths = args.contacts_db or default_contacts_dbs()
        contact_lookup, contact_warnings = build_contact_lookup(contact_db_paths)
        apply_contact_names(stats, contact_lookup)

    rows = sorted(stats.values(), key=lambda item: (item.sent_total, item.last_sent_ns or 0), reverse=True)
    csv_path = args.output_dir / "messages_texted_counts.csv"
    html_path = args.output_dir / "messages_texted_histogram.html"
    write_csv(rows, csv_path)
    write_html(
        rows,
        html_path,
        f"Messages Sent by Correspondent, Past {args.years} Years",
        args.top,
    )

    print(f"Wrote {csv_path}")
    print(f"Wrote {html_path}")
    print(f"Correspondents with at least one sent message: {len(rows):,}")
    if not args.no_contacts:
        named_count = sum(1 for item in rows if item.name)
        print(f"Matched to Contacts names: {named_count:,}")
        for warning in contact_warnings:
            print(f"Warning: {warning}")
    if rows:
        print("Top correspondents by sent-message count:")
        for item in rows[:10]:
            print(f"  {item.sent_total:>7,}  {item.display_label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
