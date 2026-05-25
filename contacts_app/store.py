from __future__ import annotations

import hashlib
import json
import math
import plistlib
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import messages_histogram as messages

from .models import Contact, ContactMetrics, GROUP_MESSAGE_WEIGHT, KEEP_SCORE_HALF_LIFE_DAYS


_CACHE: dict[str, Any] = {
    "created_at": 0.0,
    "params": None,
    "payload": None,
}


def clear_cache() -> None:
    _CACHE.update({"created_at": 0.0, "params": None, "payload": None})


def connect_readonly_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def source_id_for_path(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]


def source_uuid_for_path(path: Path) -> str:
    parent = path.parent.name
    if len(parent) == 36 and parent.count("-") == 4:
        return parent
    return "local"


def account_key_for_name(name: str) -> str:
    normalized = name.strip().casefold()
    if "icloud" in normalized:
        return "icloud"
    if "gmail" in normalized or "google" in normalized:
        return "gmail"
    return "other"


def row_text(row: sqlite3.Row, key: str) -> str:
    return str(row[key] or "").strip()


def contact_name(row: sqlite3.Row) -> str:
    name_parts = [row_text(row, "ZFIRSTNAME"), row_text(row, "ZMIDDLENAME"), row_text(row, "ZLASTNAME")]
    full_name = " ".join(part for part in name_parts if part)
    for candidate in (
        full_name,
        row_text(row, "ZNAME"),
        row_text(row, "ZNICKNAME"),
        row_text(row, "ZORGANIZATION"),
    ):
        if candidate:
            return candidate
    return "Untitled contact"


def is_placeholder_contact(contact: Contact) -> bool:
    return contact.name == "Untitled contact" and not contact.handles


LIST_CONTAINERS_SWIFT = r"""
import Contacts
import Foundation

struct ContainerInfo: Encodable {
    let identifier: String
    let sourceUUID: String
    let name: String
    let type: Int
}

let store = CNContactStore()
var granted = false
var accessError: Error?
let semaphore = DispatchSemaphore(value: 0)
store.requestAccess(for: .contacts) { ok, error in
    granted = ok
    accessError = error
    semaphore.signal()
}
semaphore.wait()

if !granted {
    let message = accessError?.localizedDescription ?? "Contacts access was not granted."
    FileHandle.standardError.write(Data(message.utf8))
    exit(2)
}

let containers = try store.containers(matching: nil).map { container in
    let sourceUUID = container.identifier.components(separatedBy: ":").first ?? container.identifier
    return ContainerInfo(
        identifier: container.identifier,
        sourceUUID: sourceUUID,
        name: container.name,
        type: container.type.rawValue
    )
}

let output = try JSONEncoder().encode(containers)
FileHandle.standardOutput.write(output)
"""


def account_name_from_configuration(source_dir: Path) -> str:
    config_path = source_dir / "Configuration.plist"
    if not config_path.exists():
        return ""
    try:
        with config_path.open("rb") as f:
            data = plistlib.load(f)
        name = str(data.get("name") or "").strip()
        plugin = str(data.get("aListPluginIdentifier") or "").casefold()
        server = str(data.get("serverName") or data.get("servername") or "").casefold()
        if name:
            return name
        if "icloud" in plugin or "icloud" in server:
            return "iCloud"
        if "google" in plugin or "google" in server:
            return "Gmail"
    except Exception:
        return ""
    return ""


def contact_account_map(db_paths: list[Path]) -> tuple[dict[str, dict[str, str]], list[str]]:
    accounts: dict[str, dict[str, str]] = {}
    warnings: list[str] = []

    for db_path in db_paths:
        source_uuid = source_uuid_for_path(db_path)
        config_name = account_name_from_configuration(db_path.parent)
        if config_name:
            accounts[source_uuid] = {"name": config_name, "key": account_key_for_name(config_name)}

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "list_containers.swift"
            script_path.write_text(LIST_CONTAINERS_SWIFT, encoding="utf-8")
            proc = subprocess.run(
                ["/usr/bin/swift", str(script_path)],
                text=True,
                capture_output=True,
                timeout=30,
            )
        if proc.returncode == 0 and proc.stdout.strip():
            for item in json.loads(proc.stdout):
                name = str(item.get("name") or "").strip() or "Other"
                source_uuid = str(item.get("sourceUUID") or "").strip()
                if source_uuid:
                    accounts[source_uuid] = {"name": name, "key": account_key_for_name(name)}
        elif proc.stderr.strip():
            warnings.append(f"Could not read Contacts account containers: {proc.stderr.strip()}")
    except Exception as exc:
        warnings.append(f"Could not read Contacts account containers: {exc}")

    return accounts, warnings


def account_for_source(source_uuid: str, account_map: dict[str, dict[str, str]]) -> dict[str, str]:
    account = account_map.get(source_uuid)
    if account:
        return account
    return {"name": "Other", "key": "other"}


def contact_entity_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT Z_ENT FROM Z_PRIMARYKEY WHERE Z_NAME = 'ABCDContact'").fetchone()
    return int(row["Z_ENT"]) if row else None


def read_contacts(db_paths: list[Path], account_map: dict[str, dict[str, str]]) -> tuple[list[Contact], list[str]]:
    contacts: dict[str, Contact] = {}
    warnings: list[str] = []

    for db_path in db_paths:
        source_id = source_id_for_path(db_path)
        source_uuid = source_uuid_for_path(db_path)
        account = account_for_source(source_uuid, account_map)
        try:
            with connect_readonly_db(db_path) as conn:
                entity_id = contact_entity_id(conn)
                if entity_id is None:
                    continue
                for row in conn.execute(
                    """
                    SELECT
                      Z_PK,
                      ZFIRSTNAME,
                      ZMIDDLENAME,
                      ZLASTNAME,
                      ZNAME,
                      ZNICKNAME,
                      ZORGANIZATION,
                      ZUNIQUEID
                    FROM ZABCDRECORD
                    WHERE Z_ENT = ?
                    """,
                    (entity_id,),
                ):
                    record_id = int(row["Z_PK"])
                    contact = Contact(
                        source_id=source_id,
                        source_uuid=source_uuid,
                        source_path=str(db_path),
                        account_name=account["name"],
                        account_key=account["key"],
                        record_id=record_id,
                        contact_identifier=row_text(row, "ZUNIQUEID"),
                        name=contact_name(row),
                        first_name=row_text(row, "ZFIRSTNAME"),
                        last_name=row_text(row, "ZLASTNAME"),
                        organization=row_text(row, "ZORGANIZATION"),
                        nickname=row_text(row, "ZNICKNAME"),
                    )
                    contacts[contact.id] = contact

                for row in conn.execute(
                    """
                    SELECT ZOWNER, ZFULLNUMBER, ZLOCALNUMBER
                    FROM ZABCDPHONENUMBER
                    WHERE ZOWNER IS NOT NULL
                    """
                ):
                    contact = contacts.get(f"{source_id}:{int(row['ZOWNER'])}")
                    if not contact:
                        continue
                    for value in (row["ZFULLNUMBER"], row["ZLOCALNUMBER"]):
                        cleaned = str(value or "").strip()
                        if cleaned and cleaned not in contact.phones:
                            contact.phones.append(cleaned)

                for row in conn.execute(
                    """
                    SELECT ZOWNER, ZADDRESS, ZADDRESSNORMALIZED
                    FROM ZABCDEMAILADDRESS
                    WHERE ZOWNER IS NOT NULL
                    """
                ):
                    contact = contacts.get(f"{source_id}:{int(row['ZOWNER'])}")
                    if not contact:
                        continue
                    for value in (row["ZADDRESS"], row["ZADDRESSNORMALIZED"]):
                        email = messages.normalize_email(str(value or ""))
                        if email and email not in contact.emails:
                            contact.emails.append(email)
        except sqlite3.Error as exc:
            warnings.append(f"Could not read Contacts database {db_path}: {exc}")

    return sorted(contacts.values(), key=lambda item: item.name.casefold()), warnings


def build_message_stats(messages_db: Path, years: int | None) -> tuple[dict[str, messages.Stats], list[str]]:
    warnings: list[str] = []
    if years is None:
        cutoff_ns = 0
    else:
        cutoff = messages.years_ago(datetime.now(timezone.utc), years)
        cutoff_ns = messages.dt_to_apple_ns(cutoff)

    try:
        with messages.connect_messages_db(messages_db) as conn:
            raw_stats = messages.collect_stats(conn, cutoff_ns)
    except sqlite3.OperationalError as exc:
        warnings.append(
            "Could not read Messages database. Grant Full Disk Access to the app "
            f"running this server, then refresh. Details: {exc}"
        )
        return {}, warnings

    by_key: dict[str, messages.Stats] = {}
    for item in raw_stats.values():
        for key in messages.handle_match_keys(item.handle):
            by_key[key] = item
    return by_key, warnings


def combine_metrics(contact: Contact, message_stats_by_key: dict[str, messages.Stats]) -> ContactMetrics:
    metrics = ContactMetrics()
    seen_message_handles: set[str] = set()

    for handle in contact.handles:
        for key in messages.handle_match_keys(handle):
            stat = message_stats_by_key.get(key)
            if not stat or stat.handle in seen_message_handles:
                continue
            seen_message_handles.add(stat.handle)
            metrics.matched_handles.add(stat.handle)
            metrics.sent_direct += stat.sent_direct
            metrics.sent_group += stat.sent_group
            metrics.received_from_them_total += stat.received_from_them_total
            metrics.shared_total += stat.shared_total
            metrics.direct_chat_count += len(stat.direct_chats or ())
            metrics.group_chat_count += len(stat.group_chats or ())
            if stat.last_sent_ns is not None:
                metrics.last_sent_ns = max(metrics.last_sent_ns or stat.last_sent_ns, stat.last_sent_ns)
            if stat.last_message_ns is not None:
                metrics.last_message_ns = max(metrics.last_message_ns or stat.last_message_ns, stat.last_message_ns)

    metrics.sent_total = metrics.sent_direct + metrics.sent_group
    return metrics


def days_since_apple_ns(value: int | None) -> float | None:
    dt = messages.apple_ns_to_dt(value)
    if dt is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86_400)


def keep_score(metrics: ContactMetrics) -> float:
    if metrics.sent_total <= 0 or metrics.last_sent_ns is None:
        return 0.0
    weighted_frequency = metrics.sent_direct + (metrics.sent_group * GROUP_MESSAGE_WEIGHT)
    days_since = days_since_apple_ns(metrics.last_sent_ns) or 0.0
    recency_weight = 0.5 ** (days_since / KEEP_SCORE_HALF_LIFE_DAYS)
    return math.log1p(weighted_frequency) * recency_weight


def contact_to_payload(contact: Contact, metrics: ContactMetrics, messages_available: bool) -> dict[str, Any]:
    score = keep_score(metrics)
    days_since_sent = days_since_apple_ns(metrics.last_sent_ns)
    return {
        "id": contact.id,
        "contactIdentifier": contact.contact_identifier,
        "name": contact.name,
        "accountName": contact.account_name,
        "accountKey": contact.account_key,
        "sourceUUID": contact.source_uuid,
        "firstName": contact.first_name,
        "lastName": contact.last_name,
        "organization": contact.organization,
        "nickname": contact.nickname,
        "phones": contact.phones,
        "emails": contact.emails,
        "hasPhone": bool(contact.phones),
        "hasEmail": bool(contact.emails),
        "handleCount": len(contact.handles),
        "matchedHandles": sorted(metrics.matched_handles),
        "sentTotal": metrics.sent_total,
        "sentDirect": metrics.sent_direct,
        "sentGroup": metrics.sent_group,
        "receivedFromThemTotal": metrics.received_from_them_total,
        "sharedTotal": metrics.shared_total,
        "directChatCount": metrics.direct_chat_count,
        "groupChatCount": metrics.group_chat_count,
        "lastSent": messages.format_dt(metrics.last_sent_ns),
        "lastMessage": messages.format_dt(metrics.last_message_ns),
        "daysSinceSent": round(days_since_sent, 1) if days_since_sent is not None else None,
        "keepScore": round(score, 4),
        "removalRank": round(1 / (1 + score), 4),
        "neverTexted": messages_available and metrics.sent_total == 0,
        "messagesAvailable": messages_available,
        "hasTextHandle": bool(contact.handles),
        "isPlaceholder": is_placeholder_contact(contact),
        "sourcePath": contact.source_path,
    }


def account_options(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options = [{"key": "all", "name": "All Contacts", "count": len(rows)}]
    preferred = {"icloud": "iCloud", "gmail": "Gmail", "other": "Other"}
    for key in ("icloud", "gmail", "other"):
        count = sum(1 for row in rows if row["accountKey"] == key)
        if count:
            names = sorted({row["accountName"] for row in rows if row["accountKey"] == key})
            options.append({"key": key, "name": preferred.get(key) or " / ".join(names), "count": count})
    return options


def build_payload(contacts_db_paths: list[Path], messages_db: Path, years: int | None) -> dict[str, Any]:
    account_map, account_warnings = contact_account_map(contacts_db_paths)
    contacts, contact_warnings = read_contacts(contacts_db_paths, account_map)
    message_stats_by_key, message_warnings = build_message_stats(messages_db, years)
    messages_available = not message_warnings
    rows = [
        contact_to_payload(contact, combine_metrics(contact, message_stats_by_key), messages_available)
        for contact in contacts
    ]
    rows.sort(
        key=lambda row: (
            0 if messages_available and row["neverTexted"] else 1,
            row["keepScore"],
            row["sentTotal"],
            row["name"].casefold(),
        )
    )

    never_texted = sum(1 for row in rows if row["neverTexted"])
    no_text_handle = sum(1 for row in rows if not row["hasTextHandle"])
    no_phone = sum(1 for row in rows if not row["hasPhone"])
    no_email = sum(1 for row in rows if not row["hasEmail"])
    placeholders = sum(1 for row in rows if row["isPlaceholder"])
    matched = sum(1 for row in rows if row["matchedHandles"])

    return {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "years": years,
        "messagesAvailable": messages_available,
        "score": {
            "groupMessageWeight": GROUP_MESSAGE_WEIGHT,
            "halfLifeDays": KEEP_SCORE_HALF_LIFE_DAYS,
            "meaning": "Lower keepScore means a stronger removal candidate. Zero means no outgoing texts matched.",
        },
        "summary": {
            "contacts": len(rows),
            "neverTexted": never_texted if messages_available else None,
            "textedAtLeastOnce": len(rows) - never_texted if messages_available else None,
            "withoutPhoneOrEmail": no_text_handle,
            "withoutPhone": no_phone,
            "withoutEmail": no_email,
            "placeholders": placeholders,
            "matchedToMessages": matched,
        },
        "accountOptions": account_options(rows),
        "warnings": [*account_warnings, *contact_warnings, *message_warnings],
        "contacts": rows,
    }


def cached_payload(contacts_db_paths: list[Path], messages_db: Path, years: int | None, refresh: bool) -> dict[str, Any]:
    params = (tuple(str(path) for path in contacts_db_paths), str(messages_db), years)
    if not refresh and _CACHE["params"] == params and _CACHE["payload"] and time.time() - _CACHE["created_at"] < 300:
        return _CACHE["payload"]

    payload = build_payload(contacts_db_paths, messages_db, years)
    _CACHE.update({"created_at": time.time(), "params": params, "payload": payload})
    return payload
