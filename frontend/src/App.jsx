import { useEffect, useMemo, useState } from "react";
import { Check, CheckSquare, GitMerge, RefreshCw, Scissors, Search, Square, Trash2, X } from "lucide-react";

const SORT_OPTIONS = [
  ["candidate", "Removal candidates"],
  ["name", "Name"],
  ["lastSent", "Oldest last text"],
  ["sentTotal", "Fewest texts"],
];

const VIEW_OPTIONS = [
  ["all", "All contacts"],
  ["never", "Never texted"],
  ["texted", "Texted before"],
  ["nophone", "No phone number"],
  ["noemail", "No email"],
  ["nohandles", "No phone/email"],
  ["placeholders", "Empty placeholders"],
];

const YEARS_OPTIONS = [
  ["10", "10 years"],
  ["5", "5 years"],
  ["2", "2 years"],
  ["1", "1 year"],
  ["all", "All local Messages"],
];

const PRUNE_OPTIONS = [
  ["nohandles", "No phone/email"],
  ["nophone", "No phone"],
  ["noemail", "No email"],
];

function fmt(value) {
  return Number(value || 0).toLocaleString();
}

function metricValue(value) {
  return value == null ? "Unknown" : fmt(value);
}

function rowHandles(row) {
  return [...(row.phones || []), ...(row.emails || [])];
}

function handleText(row) {
  const handles = rowHandles(row);
  return handles.length ? handles.join(", ") : "None";
}

function uniqueValues(rows, key) {
  return [...new Set(rows.map((row) => String(row[key] || "").trim()).filter(Boolean))];
}

function uniqueListValues(rows, key) {
  return [...new Set(rows.flatMap((row) => row[key] || []).map((value) => String(value).trim()).filter(Boolean))];
}

function candidateCompare(a, b) {
  if (!a.messagesAvailable || !b.messagesAvailable) return a.name.localeCompare(b.name);
  if (Number(a.neverTexted) !== Number(b.neverTexted)) return Number(b.neverTexted) - Number(a.neverTexted);
  return a.keepScore - b.keepScore || a.sentTotal - b.sentTotal || a.name.localeCompare(b.name);
}

function reason(row) {
  if (!row.messagesAvailable) return ["Messages unavailable", "bg-red-50 text-red-700"];
  if (!row.hasTextHandle) return ["No phone/email", "bg-red-50 text-red-700"];
  if (!row.hasPhone) return ["No phone", "bg-red-50 text-red-700"];
  if (!row.hasEmail) return ["No email", "bg-red-50 text-red-700"];
  if (row.neverTexted) return ["Never texted", "bg-red-50 text-red-700"];
  return ["Low frecency", "bg-emerald-50 text-emerald-700"];
}

function pruneCriterion(mode) {
  if (mode === "nophone") {
    return {
      label: "no phone",
      emptyMessage: "No contacts without phone numbers were found for the current account filter.",
      destructiveText: "This deletes contacts that do not have a phone number.",
      matches: (row) => !row.hasPhone,
    };
  }
  if (mode === "noemail") {
    return {
      label: "no email",
      emptyMessage: "No contacts without email addresses were found for the current account filter.",
      destructiveText: "This deletes contacts that do not have an email address.",
      matches: (row) => !row.hasEmail,
    };
  }
  return {
    label: "no phone/email",
    emptyMessage: "No contacts without phone/email were found for the current account filter.",
    destructiveText: "This deletes contacts with neither phone nor email.",
    matches: (row) => !row.hasTextHandle,
  };
}

function resultMessage(kind, result, fallbackCount) {
  const errors = result.errors?.length ? `\n\nErrors:\n${result.errors.join("\n")}` : "";
  const missing = result.missing?.length ? `\n\nMissing/skipped: ${result.missing.length}` : "";
  const stillPresent = result.stillPresent?.length ? `\n\nStill present after ${kind}: ${result.stillPresent.length}` : "";
  const backup = result.backupPath ? `\nBackup: ${result.backupPath}` : "";
  if (kind === "merge") {
    const summary = result.dryRun
      ? `Dry run: would update primary contact and delete ${result.wouldDelete || 0} merged-away contact(s).`
      : `Updated primary contact: ${result.updated ? "yes" : "no"}\nDeleted merged-away contacts: ${result.deleted || 0}`;
    return `${summary}${stillPresent}${backup}${errors}`;
  }
  const verb = kind === "prune" ? "prune" : "delete";
  const past = kind === "prune" ? "Pruned" : "Deleted";
  const summary = result.dryRun
    ? `Dry run: would ${verb} ${result.wouldDelete || 0} contact(s).`
    : `${past} ${result.deleted || 0} of ${result.requested || fallbackCount} contact(s).`;
  return `${summary}${missing}${stillPresent}${backup}${errors}`;
}

function Metric({ label, value }) {
  return (
    <div className="min-w-0 rounded-lg border border-stone-300 bg-white px-3 py-2">
      <strong className="block text-2xl leading-tight">{metricValue(value)}</strong>
      <span className="text-xs text-stone-500">{label}</span>
    </div>
  );
}

function Modal({ title, children, footer, onClose, wide = false }) {
  return (
    <section className="modal-backdrop">
      <div className={`modal-panel w-full ${wide ? "max-w-6xl" : "max-w-4xl"}`} role="dialog" aria-modal="true">
        <div className="flex items-center justify-between gap-3 border-b border-stone-300 px-4 py-3">
          <h2 className="m-0 text-lg font-semibold tracking-normal">{title}</h2>
          <button className="icon-button border-stone-300 bg-white text-zinc-900" type="button" onClick={onClose} aria-label="Close">
            <X size={16} />
            Close
          </button>
        </div>
        {children}
        <div className="flex justify-end gap-2 border-t border-stone-300 px-4 py-3">{footer}</div>
      </div>
    </section>
  );
}

function App() {
  const [payload, setPayload] = useState({ contacts: [], summary: {}, accountOptions: [], warnings: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [account, setAccount] = useState("all");
  const [view, setView] = useState("all");
  const [years, setYears] = useState("10");
  const [sortKey, setSortKey] = useState("candidate");
  const [sortDirection, setSortDirection] = useState(1);
  const [pruneMode, setPruneMode] = useState("nohandles");
  const [dryRun, setDryRun] = useState(true);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [lastSelectedIndex, setLastSelectedIndex] = useState(null);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeState, setMergeState] = useState(null);
  const [pruneState, setPruneState] = useState(null);
  const [busyAction, setBusyAction] = useState("");

  const contacts = payload.contacts || [];

  async function load(refresh = false) {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`/api/contacts?years=${encodeURIComponent(years)}${refresh ? "&refresh=1" : ""}`);
      const data = await response.json();
      setPayload(data);
      if (!data.accountOptions?.some((option) => option.key === account)) setAccount("all");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [years]);

  const filteredRows = useMemo(() => {
    const query = search.trim().toLowerCase();
    return contacts.filter((row) => {
      if (account !== "all" && row.accountKey !== account) return false;
      if (!row.messagesAvailable && (view === "never" || view === "texted")) return false;
      if (view !== "placeholders" && row.isPlaceholder) return false;
      if (view === "never" && !row.neverTexted) return false;
      if (view === "texted" && row.neverTexted) return false;
      if (view === "nophone" && row.hasPhone) return false;
      if (view === "noemail" && row.hasEmail) return false;
      if (view === "nohandles" && row.hasTextHandle) return false;
      if (view === "placeholders" && !row.isPlaceholder) return false;
      if (!query) return true;
      return [row.name, row.accountName, ...(row.phones || []), ...(row.emails || []), ...(row.matchedHandles || [])]
        .join(" ")
        .toLowerCase()
        .includes(query);
    });
  }, [account, contacts, search, view]);

  const visibleRows = useMemo(() => {
    return [...filteredRows].sort((a, b) => {
      if (sortKey === "candidate") return candidateCompare(a, b);
      if (sortKey === "name") return sortDirection * a.name.localeCompare(b.name);
      if (sortKey === "lastSent") return sortDirection * ((a.daysSinceSent ?? 999999) - (b.daysSinceSent ?? 999999));
      return sortDirection * ((a[sortKey] ?? 0) - (b[sortKey] ?? 0));
    });
  }, [filteredRows, sortDirection, sortKey]);

  const selectedRows = useMemo(() => contacts.filter((row) => selectedIds.has(row.id)), [contacts, selectedIds]);
  const selectedAccountKeys = useMemo(() => [...new Set(selectedRows.map((row) => row.accountKey))], [selectedRows]);
  const canMerge = selectedRows.length >= 2 && selectedAccountKeys.length === 1;

  function toggleSort(key) {
    if (sortKey === key) setSortDirection((current) => current * -1);
    else {
      setSortKey(key);
      setSortDirection(1);
    }
  }

  function selectRow(row, index, event) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (event.shiftKey && lastSelectedIndex != null) {
        const start = Math.min(lastSelectedIndex, index);
        const end = Math.max(lastSelectedIndex, index);
        for (let i = start; i <= end; i += 1) {
          if (visibleRows[i]) next.add(visibleRows[i].id);
        }
      } else if (next.has(row.id)) {
        next.delete(row.id);
      } else {
        next.add(row.id);
      }
      return next;
    });
    if (!event.shiftKey || lastSelectedIndex == null) setLastSelectedIndex(index);
  }

  function clearSelection() {
    setSelectedIds(new Set());
    setLastSelectedIndex(null);
  }

  async function postAction(kind, body) {
    setBusyAction(kind);
    try {
      const response = await fetch(`/api/${kind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return await response.json();
    } finally {
      setBusyAction("");
    }
  }

  async function deleteSelectedContacts() {
    const identifiers = selectedRows.map((row) => row.contactIdentifier).filter(Boolean);
    const skipped = selectedRows.length - identifiers.length;
    if (!identifiers.length) {
      alert("None of the selected rows have a Contacts identifier that can be deleted.");
      return;
    }
    const warning = skipped ? `\n\n${skipped} selected row(s) do not have a deletable Contacts identifier and will be skipped.` : "";
    const consequence = dryRun ? "No Contacts data will be changed." : "This changes your Contacts data.";
    if (!confirm(`${dryRun ? "Dry run delete" : "Delete"} ${identifiers.length} selected contact(s)?${warning}\n\n${consequence}`)) return;
    const result = await postAction("delete", { identifiers, contacts: selectedRows, dryRun });
    alert(resultMessage("delete", result, identifiers.length));
    clearSelection();
    await load(true);
  }

  function openMergeModal() {
    if (selectedRows.length < 2) {
      alert("Select at least two contacts to merge.");
      return;
    }
    if (selectedAccountKeys.length !== 1) {
      alert("Merge only supports contacts from the same account. Filter to iCloud, Gmail, or Other, then select contacts within that account.");
      return;
    }
    const primary = selectedRows[0];
    setMergeState({
      primaryIdentifier: primary.contactIdentifier,
      fields: {
        firstName: primary.firstName || "",
        lastName: primary.lastName || "",
        organization: primary.organization || "",
        nickname: primary.nickname || "",
      },
      phones: new Set(uniqueListValues(selectedRows, "phones")),
      emails: new Set(uniqueListValues(selectedRows, "emails")),
    });
    setMergeOpen(true);
  }

  async function mergeSelectedContacts() {
    if (!mergeState?.primaryIdentifier) {
      alert("Choose a primary contact.");
      return;
    }
    const deleteIdentifiers = selectedRows
      .map((row) => row.contactIdentifier)
      .filter((identifier) => identifier && identifier !== mergeState.primaryIdentifier);
    if (!deleteIdentifiers.length) {
      alert("Choose at least one merged-away contact.");
      return;
    }
    const consequence = dryRun ? "No Contacts data will be changed." : "Merged-away contacts will be deleted.";
    if (!confirm(`${dryRun ? "Dry run merge" : "Merge"} ${deleteIdentifiers.length + 1} selected contacts into one primary contact?\n\n${consequence}`)) return;
    const result = await postAction("merge", {
      primaryIdentifier: mergeState.primaryIdentifier,
      deleteIdentifiers,
      fields: mergeState.fields,
      phones: [...mergeState.phones],
      emails: [...mergeState.emails],
      accountKey: selectedAccountKeys[0],
      contacts: selectedRows,
      dryRun,
    });
    alert(resultMessage("merge", result, deleteIdentifiers.length));
    setMergeOpen(false);
    setMergeState(null);
    clearSelection();
    await load(true);
  }

  function openPruneModal() {
    const activeCriterion = pruneCriterion(pruneMode);
    const allRows = contacts.filter((row) => {
      if (account !== "all" && row.accountKey !== account) return false;
      return activeCriterion.matches(row) && !row.isPlaceholder;
    });
    const rows = allRows.filter((row) => row.contactIdentifier);
    if (!rows.length) {
      alert(activeCriterion.emptyMessage);
      return;
    }
    setPruneState({
      criterion: activeCriterion,
      rows,
      selected: new Set(rows.map((row) => row.id)),
      skipped: allRows.length - rows.length,
      accountLabel: payload.accountOptions?.find((option) => option.key === account)?.name || "All Contacts",
      dryRun,
    });
  }

  function updatePruneSelection(mode, rowId) {
    setPruneState((current) => {
      if (!current) return current;
      const selected = new Set(current.selected);
      if (mode === "all") current.rows.forEach((row) => selected.add(row.id));
      else if (mode === "none") selected.clear();
      else if (selected.has(rowId)) selected.delete(rowId);
      else selected.add(rowId);
      return { ...current, selected };
    });
  }

  async function pruneContacts() {
    if (!pruneState) return;
    const rows = pruneState.rows.filter((row) => pruneState.selected.has(row.id));
    const identifiers = rows.map((row) => row.contactIdentifier).filter(Boolean);
    if (!identifiers.length) {
      alert("Select at least one contact to prune.");
      return;
    }
    if (!pruneState.dryRun && !confirm(`Prune ${identifiers.length} selected contact(s) with ${pruneState.criterion.label} in ${pruneState.accountLabel}?\n\n${pruneState.criterion.destructiveText}`)) return;
    const result = await postAction("prune", { identifiers, contacts: rows, dryRun: pruneState.dryRun });
    alert(resultMessage("prune", result, identifiers.length));
    setPruneState(null);
    await load(true);
  }

  return (
    <div>
      <header className="sticky top-0 z-10 border-b border-stone-300 bg-stone-50 px-6 py-4">
        <h1 className="mb-3 text-[22px] font-semibold tracking-normal">Contacts Cleanup</h1>
        <div className="grid grid-cols-[minmax(220px,1fr)_repeat(4,minmax(130px,max-content))_max-content] items-center gap-2 max-[900px]:grid-cols-2">
          <label className="relative max-[900px]:col-span-2">
            <Search className="pointer-events-none absolute left-3 top-2.5 text-stone-500" size={16} />
            <input className="control w-full pl-9" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search names, numbers, emails" type="search" />
          </label>
          <select className="control" value={account} onChange={(event) => { setAccount(event.target.value); clearSelection(); }}>
            {(payload.accountOptions || [{ key: "all", name: "All Contacts", count: 0 }]).map((option) => (
              <option key={option.key} value={option.key}>{option.name} ({fmt(option.count)})</option>
            ))}
          </select>
          <select className="control" value={view} onChange={(event) => setView(event.target.value)}>
            {VIEW_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select className="control" value={years} onChange={(event) => setYears(event.target.value)}>
            {YEARS_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <select className="control" value={sortKey} onChange={(event) => { setSortKey(event.target.value); setSortDirection(1); }}>
            {SORT_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <button className="icon-button border-blue-700 bg-blue-700 text-white hover:bg-blue-800" type="button" onClick={() => load(true)} disabled={loading}>
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </header>

      <main className="px-6 py-5">
        <section className="mb-4 grid grid-cols-7 gap-2 max-[1100px]:grid-cols-4 max-[700px]:grid-cols-2">
          <Metric label="Contacts" value={payload.summary?.contacts} />
          <Metric label="Visible" value={visibleRows.length} />
          <Metric label="Never texted" value={payload.summary?.neverTexted} />
          <Metric label="Texted before" value={payload.summary?.textedAtLeastOnce} />
          <Metric label="No phone #" value={payload.summary?.withoutPhone} />
          <Metric label="No email" value={payload.summary?.withoutEmail} />
          <Metric label="Hidden empty" value={payload.summary?.placeholders} />
        </section>

        {!!payload.warnings?.length && (
          <section className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {payload.warnings.map((warning, index) => <div key={index}>{warning}</div>)}
          </section>
        )}

        <section className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-stone-300 bg-white px-3 py-2 max-[900px]:items-start">
          <span className="text-sm font-medium">{fmt(selectedIds.size)} selected</span>
          <div className="flex flex-wrap justify-end gap-2">
            <label className="inline-flex h-9 items-center gap-2 text-sm text-stone-600">
              <input checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} type="checkbox" />
              Dry Run
            </label>
            <button className="icon-button border-stone-300 bg-white text-zinc-900" type="button" onClick={clearSelection} disabled={!selectedIds.size}>
              <X size={16} />
              Clear Selection
            </button>
            <button className="icon-button border-blue-700 bg-blue-700 text-white" type="button" onClick={openMergeModal} disabled={!canMerge}>
              <GitMerge size={16} />
              Merge Selected
            </button>
            <select id="pruneMode" className="control w-40" value={pruneMode} onChange={(event) => setPruneMode(event.target.value)} aria-label="Prune criterion">
              {PRUNE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <button className="icon-button border-amber-700 bg-amber-700 text-white" type="button" onClick={openPruneModal}>
              <Scissors size={16} />
              Prune
            </button>
            <button className="icon-button border-red-700 bg-red-700 text-white" type="button" onClick={deleteSelectedContacts} disabled={!selectedIds.size || busyAction === "delete"}>
              <Trash2 size={16} />
              Delete Selected
            </button>
          </div>
        </section>

        <section className="overflow-auto rounded-lg border border-stone-300 bg-white" style={{ maxHeight: "calc(100vh - 220px)" }}>
          <table className="min-w-[1120px] w-full border-collapse text-sm">
            <thead>
              <tr>
                <HeaderCell />
                <HeaderCell label="Name" onClick={() => toggleSort("name")} />
                <HeaderCell label="Account" />
                <HeaderCell label="Reason" />
                <HeaderCell label="Handles" />
                <HeaderCell label="Keep score" numeric onClick={() => toggleSort("keepScore")} />
                <HeaderCell label="Sent" numeric onClick={() => toggleSort("sentTotal")} />
                <HeaderCell label="Direct" numeric />
                <HeaderCell label="Group" numeric />
                <HeaderCell label="Days since sent" numeric onClick={() => toggleSort("daysSinceSent")} />
                <HeaderCell label="Last sent" />
                <HeaderCell label="Received" numeric />
                <HeaderCell label="Shared" numeric />
              </tr>
            </thead>
            <tbody>
              {loading && <tr><td className="px-4 py-5 text-stone-500" colSpan={13}>Loading Contacts and Messages metadata...</td></tr>}
              {error && <tr><td className="px-4 py-5 text-red-700" colSpan={13}>{error}</td></tr>}
              {!loading && !error && visibleRows.map((row, index) => {
                const [label, badgeClass] = reason(row);
                const selected = selectedIds.has(row.id);
                return (
                  <tr
                    key={row.id}
                    className={`cursor-pointer select-none border-b border-stone-200 hover:bg-blue-50 ${selected ? "bg-blue-100" : ""}`}
                    onMouseDown={(event) => { if (event.shiftKey) event.preventDefault(); }}
                    onClick={(event) => selectRow(row, index, event)}
                  >
                    <td className="w-10 px-3 py-2 text-center">{selected ? <CheckSquare size={18} className="text-blue-700" /> : <Square size={18} className="text-stone-400" />}</td>
                    <td className="max-w-60 overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 font-semibold" title={row.name}>{row.name}</td>
                    <td className="px-3 py-2 text-left">{row.accountName}</td>
                    <td className="px-3 py-2 text-left"><span className={`inline-flex h-6 items-center rounded-full px-2 text-xs font-semibold ${badgeClass}`}>{label}</span></td>
                    <td className="max-w-72 overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 text-left text-stone-600" title={handleText(row)}>{handleText(row)}</td>
                    <td className="px-3 py-2 text-right">{row.keepScore}</td>
                    <td className="px-3 py-2 text-right">{fmt(row.sentTotal)}</td>
                    <td className="px-3 py-2 text-right">{fmt(row.sentDirect)}</td>
                    <td className="px-3 py-2 text-right">{fmt(row.sentGroup)}</td>
                    <td className="px-3 py-2 text-right">{row.daysSinceSent ?? "Never"}</td>
                    <td className="px-3 py-2 text-left">{row.lastSent || <span className="text-stone-500">Never</span>}</td>
                    <td className="px-3 py-2 text-right">{fmt(row.receivedFromThemTotal)}</td>
                    <td className="px-3 py-2 text-right">{fmt(row.sharedTotal)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      </main>

      {mergeOpen && mergeState && (
        <MergeModal
          rows={selectedRows}
          state={mergeState}
          setState={setMergeState}
          onClose={() => setMergeOpen(false)}
          onMerge={mergeSelectedContacts}
          busy={busyAction === "merge"}
        />
      )}

      {pruneState && (
        <PruneModal
          state={pruneState}
          setState={setPruneState}
          onClose={() => setPruneState(null)}
          onPrune={pruneContacts}
          busy={busyAction === "prune"}
        />
      )}
    </div>
  );
}

function HeaderCell({ label = "", numeric = false, onClick }) {
  return (
    <th
      className={`sticky top-0 z-[1] border-b border-stone-300 bg-stone-200 px-3 py-2 text-xs font-semibold text-stone-700 ${numeric ? "text-right" : "text-left"} ${onClick ? "cursor-pointer select-none" : ""}`}
      onClick={onClick}
    >
      {label}
    </th>
  );
}

function FieldSelect({ label, value, values, onChange }) {
  const ordered = ["", ...values.filter((item) => item !== "")];
  if (value && !ordered.includes(value)) ordered.push(value);
  return (
    <label className="grid grid-cols-[120px_1fr] items-center gap-2 text-sm">
      <span>{label}</span>
      <select className="control w-full" value={value} onChange={(event) => onChange(event.target.value)}>
        {ordered.map((item) => <option key={item || "blank"} value={item}>{item || "Blank"}</option>)}
      </select>
    </label>
  );
}

function ToggleList({ title, values, selected, onToggle }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-normal text-stone-500">{title}</h3>
      {!values.length && <p className="text-sm text-stone-500">None</p>}
      <div className="grid gap-2">
        {values.map((value) => (
          <label key={value} className="flex items-start gap-2 rounded-md border border-stone-300 p-2 text-sm">
            <input checked={selected.has(value)} onChange={() => onToggle(value)} type="checkbox" />
            <span>{value}</span>
          </label>
        ))}
      </div>
    </section>
  );
}

function MergeModal({ rows, state, setState, onClose, onMerge, busy }) {
  const fields = [
    ["firstName", "First name"],
    ["lastName", "Last name"],
    ["organization", "Organization"],
    ["nickname", "Nickname"],
  ];
  const phones = uniqueListValues(rows, "phones");
  const emails = uniqueListValues(rows, "emails");

  function setField(key, value) {
    setState((current) => ({ ...current, fields: { ...current.fields, [key]: value } }));
  }

  function toggleList(key, value) {
    setState((current) => {
      const next = new Set(current[key]);
      if (next.has(value)) next.delete(value);
      else next.add(value);
      return { ...current, [key]: next };
    });
  }

  return (
    <Modal
      title="Merge Contacts"
      onClose={onClose}
      footer={(
        <>
          <button className="icon-button border-stone-300 bg-white text-zinc-900" type="button" onClick={onClose}>Cancel</button>
          <button className="icon-button border-blue-700 bg-blue-700 text-white" type="button" onClick={onMerge} disabled={busy}>
            <GitMerge size={16} />
            Merge
          </button>
        </>
      )}
    >
      <div className="grid grid-cols-2 gap-5 p-4 max-[800px]:grid-cols-1">
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-normal text-stone-500">Primary Contact</h3>
          <div className="grid gap-2">
            {rows.map((row, index) => (
              <label key={row.id} className="flex items-start gap-2 rounded-md border border-stone-300 p-2 text-sm">
                <input
                  checked={state.primaryIdentifier === row.contactIdentifier}
                  name="merge-primary"
                  onChange={() => setState((current) => ({ ...current, primaryIdentifier: row.contactIdentifier }))}
                  type="radio"
                />
                <span>
                  <strong>{row.name}</strong>
                  <br />
                  <span className="text-stone-500">{row.accountName} · {handleText(row) || "No phone/email"}{index === 0 ? "" : ""}</span>
                </span>
              </label>
            ))}
          </div>
        </section>
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-normal text-stone-500">Field Values</h3>
          <div className="grid gap-2">
            {fields.map(([key, label]) => (
              <FieldSelect key={key} label={label} value={state.fields[key] || ""} values={uniqueValues(rows, key)} onChange={(value) => setField(key, value)} />
            ))}
          </div>
        </section>
        <ToggleList title="Phone Numbers To Keep" values={phones} selected={state.phones} onToggle={(value) => toggleList("phones", value)} />
        <ToggleList title="Email Addresses To Keep" values={emails} selected={state.emails} onToggle={(value) => toggleList("emails", value)} />
      </div>
    </Modal>
  );
}

function PruneModal({ state, setState, onClose, onPrune, busy }) {
  const selectedCount = state.selected.size;

  function setAll(checked) {
    setState((current) => ({ ...current, selected: checked ? new Set(current.rows.map((row) => row.id)) : new Set() }));
  }

  function toggle(rowId) {
    setState((current) => {
      const selected = new Set(current.selected);
      if (selected.has(rowId)) selected.delete(rowId);
      else selected.add(rowId);
      return { ...current, selected };
    });
  }

  return (
    <Modal
      title="Prune Preview"
      wide
      onClose={onClose}
      footer={(
        <>
          <button className="icon-button border-stone-300 bg-white text-zinc-900" type="button" onClick={onClose}>Cancel</button>
          <button className="icon-button border-amber-700 bg-amber-700 text-white" type="button" onClick={onPrune} disabled={!selectedCount || busy}>
            <Scissors size={16} />
            {state.dryRun ? "Dry Run Prune" : "Prune"}
          </button>
        </>
      )}
    >
      <div className="p-4">
        <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-stone-600">
          <strong className="text-zinc-900">{fmt(selectedCount)} of {fmt(state.rows.length)} selected</strong>
          <span>{state.accountLabel}</span>
          <span>{state.criterion.label}</span>
          <span>{state.dryRun ? "Dry Run: no Contacts data will be changed" : state.criterion.destructiveText}</span>
          {!!state.skipped && <span>{fmt(state.skipped)} non-deletable row(s) skipped</span>}
          <span className="flex-1" />
          <button className="icon-button h-8 border-stone-300 bg-white text-zinc-900" type="button" onClick={() => setAll(true)}>
            <Check size={15} />
            Select All
          </button>
          <button className="icon-button h-8 border-stone-300 bg-white text-zinc-900" type="button" onClick={() => setAll(false)}>
            <X size={15} />
            Select None
          </button>
        </div>
        <div className="max-h-[min(520px,calc(100vh-260px))] overflow-auto rounded-lg border border-stone-300">
          <table className="min-w-[760px] w-full border-collapse text-sm">
            <thead>
              <tr>
                <HeaderCell />
                <HeaderCell label="Name" />
                <HeaderCell label="Account" />
                <HeaderCell label="Handles" />
                <HeaderCell label="Sent" numeric />
                <HeaderCell label="Last sent" />
              </tr>
            </thead>
            <tbody>
              {state.rows.map((row) => (
                <tr key={row.id} className="border-b border-stone-200">
                  <td className="w-10 px-3 py-2 text-center">
                    <input checked={state.selected.has(row.id)} onChange={() => toggle(row.id)} type="checkbox" aria-label={`Prune ${row.name}`} />
                  </td>
                  <td className="max-w-60 overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 font-semibold" title={row.name}>{row.name}</td>
                  <td className="px-3 py-2">{row.accountName}</td>
                  <td className="max-w-80 overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 text-stone-600" title={handleText(row)}>{handleText(row)}</td>
                  <td className="px-3 py-2 text-right">{fmt(row.sentTotal)}</td>
                  <td className="px-3 py-2">{row.lastSent || <span className="text-stone-500">Never</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Modal>
  );
}

export default App;
