#!/usr/bin/env python3
"""
Local Contacts cleanup app powered by Messages metadata.

The app is read-only: it reads Contacts and Messages SQLite metadata, then serves
a local review UI. It does not read message body text and does not edit Contacts.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import messages_histogram as messages


KEEP_SCORE_HALF_LIFE_DAYS = 730
GROUP_MESSAGE_WEIGHT = 0.25


@dataclass
class Contact:
    source_id: str
    source_uuid: str
    source_path: str
    account_name: str
    account_key: str
    record_id: int
    contact_identifier: str
    name: str
    first_name: str = ""
    last_name: str = ""
    organization: str = ""
    nickname: str = ""
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.source_id}:{self.record_id}"

    @property
    def handles(self) -> list[str]:
        return list(dict.fromkeys([*self.phones, *self.emails]))


@dataclass
class ContactMetrics:
    sent_total: int = 0
    sent_direct: int = 0
    sent_group: int = 0
    received_from_them_total: int = 0
    shared_total: int = 0
    direct_chat_count: int = 0
    group_chat_count: int = 0
    last_sent_ns: int | None = None
    last_message_ns: int | None = None
    matched_handles: set[str] = field(default_factory=set)


_CACHE: dict[str, Any] = {
    "created_at": 0.0,
    "params": None,
    "payload": None,
}


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


DELETE_CONTACTS_SWIFT = r"""
import Contacts
import Foundation

struct DeleteRequest: Decodable {
    let identifiers: [String]
}

struct DeleteResponse: Encodable {
    let requested: Int
    let deleted: Int
    let missing: [String]
    let stillPresent: [String]
    let errors: [String]
}

func emit(_ response: DeleteResponse) {
    let data = try! JSONEncoder().encode(response)
    FileHandle.standardOutput.write(data)
}

let input = Data((CommandLine.arguments.dropFirst().first ?? "{}").utf8)
let request = try JSONDecoder().decode(DeleteRequest.self, from: input)
let identifiers = Array(Set(request.identifiers.filter { !$0.isEmpty }))
let store = CNContactStore()

var accessGranted = false
var accessError: Error?
let semaphore = DispatchSemaphore(value: 0)
store.requestAccess(for: .contacts) { granted, error in
    accessGranted = granted
    accessError = error
    semaphore.signal()
}
semaphore.wait()

if !accessGranted {
    emit(DeleteResponse(
        requested: identifiers.count,
        deleted: 0,
        missing: [],
        stillPresent: [],
        errors: [accessError?.localizedDescription ?? "Contacts access was not granted."]
    ))
    exit(2)
}

let keys = [
    CNContactIdentifierKey,
    CNContactTypeKey,
    CNContactNamePrefixKey,
    CNContactGivenNameKey,
    CNContactMiddleNameKey,
    CNContactFamilyNameKey,
    CNContactPreviousFamilyNameKey,
    CNContactNameSuffixKey,
    CNContactNicknameKey,
    CNContactOrganizationNameKey,
    CNContactDepartmentNameKey,
    CNContactJobTitleKey,
    CNContactPhoneticGivenNameKey,
    CNContactPhoneticMiddleNameKey,
    CNContactPhoneticFamilyNameKey,
    CNContactPhoneticOrganizationNameKey,
    CNContactBirthdayKey,
    CNContactNonGregorianBirthdayKey,
    CNContactNoteKey,
    CNContactImageDataKey,
    CNContactThumbnailImageDataKey,
    CNContactImageDataAvailableKey,
    CNContactPhoneNumbersKey,
    CNContactEmailAddressesKey,
    CNContactPostalAddressesKey,
    CNContactDatesKey,
    CNContactUrlAddressesKey,
    CNContactRelationsKey,
    CNContactSocialProfilesKey,
    CNContactInstantMessageAddressesKey
] as [CNKeyDescriptor]
let saveRequest = CNSaveRequest()
var prepared = Set<String>()
var missing: [String] = []
var errors: [String] = []
let requested = Set(identifiers)

let fetchRequest = CNContactFetchRequest(keysToFetch: keys)
fetchRequest.unifyResults = false

do {
    try store.enumerateContacts(with: fetchRequest) { contact, _ in
        guard requested.contains(contact.identifier) else {
            return
        }
        guard let mutableContact = contact.mutableCopy() as? CNMutableContact else {
            errors.append("Could not prepare contact \(contact.identifier) for deletion.")
            return
        }
        do {
            saveRequest.delete(mutableContact)
            prepared.insert(contact.identifier)
        } catch {
            errors.append("Could not stage contact \(contact.identifier): \(error.localizedDescription)")
        }
    }
} catch {
    emit(DeleteResponse(
        requested: identifiers.count,
        deleted: 0,
        missing: identifiers,
        stillPresent: [],
        errors: [error.localizedDescription]
    ))
    exit(1)
}

missing = identifiers.filter { !prepared.contains($0) }

do {
    if !prepared.isEmpty {
        try store.execute(saveRequest)
    }

    let verifyStore = CNContactStore()
    let verifyRequest = CNContactFetchRequest(keysToFetch: keys)
    verifyRequest.unifyResults = false
    var stillPresentSet = Set<String>()
    do {
        try verifyStore.enumerateContacts(with: verifyRequest) { contact, _ in
            if prepared.contains(contact.identifier) {
                stillPresentSet.insert(contact.identifier)
            }
        }
    } catch {
        errors.append("Could not verify deletion: \(error.localizedDescription)")
    }

    let stillPresent = identifiers.filter { stillPresentSet.contains($0) }
    emit(DeleteResponse(
        requested: identifiers.count,
        deleted: prepared.count - stillPresent.count,
        missing: missing,
        stillPresent: stillPresent,
        errors: errors
    ))
} catch {
    emit(DeleteResponse(
        requested: identifiers.count,
        deleted: 0,
        missing: missing,
        stillPresent: Array(prepared),
        errors: [error.localizedDescription]
    ))
    exit(1)
}
"""


def delete_contacts_with_framework(identifiers: list[str]) -> dict[str, Any]:
    clean_identifiers = sorted({identifier for identifier in identifiers if identifier})
    if not clean_identifiers:
        return {"requested": 0, "deleted": 0, "missing": [], "stillPresent": [], "errors": ["No deletable Contacts identifiers were provided."]}

    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = Path(tmpdir) / "delete_contacts.swift"
        script_path.write_text(DELETE_CONTACTS_SWIFT, encoding="utf-8")
        proc = subprocess.run(
            ["/usr/bin/swift", str(script_path), json.dumps({"identifiers": clean_identifiers})],
            text=True,
            capture_output=True,
            timeout=60,
        )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    try:
        result = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        result = {}
    if proc.returncode != 0 and stderr:
        result.setdefault("errors", []).append(stderr)
    if not result:
        result = {"requested": len(clean_identifiers), "deleted": 0, "missing": [], "stillPresent": [], "errors": ["Contacts deletion did not return a result."]}
    _CACHE.update({"created_at": 0.0, "params": None, "payload": None})
    return result


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def applescript_list(values: list[str]) -> str:
    return "{" + ", ".join(applescript_string(value) for value in values) + "}"


def delete_contacts_with_contacts_app(identifiers: list[str]) -> dict[str, Any]:
    clean_identifiers = sorted({identifier for identifier in identifiers if identifier})
    if not clean_identifiers:
        return {"requested": 0, "deleted": 0, "missing": [], "stillPresent": [], "errors": ["No deletable Contacts identifiers were provided."]}

    id_list = "{" + ", ".join(applescript_string(identifier) for identifier in clean_identifiers) + "}"
    script = f"""
set targetIds to {id_list}
set reportLines to {{}}
tell application "Contacts"
    repeat with targetId in targetIds
        set targetIdText to targetId as text
        set matches to people whose id is targetIdText
        if (count of matches) is 0 then
            set end of reportLines to "MISSING\t" & targetIdText
        else
            repeat with matchedPerson in matches
                delete matchedPerson
            end repeat
            set end of reportLines to "STAGED\t" & targetIdText
        end if
    end repeat
    save
    repeat with targetId in targetIds
        set targetIdText to targetId as text
        set matches to people whose id is targetIdText
        if (count of matches) is 0 then
            set end of reportLines to "GONE\t" & targetIdText
        else
            set end of reportLines to "PRESENT\t" & targetIdText
        end if
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return reportLines as text
"""
    proc = subprocess.run(
        ["osascript"],
        input=script,
        text=True,
        capture_output=True,
        timeout=60,
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()

    staged: set[str] = set()
    missing: set[str] = set()
    gone: set[str] = set()
    still_present: set[str] = set()
    for line in stdout.splitlines():
        if "\t" not in line:
            continue
        status, identifier = line.split("\t", 1)
        if status == "STAGED":
            staged.add(identifier)
        elif status == "MISSING":
            missing.add(identifier)
        elif status == "GONE":
            gone.add(identifier)
        elif status == "PRESENT":
            still_present.add(identifier)

    errors = [stderr] if proc.returncode != 0 and stderr else []
    deleted = len(staged & gone)
    result = {
        "requested": len(clean_identifiers),
        "deleted": deleted,
        "missing": sorted(missing),
        "stillPresent": sorted(still_present & staged),
        "errors": errors,
        "method": "contacts_app",
    }
    _CACHE.update({"created_at": 0.0, "params": None, "payload": None})
    return result


def merge_contacts_with_contacts_app(payload: dict[str, Any]) -> dict[str, Any]:
    primary_identifier = str(payload.get("primaryIdentifier") or "").strip()
    delete_identifiers = sorted({str(value).strip() for value in payload.get("deleteIdentifiers", []) if str(value).strip()})
    account_key = str(payload.get("accountKey") or "").strip()
    fields = payload.get("fields") or {}
    phones = sorted({str(value).strip() for value in payload.get("phones", []) if str(value).strip()})
    emails = sorted({str(value).strip() for value in payload.get("emails", []) if str(value).strip()})

    if not primary_identifier:
        return {"updated": False, "deleted": 0, "missing": [], "stillPresent": [], "errors": ["No primary contact was provided."]}
    if not account_key:
        return {"updated": False, "deleted": 0, "missing": [], "stillPresent": [], "errors": ["Merge requires selected contacts to come from the same account."]}
    delete_identifiers = [identifier for identifier in delete_identifiers if identifier != primary_identifier]
    if not delete_identifiers:
        return {"updated": False, "deleted": 0, "missing": [], "stillPresent": [], "errors": ["Choose at least one other contact to merge into the primary contact."]}

    set_lines = []
    field_map = {
        "firstName": "first name",
        "lastName": "last name",
        "organization": "organization",
        "nickname": "nickname",
    }
    for key, applescript_property in field_map.items():
        if key in fields:
            set_lines.append(f"        set {applescript_property} of primaryPerson to {applescript_string(str(fields.get(key) or ''))}")

    script = f"""
set primaryId to {applescript_string(primary_identifier)}
set deleteIds to {applescript_list(delete_identifiers)}
set phoneValues to {applescript_list(phones)}
set emailValues to {applescript_list(emails)}
set reportLines to {{}}
tell application "Contacts"
    set primaryMatches to people whose id is primaryId
    if (count of primaryMatches) is 0 then error "Primary contact was not found."
    set primaryPerson to item 1 of primaryMatches
{chr(10).join(set_lines) if set_lines else '        -- no scalar fields selected'}
    set existingPhones to {{}}
    repeat with ph in phones of primaryPerson
        set end of existingPhones to value of ph as text
    end repeat
    repeat with phoneValue in phoneValues
        set phoneText to phoneValue as text
        if existingPhones does not contain phoneText then
            make new phone at end of phones of primaryPerson with properties {{label:"Phone", value:phoneText}}
        end if
    end repeat
    set existingEmails to {{}}
    repeat with em in emails of primaryPerson
        set end of existingEmails to value of em as text
    end repeat
    repeat with emailValue in emailValues
        set emailText to emailValue as text
        if existingEmails does not contain emailText then
            make new email at end of emails of primaryPerson with properties {{label:"Email", value:emailText}}
        end if
    end repeat
    save
    set end of reportLines to "UPDATED\t" & primaryId
    repeat with deleteId in deleteIds
        set deleteIdText to deleteId as text
        set matches to people whose id is deleteIdText
        if (count of matches) is 0 then
            set end of reportLines to "MISSING\t" & deleteIdText
        else
            repeat with matchedPerson in matches
                delete matchedPerson
            end repeat
            set end of reportLines to "STAGED\t" & deleteIdText
        end if
    end repeat
    save
    repeat with deleteId in deleteIds
        set deleteIdText to deleteId as text
        set matches to people whose id is deleteIdText
        if (count of matches) is 0 then
            set end of reportLines to "GONE\t" & deleteIdText
        else
            set end of reportLines to "PRESENT\t" & deleteIdText
        end if
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return reportLines as text
"""
    proc = subprocess.run(
        ["osascript"],
        input=script,
        text=True,
        capture_output=True,
        timeout=60,
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()

    updated = False
    staged: set[str] = set()
    missing: set[str] = set()
    gone: set[str] = set()
    still_present: set[str] = set()
    for line in stdout.splitlines():
        if "\t" not in line:
            continue
        status, identifier = line.split("\t", 1)
        if status == "UPDATED":
            updated = True
        elif status == "STAGED":
            staged.add(identifier)
        elif status == "MISSING":
            missing.add(identifier)
        elif status == "GONE":
            gone.add(identifier)
        elif status == "PRESENT":
            still_present.add(identifier)

    errors = [stderr] if proc.returncode != 0 and stderr else []
    result = {
        "updated": updated,
        "deleted": len(staged & gone),
        "missing": sorted(missing),
        "stillPresent": sorted(still_present & staged),
        "errors": errors,
        "method": "contacts_app",
    }
    _CACHE.update({"created_at": 0.0, "params": None, "payload": None})
    return result


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Contacts Cleanup</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #202124;
      --muted: #64665f;
      --line: #deded8;
      --accent: #0b6bcb;
      --danger: #b42318;
      --ok: #287947;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      padding: 18px 24px 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfbf8;
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1 {
      margin: 0 0 12px;
      font-size: 22px;
      font-weight: 650;
      letter-spacing: 0;
    }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(4, minmax(130px, max-content)) max-content;
      gap: 10px;
      align-items: center;
    }
    input, select, button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: white;
      color: var(--text);
      padding: 0 10px;
      font: inherit;
    }
    button {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
      cursor: pointer;
    }
    main { padding: 18px 24px 28px; }
    .summary {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      min-width: 0;
    }
    .metric strong {
      display: block;
      font-size: 22px;
      line-height: 1.1;
      margin-bottom: 4px;
    }
    .metric span { color: var(--muted); font-size: 12px; }
    .warnings {
      display: none;
      border: 1px solid #f2b8b5;
      background: #fff4f2;
      color: var(--danger);
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 14px;
    }
    .selectionbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border: 1px solid var(--line);
      background: white;
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 14px;
    }
    .selectionbar button {
      background: var(--danger);
      border-color: var(--danger);
    }
    .selectionbar .secondary {
      background: white;
      border-color: var(--line);
      color: var(--text);
    }
    .selectionbar .merge {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }
    .selectionbar button:disabled {
      background: #deded8;
      border-color: #deded8;
      color: #7a7c75;
      cursor: default;
    }
    .table-wrap {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      overflow: auto;
      max-height: calc(100vh - 312px);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1120px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 8px 10px;
      text-align: right;
      vertical-align: top;
      white-space: nowrap;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      background: #efefea;
      color: #3c3d39;
      font-size: 12px;
      font-weight: 650;
      cursor: pointer;
      user-select: none;
    }
    th:first-child, td:first-child,
    th:nth-child(2), td:nth-child(2),
    th:nth-child(3), td:nth-child(3),
    th:nth-child(5), td:nth-child(5) {
      text-align: left;
    }
    tbody tr[data-id] {
      cursor: pointer;
      user-select: none;
      -webkit-user-select: none;
    }
    tbody tr:hover { background: #f8fbff; }
    tbody tr.selected { background: #e9f2ff; }
    .select-col {
      width: 42px;
      text-align: center;
    }
    .select-mark {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border: 1px solid var(--line);
      border-radius: 5px;
      color: transparent;
      font-size: 13px;
      font-weight: 700;
    }
    tr.selected .select-mark {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }
    .name {
      font-weight: 600;
      max-width: 240px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .handles {
      color: var(--muted);
      max-width: 300px;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      height: 22px;
      border-radius: 999px;
      padding: 0 8px;
      font-size: 12px;
      font-weight: 600;
      background: #eeeeea;
      color: #4b4d47;
    }
    .badge.never { background: #ffe9e6; color: var(--danger); }
    .badge.texted { background: #e6f4ea; color: var(--ok); }
    .muted { color: var(--muted); }
    .loading { color: var(--muted); padding: 18px; }
    .modal {
      position: fixed;
      inset: 0;
      z-index: 30;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(32, 33, 36, 0.36);
      padding: 24px;
    }
    .modal[hidden] { display: none; }
    .modal-panel {
      width: min(900px, 100%);
      max-height: min(760px, calc(100vh - 48px));
      overflow: auto;
      background: white;
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.24);
    }
    .modal-head, .modal-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    .modal-actions {
      border-top: 1px solid var(--line);
      border-bottom: 0;
      justify-content: flex-end;
    }
    .modal-head h2 {
      margin: 0;
      font-size: 18px;
      letter-spacing: 0;
    }
    .modal-body {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      padding: 16px;
    }
    .modal-section h3 {
      margin: 0 0 8px;
      font-size: 13px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .option-list {
      display: grid;
      gap: 8px;
    }
    .option-row {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
    }
    .option-row input {
      height: auto;
      margin-top: 3px;
    }
    .field-row {
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 8px;
      align-items: center;
      margin-bottom: 8px;
    }
    .field-row select {
      width: 100%;
    }
    .modal-actions .secondary {
      background: white;
      border-color: var(--line);
      color: var(--text);
    }
    @media (max-width: 900px) {
      header { position: static; }
      .toolbar { grid-template-columns: 1fr 1fr; }
      .toolbar input { grid-column: 1 / -1; }
      .summary { grid-template-columns: 1fr 1fr; }
      .table-wrap { max-height: none; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Contacts Cleanup</h1>
    <div class="toolbar">
      <input id="search" type="search" placeholder="Search names, numbers, emails">
      <select id="account">
        <option value="all">All Contacts</option>
      </select>
      <select id="view">
        <option value="all">All contacts</option>
        <option value="never">Never texted</option>
        <option value="texted">Texted before</option>
        <option value="nophone">No phone number</option>
        <option value="nohandles">No phone/email</option>
        <option value="placeholders">Empty placeholders</option>
      </select>
      <select id="years">
        <option value="10">10 years</option>
        <option value="5">5 years</option>
        <option value="2">2 years</option>
        <option value="1">1 year</option>
        <option value="all">All local Messages</option>
      </select>
      <select id="sort">
        <option value="candidate">Removal candidates</option>
        <option value="name">Name</option>
        <option value="lastSent">Oldest last text</option>
        <option value="sentTotal">Fewest texts</option>
      </select>
      <button id="refresh" type="button">Refresh</button>
    </div>
  </header>
  <main>
    <section class="summary" id="summary"></section>
    <section class="warnings" id="warnings"></section>
    <section class="selectionbar">
      <span id="selectionCount">0 selected</span>
      <div>
        <button id="clearSelection" class="secondary" type="button" disabled>Clear Selection</button>
        <button id="mergeSelected" class="merge" type="button" disabled>Merge Selected</button>
        <button id="deleteSelected" type="button" disabled>Delete Selected</button>
      </div>
    </section>
    <section class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="select-col"></th>
            <th data-sort="name">Name</th>
            <th>Account</th>
            <th>Reason</th>
            <th>Handles</th>
            <th data-sort="keepScore">Keep score</th>
            <th data-sort="sentTotal">Sent</th>
            <th>Direct</th>
            <th>Group</th>
            <th data-sort="daysSinceSent">Days since sent</th>
            <th>Last sent</th>
            <th>Received</th>
            <th>Shared</th>
          </tr>
        </thead>
        <tbody id="rows">
          <tr><td class="loading" colspan="13">Loading Contacts and Messages metadata...</td></tr>
        </tbody>
      </table>
    </section>
  </main>
  <section id="mergeModal" class="modal" hidden>
    <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="mergeTitle">
      <div class="modal-head">
        <h2 id="mergeTitle">Merge Contacts</h2>
        <button id="closeMerge" class="secondary" type="button">Close</button>
      </div>
      <div id="mergeBody" class="modal-body"></div>
      <div class="modal-actions">
        <button id="cancelMerge" class="secondary" type="button">Cancel</button>
        <button id="confirmMerge" type="button">Merge</button>
      </div>
    </div>
  </section>
  <script>
    const state = {
      contacts: [],
      payload: null,
      selectedIds: new Set(),
      visibleRows: [],
      lastSelectedIndex: null,
      sortKey: 'candidate',
      sortDirection: 1,
    };

    const el = {
      search: document.querySelector('#search'),
      account: document.querySelector('#account'),
      view: document.querySelector('#view'),
      years: document.querySelector('#years'),
      sort: document.querySelector('#sort'),
      refresh: document.querySelector('#refresh'),
      clearSelection: document.querySelector('#clearSelection'),
      mergeSelected: document.querySelector('#mergeSelected'),
      deleteSelected: document.querySelector('#deleteSelected'),
      selectionCount: document.querySelector('#selectionCount'),
      summary: document.querySelector('#summary'),
      warnings: document.querySelector('#warnings'),
      rows: document.querySelector('#rows'),
      mergeModal: document.querySelector('#mergeModal'),
      mergeBody: document.querySelector('#mergeBody'),
      closeMerge: document.querySelector('#closeMerge'),
      cancelMerge: document.querySelector('#cancelMerge'),
      confirmMerge: document.querySelector('#confirmMerge'),
    };

    function fmt(value) {
      return Number(value || 0).toLocaleString();
    }

    function metricValue(value) {
      return value == null ? 'Unknown' : fmt(value);
    }

    function reason(row) {
      if (!row.messagesAvailable) return '<span class="badge never">Messages unavailable</span>';
      if (!row.hasTextHandle) return '<span class="badge never">No phone/email</span>';
      if (!row.hasPhone) return '<span class="badge never">No phone</span>';
      if (row.neverTexted) return '<span class="badge never">Never texted</span>';
      return '<span class="badge texted">Low frecency</span>';
    }

    function handles(row) {
      return [...row.phones, ...row.emails].map(escapeHtml).join(', ') || '<span class="muted">None</span>';
    }

    function renderAccountOptions(options) {
      const current = el.account.value || 'all';
      el.account.innerHTML = (options || [{ key: 'all', name: 'All Contacts', count: 0 }]).map(option => `
        <option value="${escapeHtml(option.key)}">${escapeHtml(option.name)} (${fmt(option.count)})</option>
      `).join('');
      el.account.value = [...el.account.options].some(option => option.value === current) ? current : 'all';
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      }[char]));
    }

    function candidateCompare(a, b) {
      if (!a.messagesAvailable || !b.messagesAvailable) {
        return a.name.localeCompare(b.name);
      }
      return (
        Number(a.neverTexted) === Number(b.neverTexted)
          ? a.keepScore - b.keepScore || a.sentTotal - b.sentTotal || a.name.localeCompare(b.name)
          : Number(b.neverTexted) - Number(a.neverTexted)
      );
    }

    function sortedRows(rows) {
      const key = state.sortKey;
      const direction = state.sortDirection;
      return [...rows].sort((a, b) => {
        if (key === 'candidate') return candidateCompare(a, b);
        if (key === 'name') return direction * a.name.localeCompare(b.name);
        if (key === 'lastSent') return direction * ((a.daysSinceSent ?? 999999) - (b.daysSinceSent ?? 999999));
        const av = a[key] ?? 0;
        const bv = b[key] ?? 0;
        return direction * (av - bv);
      });
    }

    function filteredRows() {
      const query = el.search.value.trim().toLowerCase();
      const account = el.account.value;
      const view = el.view.value;
      return state.contacts.filter(row => {
        if (account !== 'all' && row.accountKey !== account) return false;
        if (!row.messagesAvailable && (view === 'never' || view === 'texted')) return false;
        if (view !== 'placeholders' && row.isPlaceholder) return false;
        if (view === 'never' && !row.neverTexted) return false;
        if (view === 'texted' && row.neverTexted) return false;
        if (view === 'nophone' && row.hasPhone) return false;
        if (view === 'nohandles' && row.hasTextHandle) return false;
        if (view === 'placeholders' && !row.isPlaceholder) return false;
        if (!query) return true;
        const haystack = [row.name, row.accountName, ...row.phones, ...row.emails, ...row.matchedHandles].join(' ').toLowerCase();
        return haystack.includes(query);
      });
    }

    function renderSummary(payload, visibleCount) {
      const summary = payload.summary || {};
      el.summary.innerHTML = [
        ['Contacts', summary.contacts],
        ['Visible', visibleCount],
        ['Never texted', summary.neverTexted],
        ['Texted before', summary.textedAtLeastOnce],
        ['No phone #', summary.withoutPhone],
        ['Hidden empty', summary.placeholders],
      ].map(([label, value]) => `
        <div class="metric">
          <strong>${metricValue(value)}</strong>
          <span>${label}</span>
        </div>
      `).join('');
    }

    function renderWarnings(warnings) {
      if (!warnings || warnings.length === 0) {
        el.warnings.style.display = 'none';
        el.warnings.innerHTML = '';
        return;
      }
      el.warnings.style.display = 'block';
      el.warnings.innerHTML = warnings.map(escapeHtml).join('<br>');
    }

    function renderRows(payload) {
      const rows = sortedRows(filteredRows());
      state.visibleRows = rows;
      renderSummary(payload, rows.length);
      if (!rows.length) {
        el.rows.innerHTML = '<tr><td class="loading" colspan="13">No contacts match the current filters.</td></tr>';
        updateSelectionState();
        return;
      }
      el.rows.innerHTML = rows.map((row, index) => `
        <tr data-id="${escapeHtml(row.id)}" data-index="${index}" class="${state.selectedIds.has(row.id) ? 'selected' : ''}">
          <td class="select-col"><span class="select-mark">✓</span></td>
          <td class="name" title="${escapeHtml(row.name)}">${escapeHtml(row.name)}</td>
          <td>${escapeHtml(row.accountName)}</td>
          <td>${reason(row)}</td>
          <td class="handles" title="${escapeHtml([...row.phones, ...row.emails].join(', '))}">${handles(row)}</td>
          <td>${row.messagesAvailable ? row.keepScore.toFixed(4) : '<span class="muted">Unknown</span>'}</td>
          <td>${fmt(row.sentTotal)}</td>
          <td>${fmt(row.sentDirect)}</td>
          <td>${fmt(row.sentGroup)}</td>
          <td>${row.daysSinceSent == null ? '<span class="muted">Never</span>' : fmt(Math.round(row.daysSinceSent))}</td>
          <td>${row.lastSent ? escapeHtml(row.lastSent) : '<span class="muted">Never</span>'}</td>
          <td>${fmt(row.receivedFromThemTotal)}</td>
          <td>${fmt(row.sharedTotal)}</td>
        </tr>
      `).join('');
      updateSelectionState();
    }

    function updateSelectionState() {
      const currentIds = new Set(state.contacts.map(row => row.id));
      for (const id of [...state.selectedIds]) {
        if (!currentIds.has(id)) state.selectedIds.delete(id);
      }
      const count = state.selectedIds.size;
      const accountKeys = selectedAccountKeys();
      el.selectionCount.textContent = `${fmt(count)} selected`;
      if (count >= 2 && accountKeys.length > 1) {
        el.selectionCount.textContent += ' · merge requires one account';
      }
      el.clearSelection.disabled = count === 0;
      el.mergeSelected.disabled = !canMergeSelection();
      el.deleteSelected.disabled = count === 0;
    }

    function selectedRows() {
      return state.contacts.filter(row => state.selectedIds.has(row.id));
    }

    function selectedAccountKeys() {
      return [...new Set(selectedRows().map(row => row.accountKey))];
    }

    function canMergeSelection() {
      const rows = selectedRows();
      return rows.length >= 2 && selectedAccountKeys().length === 1;
    }

    function selectRow(row, event) {
      const index = Number(row.dataset.index);
      const id = row.dataset.id;
      if (!id || Number.isNaN(index)) return;

      if (event.shiftKey && state.lastSelectedIndex != null) {
        const start = Math.min(state.lastSelectedIndex, index);
        const end = Math.max(state.lastSelectedIndex, index);
        for (let i = start; i <= end; i += 1) {
          const visible = state.visibleRows[i];
          if (visible) state.selectedIds.add(visible.id);
        }
      } else {
        if (state.selectedIds.has(id)) state.selectedIds.delete(id);
        else state.selectedIds.add(id);
        state.lastSelectedIndex = index;
      }
      renderRows(state.payload || { summary: window.lastSummary || {} });
    }

    async function deleteSelectedContacts() {
      const rows = selectedRows();
      const identifiers = rows.map(row => row.contactIdentifier).filter(Boolean);
      const skipped = rows.length - identifiers.length;
      if (!identifiers.length) {
        alert('None of the selected rows have a Contacts identifier that can be deleted.');
        return;
      }
      const warning = skipped
        ? `\n\n${skipped} selected row(s) do not have a deletable Contacts identifier and will be skipped.`
        : '';
      if (!confirm(`Delete ${identifiers.length} selected contact(s) from Contacts?${warning}\n\nThis changes your Contacts data.`)) {
        return;
      }
      el.deleteSelected.disabled = true;
      el.selectionCount.textContent = 'Deleting...';
      const response = await fetch('/api/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifiers }),
      });
      const result = await response.json();
      const errors = result.errors && result.errors.length ? `\n\nErrors:\n${result.errors.join('\n')}` : '';
      const missing = result.missing && result.missing.length ? `\n\nMissing/skipped: ${result.missing.length}` : '';
      const stillPresent = result.stillPresent && result.stillPresent.length ? `\n\nStill present after delete: ${result.stillPresent.length}` : '';
      alert(`Deleted ${result.deleted || 0} of ${result.requested || identifiers.length} requested contact(s).${missing}${stillPresent}${errors}`);
      state.selectedIds.clear();
      await load(true);
    }

    function uniqueValues(rows, key) {
      return [...new Set(rows.map(row => (row[key] || '').trim()).filter(Boolean))];
    }

    function uniqueListValues(rows, key) {
      return [...new Set(rows.flatMap(row => row[key] || []).map(value => String(value).trim()).filter(Boolean))];
    }

    function optionSelect(name, label, values, primaryValue) {
      const ordered = ['', ...values.filter(value => value !== '')];
      if (primaryValue && !ordered.includes(primaryValue)) ordered.push(primaryValue);
      return `
        <div class="field-row">
          <label for="merge-${name}">${escapeHtml(label)}</label>
          <select id="merge-${name}" data-field="${escapeHtml(name)}">
            ${ordered.map(value => `
              <option value="${escapeHtml(value)}" ${value === (primaryValue || '') ? 'selected' : ''}>
                ${value ? escapeHtml(value) : 'Blank'}
              </option>
            `).join('')}
          </select>
        </div>
      `;
    }

    function checkboxList(name, values) {
      if (!values.length) return '<p class="muted">None</p>';
      return `<div class="option-list">
        ${values.map(value => `
          <label class="option-row">
            <input type="checkbox" data-merge-list="${escapeHtml(name)}" value="${escapeHtml(value)}" checked>
            <span>${escapeHtml(value)}</span>
          </label>
        `).join('')}
      </div>`;
    }

    function renderMergeModal() {
      const rows = selectedRows();
      if (rows.length < 2) {
        alert('Select at least two contacts to merge.');
        return;
      }
      if (selectedAccountKeys().length !== 1) {
        alert('Merge only supports contacts from the same account. Filter to iCloud, Gmail, or Other, then select contacts within that account.');
        return;
      }
      const primary = rows[0];
      const fields = [
        ['firstName', 'First name'],
        ['lastName', 'Last name'],
        ['organization', 'Organization'],
        ['nickname', 'Nickname'],
      ];
      el.mergeBody.innerHTML = `
        <div class="modal-section">
          <h3>Primary Contact</h3>
          <div class="option-list">
            ${rows.map((row, index) => `
              <label class="option-row">
                <input type="radio" name="merge-primary" value="${escapeHtml(row.contactIdentifier)}" ${index === 0 ? 'checked' : ''}>
                <span>
                  <strong>${escapeHtml(row.name)}</strong><br>
                  <span class="muted">${escapeHtml(row.accountName)} · ${escapeHtml([...row.phones, ...row.emails].join(', ') || 'No phone/email')}</span>
                </span>
              </label>
            `).join('')}
          </div>
        </div>
        <div class="modal-section">
          <h3>Field Values</h3>
          ${fields.map(([key, label]) => optionSelect(key, label, uniqueValues(rows, key), primary[key] || '')).join('')}
        </div>
        <div class="modal-section">
          <h3>Phone Numbers To Keep</h3>
          ${checkboxList('phones', uniqueListValues(rows, 'phones'))}
        </div>
        <div class="modal-section">
          <h3>Email Addresses To Keep</h3>
          ${checkboxList('emails', uniqueListValues(rows, 'emails'))}
        </div>
      `;
      el.mergeModal.hidden = false;
    }

    function closeMergeModal() {
      el.mergeModal.hidden = true;
      el.mergeBody.innerHTML = '';
    }

    async function mergeSelectedContacts() {
      const rows = selectedRows();
      const accountKeys = selectedAccountKeys();
      if (accountKeys.length !== 1) {
        alert('Merge only supports contacts from the same account.');
        return;
      }
      const primaryInput = el.mergeBody.querySelector('input[name="merge-primary"]:checked');
      const primaryIdentifier = primaryInput ? primaryInput.value : '';
      if (!primaryIdentifier) {
        alert('Choose a primary contact.');
        return;
      }
      const fields = {};
      el.mergeBody.querySelectorAll('select[data-field]').forEach(select => {
        fields[select.dataset.field] = select.value;
      });
      const phones = [...el.mergeBody.querySelectorAll('input[data-merge-list="phones"]:checked')].map(input => input.value);
      const emails = [...el.mergeBody.querySelectorAll('input[data-merge-list="emails"]:checked')].map(input => input.value);
      const deleteIdentifiers = rows
        .map(row => row.contactIdentifier)
        .filter(identifier => identifier && identifier !== primaryIdentifier);
      if (!confirm(`Merge ${deleteIdentifiers.length + 1} selected contacts into one primary contact?\n\nMerged-away contacts will be deleted.`)) {
        return;
      }
      el.confirmMerge.disabled = true;
      el.confirmMerge.textContent = 'Merging...';
      const response = await fetch('/api/merge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ primaryIdentifier, deleteIdentifiers, fields, phones, emails, accountKey: accountKeys[0] }),
      });
      const result = await response.json();
      const errors = result.errors && result.errors.length ? `\n\nErrors:\n${result.errors.join('\n')}` : '';
      const stillPresent = result.stillPresent && result.stillPresent.length ? `\n\nStill present after merge: ${result.stillPresent.length}` : '';
      alert(`Updated primary contact: ${result.updated ? 'yes' : 'no'}\nDeleted merged-away contacts: ${result.deleted || 0}${stillPresent}${errors}`);
      el.confirmMerge.disabled = false;
      el.confirmMerge.textContent = 'Merge';
      closeMergeModal();
      state.selectedIds.clear();
      await load(true);
    }

    async function load(refresh = false) {
      el.rows.innerHTML = '<tr><td class="loading" colspan="13">Loading Contacts and Messages metadata...</td></tr>';
      const years = el.years.value;
      const response = await fetch(`/api/contacts?years=${encodeURIComponent(years)}${refresh ? '&refresh=1' : ''}`);
      const payload = await response.json();
      state.payload = payload;
      state.contacts = payload.contacts || [];
      renderAccountOptions(payload.accountOptions || []);
      renderWarnings(payload.warnings || []);
      renderRows(payload);
    }

    el.rows.addEventListener('mousedown', event => {
      if (event.shiftKey && event.target.closest('tr[data-id]')) {
        event.preventDefault();
      }
    });
    el.rows.addEventListener('click', event => {
      const row = event.target.closest('tr[data-id]');
      if (row) {
        event.preventDefault();
        selectRow(row, event);
      }
    });
    el.search.addEventListener('input', () => renderRows(state.payload || { summary: window.lastSummary || {} }));
    el.account.addEventListener('change', () => {
      state.selectedIds.clear();
      renderRows(state.payload || { summary: window.lastSummary || {} });
    });
    el.view.addEventListener('change', () => renderRows(state.payload || { summary: window.lastSummary || {} }));
    el.sort.addEventListener('change', () => {
      state.sortKey = el.sort.value;
      state.sortDirection = state.sortKey === 'name' ? 1 : 1;
      renderRows(state.payload || { summary: window.lastSummary || {} });
    });
    el.years.addEventListener('change', () => load(true));
    el.refresh.addEventListener('click', () => load(true));
    el.clearSelection.addEventListener('click', () => {
      state.selectedIds.clear();
      state.lastSelectedIndex = null;
      renderRows(state.payload || { summary: window.lastSummary || {} });
    });
    el.mergeSelected.addEventListener('click', renderMergeModal);
    el.closeMerge.addEventListener('click', closeMergeModal);
    el.cancelMerge.addEventListener('click', closeMergeModal);
    el.confirmMerge.addEventListener('click', () => {
      mergeSelectedContacts().catch(error => {
        el.confirmMerge.disabled = false;
        el.confirmMerge.textContent = 'Merge';
        alert(error.message);
      });
    });
    el.deleteSelected.addEventListener('click', () => {
      deleteSelectedContacts().catch(error => alert(error.message));
    });

    document.querySelectorAll('th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (state.sortKey === key) state.sortDirection *= -1;
        else {
          state.sortKey = key;
          state.sortDirection = key === 'name' ? 1 : 1;
        }
        el.sort.value = ['candidate', 'name', 'lastSent', 'sentTotal'].includes(key) ? key : el.sort.value;
        renderRows(state.payload || { summary: window.lastSummary || {} });
      });
    });

    const originalRenderSummary = renderSummary;
    renderSummary = function(payload, visibleCount) {
      window.lastSummary = payload.summary || window.lastSummary || {};
      originalRenderSummary(payload, visibleCount);
    };

    load(false).catch(error => {
      el.rows.innerHTML = `<tr><td class="loading" colspan="13">${escapeHtml(error.message)}</td></tr>`;
    });
  </script>
</body>
</html>
"""


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
            self.send_html(INDEX_HTML)
            return
        if parsed.path == "/api/contacts":
            params = parse_qs(parsed.query)
            raw_years = params.get("years", ["10"])[0]
            years = None if raw_years == "all" else int(raw_years)
            refresh = params.get("refresh", ["0"])[0] == "1"
            try:
                payload = cached_payload(self.contacts_db_paths, self.messages_db, years, refresh)
            except Exception as exc:
                self.send_json({"warnings": [html.escape(str(exc))], "contacts": [], "summary": {}}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_json(payload)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/delete", "/api/merge"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            if parsed.path == "/api/delete":
                identifiers = payload.get("identifiers", [])
                if not isinstance(identifiers, list) or not all(isinstance(item, str) for item in identifiers):
                    raise ValueError("identifiers must be a list of strings")
                result = delete_contacts_with_contacts_app(identifiers)
            else:
                if not isinstance(payload, dict):
                    raise ValueError("merge payload must be an object")
                result = merge_contacts_with_contacts_app(payload)
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
