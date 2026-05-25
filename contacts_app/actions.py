from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import clear_cache


BACKUP_DIR = Path("contacts_backups")


def write_backup(action: str, contacts: list[dict[str, Any]], request: dict[str, Any]) -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_action = "".join(char for char in action if char.isalnum() or char in ("-", "_")) or "contacts"
    path = BACKUP_DIR / f"{timestamp}-{safe_action}.json"
    path.write_text(
        json.dumps(
            {
                "action": action,
                "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                "contacts": contacts,
                "request": request,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return str(path)


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


def delete_contacts_with_framework(identifiers: list[str], dry_run: bool = False) -> dict[str, Any]:
    clean_identifiers = sorted({identifier for identifier in identifiers if identifier})
    if not clean_identifiers:
        return {"requested": 0, "deleted": 0, "missing": [], "stillPresent": [], "errors": ["No deletable Contacts identifiers were provided."]}
    if dry_run:
        return {"requested": len(clean_identifiers), "deleted": 0, "missing": [], "stillPresent": [], "errors": [], "dryRun": True, "wouldDelete": len(clean_identifiers)}

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
    clear_cache()
    return result


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def applescript_list(values: list[str]) -> str:
    return "{" + ", ".join(applescript_string(value) for value in values) + "}"


def delete_contacts_with_contacts_app(identifiers: list[str], dry_run: bool = False) -> dict[str, Any]:
    clean_identifiers = sorted({identifier for identifier in identifiers if identifier})
    if not clean_identifiers:
        return {"requested": 0, "deleted": 0, "missing": [], "stillPresent": [], "errors": ["No deletable Contacts identifiers were provided."]}
    if dry_run:
        return {
            "requested": len(clean_identifiers),
            "deleted": 0,
            "missing": [],
            "stillPresent": [],
            "errors": [],
            "dryRun": True,
            "wouldDelete": len(clean_identifiers),
            "method": "contacts_app",
        }

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
    clear_cache()
    return result


def merge_contacts_with_contacts_app(payload: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
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
    if dry_run:
        return {
            "updated": False,
            "deleted": 0,
            "missing": [],
            "stillPresent": [],
            "errors": [],
            "dryRun": True,
            "wouldUpdate": True,
            "wouldDelete": len(delete_identifiers),
            "method": "contacts_app",
        }

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
    clear_cache()
    return result
