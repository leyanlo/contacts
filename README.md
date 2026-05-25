# Contacts

Utilities for reviewing, pruning, and merging contacts.

## Install

```sh
npm install
```

The local cleanup app uses Python 3 and macOS Contacts/Messages data directly. It has no third-party Python dependencies.

## VCF Scripts

These scripts operate on exported `.vcf` files. They do not modify macOS Contacts.

Merge multiple VCF files into one:

```sh
npm run merge-contacts -- contacts1.vcf contacts2.vcf
# writes merged-contacts.vcf
```

Remove contacts that have neither email nor phone:

```sh
npm run prune-contacts -- contacts.vcf
# writes pruned-contacts.vcf
```

Use `--output` or `-o` to choose an output file.

## Local Cleanup App

Run:

```sh
python3 contacts_cleanup_app.py --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

The app reads local Contacts and Messages metadata, then ranks cleanup candidates. Message body text is not read.

Useful filters include:

- Account: All Contacts, iCloud, Gmail, Other
- Never texted
- Texted before
- No phone number
- No email
- No phone/email
- Empty placeholders

## macOS Permissions

Messages data is protected by Full Disk Access. Grant access to the app that starts the server:

- Codex, if started from Codex
- Terminal or your terminal app, if started from a shell

Contacts deletion/merge uses Contacts.app automation. macOS may ask for permission to control Contacts.

## Delete, Prune, And Merge Safety

Dry Run is enabled by default. With Dry Run on, delete, prune, and merge requests do not mutate Contacts.

Before every delete, prune, or merge request, the server writes a JSON backup of the affected rows to:

```text
contacts_backups/
```

Those backups are ignored by git.

The Prune control bulk-targets the current account filter. It can prune contacts with no phone/email, no phone number, or no email address. Empty placeholder contacts are excluded from the bulk prune action so they stay visible as a separate cleanup bucket.

Merge only supports contacts from the same account, for example iCloud-to-iCloud or Gmail-to-Gmail. The app lets you choose:

- Primary contact
- First name
- Last name
- Organization
- Nickname
- Phone numbers to keep
- Email addresses to keep

The live Contacts merge flow does not use `scripts/merge-contacts.js`. That script concatenates VCF files; the app merge updates one live macOS contact and deletes the merged-away contacts through Contacts.app scripting.

## Project Layout

```text
contacts_cleanup_app.py        # launcher
contacts_app/
  actions.py                   # Contacts.app delete/merge actions and backups
  models.py                    # app data models
  server.py                    # local HTTP API/server
  store.py                     # Contacts and Messages readers/ranking
  static/index.html            # browser UI
messages_histogram.py          # standalone Messages histogram script
scripts/
  merge-contacts.js            # VCF concatenation helper
  prune-contacts.js            # VCF pruning helper
```
