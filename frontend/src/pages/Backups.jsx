import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Td, TdActions } from "../components/Table";
import NotifyFields, { hasRecipients } from "../components/NotifyFields";
import { canDo, useAuth } from "../auth";

const WEEKDAYS = [
  { value: 1, label: "Monday" },
  { value: 2, label: "Tuesday" },
  { value: 3, label: "Wednesday" },
  { value: 4, label: "Thursday" },
  { value: 5, label: "Friday" },
  { value: 6, label: "Saturday" },
  { value: 0, label: "Sunday" },
];

function emptyForm(targets) {
  const firstDb = targets?.databases?.[0];
  const firstApp = (targets?.apps || []).find((a) => a.folders?.length) || targets?.apps?.[0];
  const firstRemote = targets?.remotes?.[0] || "";
  const kind = firstDb ? "database" : "files";
  const name = kind === "database" ? firstDb?.name || "Database backup" : firstApp?.name || "App files";
  return {
    name,
    enabled: true,
    kind,
    schedule: {
      repeat: "daily",
      time: "03:00",
      weekday: 1,
      monthday: 1,
      timezone: targets?.timezone?.iana || "UTC",
    },
    source: sourceFor(kind, firstDb, firstApp),
    destination_remote: firstRemote,
    destination_folder: "",
    notify: {
      enabled: false,
      account_id: "",
      group_id: "",
      recipient_ids: [],
      to_emails: [],
      on: "always",
      subject_success: "",
      title_success: "",
      message_success: "",
      subject_failure: "",
      title_failure: "",
      message_failure: "",
    },
  };
}

function sourceFor(kind, db, app) {
  if (kind === "files") {
    const folders = (app?.folders || []).filter((f) => f.selected).map((f) => f.path);
    return {
      type: "app_files",
      app_id: app?.id || "",
      paths: folders,
    };
  }
  if (db?.engine === "sqlite") {
    return { type: "sqlite", connection_id: db.id, path: db.path || "" };
  }
  if (db?.engine === "postgres") {
    return {
      type: "postgres",
      connection_id: db.id,
      database: db.database || "",
    };
  }
  return {
    type: "mysql",
    connection_id: db?.id || "",
    database: db?.database || "",
  };
}

function parentFolder(path) {
  const parts = String(path || "")
    .split("/")
    .filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function folderName(path) {
  const parts = String(path || "")
    .split("/")
    .filter(Boolean);
  return parts[parts.length - 1] || "";
}

async function fetchRemoteDirs(remote, path) {
  const data = await api(
    `/rclone/list?fs=${encodeURIComponent(`${remote}:`)}&remote=${encodeURIComponent(path || "")}&dirs_only=true`
  );
  return (data.list || [])
    .filter((item) => item.IsDir)
    .map((item) => {
      const name = item.Name || folderName(item.Path) || "";
      const full = item.Path || (path ? `${String(path).replace(/\/$/, "")}/${name}` : name);
      return { name, path: String(full).replace(/^\/+/, "") };
    })
    .filter((item) => item.name)
    .sort((a, b) => a.name.localeCompare(b.name));
}

function describeSource(job, targets) {
  const src = job.source || {};
  if (src.type === "app_files" || job.kind === "files") {
    const app = (targets.apps || []).find((a) => a.id === src.app_id);
    const paths = src.paths || [];
    const detail = paths.length === 1 ? paths[0] : paths.length ? `${paths.length} folders` : "";
    return `App files · ${app?.name || src.app_id || "app"}${detail ? ` · ${detail}` : ""}`;
  }
  if (src.type === "sqlite") {
    const db = (targets.databases || []).find((d) => d.id === src.connection_id);
    return `Database · ${db?.name || "SQLite"}`;
  }
  if (src.type === "mysql" || src.type === "postgres") {
    const db = (targets.databases || []).find((d) => d.id === src.connection_id);
    const fallback = src.type === "postgres" ? "PostgreSQL" : "MySQL";
    return `Database · ${src.database || db?.database || db?.name || fallback}`;
  }
  if (src.type === "path") return `Files · ${src.path}`;
  return src.type || "—";
}

function notifyFromJob(job) {
  const n = job?.notify || {};
  return {
    enabled: Boolean(n.enabled),
    account_id: n.account_id || "",
    group_id: n.group_id || "",
    recipient_ids: n.recipient_ids || n.contact_ids || [],
    to_emails: n.to_emails || [],
    on: n.on || "always",
    subject_success: n.subject_success || n.subject || "",
    title_success: n.title_success || n.title || "",
    message_success: n.message_success || n.message || "",
    subject_failure: n.subject_failure || n.subject || "",
    title_failure: n.title_failure || n.title || "",
    message_failure: n.message_failure || n.message || "",
  };
}

function emptyGroupForm(jobs, mailAccounts) {
  return {
    name: "",
    job_ids: [],
    timeout_minutes: 120,
    notify: {
      enabled: true,
      account_id: (mailAccounts || [])[0]?.id || "",
      recipient_ids: [],
      to_emails: [],
      on: "always",
      subject_success: "",
      title_success: "",
      message_success: "",
      subject_failure: "",
      title_failure: "",
      message_failure: "",
    },
  };
}

function notifyFromGroup(group) {
  const n = group?.notify || {};
  return {
    enabled: n.enabled !== false,
    account_id: n.account_id || "",
    recipient_ids: n.recipient_ids || n.contact_ids || [],
    to_emails: n.to_emails || [],
    on: n.on || "always",
    subject_success: n.subject_success || "",
    title_success: n.title_success || "",
    message_success: n.message_success || "",
    subject_failure: n.subject_failure || "",
    title_failure: n.title_failure || "",
    message_failure: n.message_failure || "",
  };
}

function EmailCopyFields({ outcome, notify, onChange }) {
  const subjectKey = `subject_${outcome}`;
  const titleKey = `title_${outcome}`;
  const messageKey = `message_${outcome}`;
  const set = (key, value) => onChange({ ...(notify || {}), [key]: value });
  const ok = outcome === "success";
  return (
    <>
      <div className="form-grid cols-2">
        <div className="field">
          <label>Email subject</label>
          <input
            value={notify?.[subjectKey] || ""}
            onChange={(e) => set(subjectKey, e.target.value)}
            placeholder={ok ? "[Backup OK] {name}" : "[Backup FAILED] {name}"}
          />
          <span className="field-hint">Leave blank for the default. Use {"{name}"} for the backup name.</span>
        </div>
        <div className="field">
          <label>Email title</label>
          <input
            value={notify?.[titleKey] || ""}
            onChange={(e) => set(titleKey, e.target.value)}
            placeholder={ok ? "Backup succeeded" : "Backup failed"}
          />
          <span className="field-hint">Heading at the top of the email.</span>
        </div>
      </div>
      <div className="field">
        <label>Message</label>
        <textarea
          className="message-copy"
          rows={4}
          value={notify?.[messageKey] || ""}
          onChange={(e) => set(messageKey, e.target.value)}
          placeholder={
            ok
              ? "Optional text shown under the title when the backup succeeds."
              : "Optional text shown under the title when the backup fails."
          }
        />
        <span className="field-hint">
          Shown under the email title. Recipients with &quot;Include backup details&quot; enabled also get status,
          times, destination, file sync summary (for folder backups), and log.
        </span>
      </div>
    </>
  );
}

export default function BackupsPage() {
  const me = useAuth();
  const canSaveBackup = canDo(me, "backups", "save");
  const canDeleteBackup = canDo(me, "backups", "delete");
  const canRunBackup = canDo(me, "backups", "run");
  const [jobs, setJobs] = useState([]);
  const [history, setHistory] = useState([]);
  const [targets, setTargets] = useState({ apps: [], databases: [], remotes: [] });
  const [form, setForm] = useState(null);
  const [error, setError] = useState("");
  const [log, setLog] = useState("");
  const [saving, setSaving] = useState(false);
  const [runningId, setRunningId] = useState(null);
  const [schemaDbs, setSchemaDbs] = useState([]);
  const [browsePath, setBrowsePath] = useState("");
  const [remoteFolders, setRemoteFolders] = useState([]);
  const [foldersLoading, setFoldersLoading] = useState(false);
  const [foldersError, setFoldersError] = useState("");
  const [appBrowsePath, setAppBrowsePath] = useState("");
  const [appDirs, setAppDirs] = useState([]);
  const [appDirsLoading, setAppDirsLoading] = useState(false);
  const [appDirsError, setAppDirsError] = useState("");
  const [customAppPath, setCustomAppPath] = useState("");
  const [groupForm, setGroupForm] = useState(null);
  const [groupSaving, setGroupSaving] = useState(false);

  const recipients = targets.recipients || [];
  const notifyGroups = targets.notify_groups || [];

  const groupByJobId = useMemo(() => {
    const map = {};
    for (const g of notifyGroups) {
      for (const jid of g.job_ids || []) {
        map[jid] = g;
      }
    }
    return map;
  }, [notifyGroups]);

  const selectedApp = useMemo(
    () => (targets.apps || []).find((a) => a.id === form?.source?.app_id),
    [targets, form]
  );
  const selectedDb = useMemo(
    () => (targets.databases || []).find((d) => d.id === form?.source?.connection_id),
    [targets, form]
  );
  const folderOptions = useMemo(() => {
    const fromApp = selectedApp?.folders || [];
    const byPath = new Map(fromApp.map((f) => [f.path, f]));
    for (const p of form?.source?.paths || []) {
      if (p && !byPath.has(p)) byPath.set(p, { path: p, label: p });
    }
    return [...byPath.values()];
  }, [selectedApp, form?.source?.paths]);

  async function load() {
    try {
      const [jobList, hist, t] = await Promise.all([
        api("/backups"),
        api("/backups/history"),
        api("/backups/targets"),
      ]);
      setJobs(jobList);
      setHistory(hist);
      setTargets(t);
      setForm((prev) => prev || emptyForm(t));
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    const connId = form?.source?.connection_id;
    const engine = selectedDb?.engine;
    if (form?.kind !== "database" || !["mysql", "postgres"].includes(engine) || !connId) {
      setSchemaDbs([]);
      return;
    }
    api(`/db/${encodeURIComponent(connId)}/databases`)
      .then((list) =>
        setSchemaDbs(
          (list || []).filter(
            (n) => !["information_schema", "performance_schema", "mysql", "sys", "template0", "template1"].includes(n)
          )
        )
      )
      .catch(() => setSchemaDbs([]));
  }, [form?.kind, form?.source?.connection_id, selectedDb?.engine]);

  useEffect(() => {
    const remote = form?.destination_remote;
    if (!remote) {
      setRemoteFolders([]);
      setFoldersError("");
      setFoldersLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      setFoldersLoading(true);
      setFoldersError("");
      try {
        let dirs;
        try {
          dirs = await fetchRemoteDirs(remote, browsePath);
        } catch (e) {
          if (!browsePath) throw e;
          dirs = await fetchRemoteDirs(remote, "");
        }
        if (!cancelled) setRemoteFolders(dirs);
      } catch (e) {
        if (!cancelled) {
          setRemoteFolders([]);
          setFoldersError(e.message);
        }
      } finally {
        if (!cancelled) setFoldersLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [form?.destination_remote, browsePath]);

  useEffect(() => {
    const appId = form?.source?.app_id;
    if (form?.kind !== "files" || !appId) {
      setAppDirs([]);
      setAppDirsError("");
      setAppDirsLoading(false);
      return;
    }
    let cancelled = false;
    (async () => {
      setAppDirsLoading(true);
      setAppDirsError("");
      try {
        let data;
        try {
          data = await api(
            `/backups/apps/${encodeURIComponent(appId)}/folders?path=${encodeURIComponent(appBrowsePath || "")}`
          );
        } catch (e) {
          if (!appBrowsePath) throw e;
          data = await api(`/backups/apps/${encodeURIComponent(appId)}/folders?path=`);
          if (!cancelled) setAppBrowsePath("");
        }
        if (!cancelled) setAppDirs(data.dirs || []);
      } catch (e) {
        if (!cancelled) {
          setAppDirs([]);
          setAppDirsError(e.message);
        }
      } finally {
        if (!cancelled) setAppDirsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [form?.kind, form?.source?.app_id, appBrowsePath]);

  function setKind(kind) {
    const db = targets.databases[0];
    const app = (targets.apps || []).find((a) => a.folders?.length) || targets.apps[0];
    const name =
      kind === "database" ? db?.name || "Database backup" : app?.name ? `${app.name} files` : "App files";
    setAppBrowsePath("");
    setCustomAppPath("");
    setForm((prev) => ({
      ...prev,
      kind,
      name,
      source: sourceFor(kind, db, app),
    }));
  }

  function setDatabase(id) {
    const db = targets.databases.find((d) => d.id === id);
    setForm((prev) => ({
      ...prev,
      source: sourceFor("database", db, null),
      name: prev.name,
    }));
  }

  function setApp(id) {
    const app = targets.apps.find((a) => a.id === id);
    setAppBrowsePath("");
    setCustomAppPath("");
    setForm((prev) => ({
      ...prev,
      source: sourceFor("files", null, app),
    }));
  }

  function togglePath(rel) {
    const paths = new Set(form.source.paths || []);
    if (paths.has(rel)) paths.delete(rel);
    else paths.add(rel);
    setForm({ ...form, source: { ...form.source, paths: [...paths] } });
  }

  function addAppFolder(rel) {
    const path = String(rel || "")
      .replace(/\\/g, "/")
      .replace(/^\/+|\/+$/g, "");
    if (!path || path.split("/").includes("..")) return;
    const paths = new Set(form.source.paths || []);
    paths.add(path);
    setForm({ ...form, source: { ...form.source, paths: [...paths] } });
    setAppBrowsePath(path);
    setCustomAppPath(path);
  }

  function pickDestinationFolder(path) {
    setForm((prev) => ({ ...prev, destination_folder: path }));
    setBrowsePath(path);
  }

  async function saveGroup() {
    if (!groupForm?.name || !(groupForm.job_ids || []).length) return;
    setGroupSaving(true);
    setError("");
    try {
      await api("/backups/notify-groups", { method: "POST", body: groupForm });
      setGroupForm(null);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setGroupSaving(false);
    }
  }

  async function removeGroup(id) {
    if (!confirm("Remove this notification group? Backups will no longer be grouped.")) return;
    await api(`/backups/notify-groups/${id}`, { method: "DELETE" });
    await load();
  }

  function editGroup(group) {
    setGroupForm({
      id: group.id,
      name: group.name,
      job_ids: group.job_ids || [],
      timeout_minutes: group.timeout_minutes || 120,
      notify: notifyFromGroup(group),
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function toggleGroupJob(jobId) {
    const ids = new Set(groupForm?.job_ids || []);
    if (ids.has(jobId)) ids.delete(jobId);
    else ids.add(jobId);
    setGroupForm({ ...groupForm, job_ids: [...ids] });
  }

  async function save() {
    if (!form?.name) return;
    setSaving(true);
    setError("");
    try {
      await api("/backups", { method: "POST", body: form });
      setForm(emptyForm(targets));
      setBrowsePath("");
      setAppBrowsePath("");
      setCustomAppPath("");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function remove(id) {
    if (!confirm("Remove this backup?")) return;
    await api(`/backups/${id}`, { method: "DELETE" });
    await load();
  }

  async function run(id) {
    setError("");
    setRunningId(id);
    try {
      const result = await api(`/backups/${id}/run`, { method: "POST" });
      setLog(result.log || JSON.stringify(result, null, 2));
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setRunningId(null);
    }
  }

  function edit(job) {
    const schedule = {
      repeat: "daily",
      time: "03:00",
      weekday: 1,
      monthday: 1,
      timezone: targets.timezone?.iana || "UTC",
      ...(job.schedule || {}),
    };
    let remote = job.destination_remote || "";
    let folder = job.destination_folder || "";
    if (!remote && job.destination) {
      const idx = job.destination.indexOf(":");
      if (idx >= 0) {
        remote = job.destination.slice(0, idx);
        folder = job.destination.slice(idx + 1);
      }
    }
    const kind = job.kind || (job.source?.type === "app_files" || job.source?.type === "path" ? "files" : "database");
    setForm({
      id: job.id,
      name: job.name,
      enabled: job.enabled !== false,
      kind,
      schedule,
      source: job.source || sourceFor(kind, targets.databases[0], targets.apps[0]),
      destination_remote: remote,
      destination_folder: folder,
      notify: notifyFromJob(job),
    });
    setBrowsePath(folder);
    setAppBrowsePath((job.source?.paths || [])[0] || "");
    setCustomAppPath((job.source?.paths || [])[0] || "");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  if (!form) {
    return (
      <div>
        <h1>Backups</h1>
        <p className="muted">Loading…</p>
      </div>
    );
  }

  const schedule = form.schedule || { repeat: "daily", time: "03:00" };
  const inNotifyGroup = Boolean(form.notify?.group_id);
  const assignedGroup = groupByJobId[form.id] || notifyGroups.find((g) => g.id === form.notify?.group_id);
  const canSave =
    form.name &&
    form.destination_remote &&
    (form.kind === "database" ? form.source.connection_id : form.source.app_id && (form.source.paths || []).length) &&
    (!form.notify?.enabled ||
      inNotifyGroup ||
      (form.notify?.account_id && hasRecipients(form.notify)));

  return (
    <div>
      <div className="page-header">
        <h1>Backups</h1>
        <div className="page-header-actions">
          <button onClick={load}>Refresh</button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      <p className="muted">
        Save a database snapshot, or keep an app’s files in sync with cloud storage (only new and changed files are
        uploaded). App file backups copy the files inside each selected folder into the destination — not the whole
        storage tree.
      </p>

      {canSaveBackup && (
      <>
      <div className="panel stack" style={{ marginBottom: "1rem" }}>
        <h2 style={{ marginTop: 0 }}>Notification groups</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Combine multiple backups into one email. Scheduled runs wait until every backup in the group finishes, or
          send after the timeout with a note about missing jobs. Clicking Run still emails that backup immediately.
          Add recipients on the <Link to="/mail">Mail</Link> page.
        </p>
        {groupForm ? (
          <>
            <div className="field">
              <label>Group name</label>
              <input
                value={groupForm.name}
                onChange={(e) => setGroupForm({ ...groupForm, name: e.target.value })}
                placeholder="Nightly backups"
              />
            </div>
            <div className="field">
              <label>Backups in this group</label>
              {jobs.length === 0 ? (
                <p className="muted">Save at least one backup first.</p>
              ) : (
                <div className="stack">
                  {jobs.map((j) => (
                    <label key={j.id} className="check-row">
                      <input
                        type="checkbox"
                        checked={(groupForm.job_ids || []).includes(j.id)}
                        onChange={() => toggleGroupJob(j.id)}
                      />
                      {j.name}
                    </label>
                  ))}
                </div>
              )}
            </div>
            <div className="form-grid cols-2">
              <div className="field">
                <label>Send with</label>
                <select
                  value={groupForm.notify?.account_id || ""}
                  onChange={(e) =>
                    setGroupForm({
                      ...groupForm,
                      notify: { ...(groupForm.notify || {}), account_id: e.target.value, enabled: true },
                    })
                  }
                >
                  <option value="">Select a mail account…</option>
                  {(targets.mail_accounts || []).map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} ({a.from_email})
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>When</label>
                <select
                  value={groupForm.notify?.on || "always"}
                  onChange={(e) =>
                    setGroupForm({ ...groupForm, notify: { ...(groupForm.notify || {}), on: e.target.value } })
                  }
                >
                  <option value="always">Every run (success or fail)</option>
                  <option value="failure">Only if any fails</option>
                  <option value="success">Only if all succeed</option>
                </select>
              </div>
              <div className="field">
                <label>Timeout (minutes)</label>
                <input
                  type="number"
                  min={5}
                  max={1440}
                  value={groupForm.timeout_minutes || 120}
                  onChange={(e) => setGroupForm({ ...groupForm, timeout_minutes: Number(e.target.value) || 120 })}
                />
                <span className="field-hint">Send anyway if not all backups finish in time.</span>
              </div>
            </div>
            <NotifyFields
              recipients={recipients}
              notify={groupForm.notify}
              onChange={(notify) => setGroupForm({ ...groupForm, notify: { ...notify, enabled: true } })}
            />
            {(groupForm.notify?.on || "always") !== "failure" && (
              <>
                <p className="muted" style={{ fontSize: "0.85rem", marginBottom: 0 }}>
                  If all backups succeed
                </p>
                <EmailCopyFields
                  outcome="success"
                  notify={groupForm.notify}
                  onChange={(notify) => setGroupForm({ ...groupForm, notify: { ...notify, enabled: true } })}
                />
              </>
            )}
            {(groupForm.notify?.on || "always") !== "success" && (
              <>
                <p className="muted" style={{ fontSize: "0.85rem", marginBottom: 0 }}>
                  If any backup fails
                </p>
                <EmailCopyFields
                  outcome="failure"
                  notify={groupForm.notify}
                  onChange={(notify) => setGroupForm({ ...groupForm, notify: { ...notify, enabled: true } })}
                />
              </>
            )}
            <div className="toolbar">
              <button
                className="primary"
                onClick={saveGroup}
                disabled={
                  groupSaving ||
                  !groupForm.name ||
                  !(groupForm.job_ids || []).length ||
                  !groupForm.notify?.account_id ||
                  !hasRecipients(groupForm.notify)
                }
              >
                {groupForm.id ? "Update group" : "Create group"}
              </button>
              <button onClick={() => setGroupForm(null)} disabled={groupSaving}>
                Cancel
              </button>
            </div>
          </>
        ) : (
          <button
            onClick={() => setGroupForm(emptyGroupForm(jobs, targets.mail_accounts))}
            disabled={jobs.length === 0}
          >
            Create notification group
          </button>
        )}
        {notifyGroups.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Backups</th>
                  <th>Timeout</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {notifyGroups.map((g) => (
                  <tr key={g.id}>
                    <Td label="Group">
                      <strong>{g.name}</strong>
                      {g.notify?.enabled && <span className="badge">email</span>}
                    </Td>
                    <Td label="Backups" className="muted">
                      {(g.job_ids || [])
                        .map((jid) => jobs.find((j) => j.id === jid)?.name || jid)
                        .join(", ") || "—"}
                    </Td>
                    <Td label="Timeout" className="muted">{g.timeout_minutes || 120} min</Td>
                    <TdActions>
                      <div className="row-actions">
                        <button onClick={() => editGroup(g)}>Edit</button>
                        {canDeleteBackup && (
                          <button className="danger" onClick={() => removeGroup(g.id)}>
                            Delete
                          </button>
                        )}
                      </div>
                    </TdActions>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="panel stack" style={{ marginBottom: "1rem" }}>
        <h2 style={{ marginTop: 0 }}>{form.id ? "Edit backup" : "New backup"}</h2>

        <div className="field">
          <label>Name</label>
          <input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Nightly Glam files"
          />
        </div>

        <label className="muted" style={{ fontSize: "0.85rem" }}>
          What to back up
        </label>
        <div className="provider-grid">
          <button
            type="button"
            className={form.kind === "database" ? "provider-card selected" : "provider-card"}
            onClick={() => setKind("database")}
          >
            <strong>Database</strong>
            <span className="muted">Latest dump of a MySQL, PostgreSQL, or SQLite database</span>
          </button>
          <button
            type="button"
            className={form.kind === "files" ? "provider-card selected" : "provider-card"}
            onClick={() => setKind("files")}
          >
            <strong>App files</strong>
            <span className="muted">Sync attachments and uploads (no extra copies of unchanged files)</span>
          </button>
        </div>

        {form.kind === "database" && (
          <>
            <div className="field">
              <label>Database</label>
              <select value={form.source.connection_id || ""} onChange={(e) => setDatabase(e.target.value)}>
                <option value="">Select a database…</option>
                {(targets.databases || []).map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                    {d.database ? ` — ${d.database}` : ""}
                    {d.engine === "sqlite" ? " (SQLite)" : d.engine === "postgres" ? " (PostgreSQL)" : ""}
                  </option>
                ))}
              </select>
            </div>
            {["mysql", "postgres"].includes(selectedDb?.engine) && schemaDbs.length > 0 && (
              <div className="field">
                <label>Which schema</label>
                <select
                  value={form.source.database || selectedDb.database || ""}
                  onChange={(e) =>
                    setForm({ ...form, source: { ...form.source, database: e.target.value } })
                  }
                >
                  {schemaDbs.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </div>
            )}
            {(targets.databases || []).length === 0 && (
              <p className="muted">No databases are configured yet. Add them under Databases.</p>
            )}
          </>
        )}

        {form.kind === "files" && (
          <>
            <div className="field">
              <label>App</label>
              <select value={form.source.app_id || ""} onChange={(e) => setApp(e.target.value)}>
                <option value="">Select an app…</option>
                {(targets.apps || []).map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
            {selectedApp && (
              <>
                <div className="stack">
                  <span className="muted" style={{ fontSize: "0.85rem" }}>
                    Folders to keep in sync
                  </span>
                  {folderOptions.length === 0 ? (
                    <p className="muted">
                      No typical file folders found yet. Browse below to pick something like
                      storage/app/public/attachments.
                    </p>
                  ) : (
                    folderOptions.map((f) => (
                      <label key={f.path} className="check-row">
                        <input
                          type="checkbox"
                          checked={(form.source.paths || []).includes(f.path)}
                          onChange={() => togglePath(f.path)}
                        />
                        <span>
                          {f.label}
                          {f.label !== f.path && (
                            <>
                              <br />
                              <span className="field-hint">{f.path}</span>
                            </>
                          )}
                        </span>
                      </label>
                    ))
                  )}
                </div>
                <div className="field">
                  <label>Pick a nested folder</label>
                  <select
                    value={appBrowsePath}
                    disabled={appDirsLoading}
                    onChange={(e) => {
                      setAppBrowsePath(e.target.value);
                      setCustomAppPath(e.target.value);
                    }}
                  >
                    <option value="">App root</option>
                    {!!appBrowsePath && parentFolder(appBrowsePath) !== "" && (
                      <option value={parentFolder(appBrowsePath)}>↑ {parentFolder(appBrowsePath)}</option>
                    )}
                    {!!appBrowsePath && <option value={appBrowsePath}>{appBrowsePath}/</option>}
                    {appDirs
                      .filter((d) => d.path !== appBrowsePath)
                      .map((d) => (
                        <option key={d.path} value={d.path}>
                          {d.name}/
                        </option>
                      ))}
                  </select>
                  <div className="path-add-row">
                    <input
                      value={customAppPath}
                      onChange={(e) => setCustomAppPath(e.target.value)}
                      onBlur={() => setAppBrowsePath(customAppPath.trim().replace(/^\/+|\/+$/g, ""))}
                      placeholder="storage/app/public/attachments"
                    />
                    <button
                      type="button"
                      onClick={() => addAppFolder(customAppPath || appBrowsePath)}
                      disabled={!(customAppPath || appBrowsePath)}
                    >
                      Add folder
                    </button>
                  </div>
                  <span className={appDirsError ? "error" : "field-hint"}>
                    {appDirsLoading
                      ? "Loading folders…"
                      : appDirsError
                        ? appDirsError
                        : "Files inside the folder are copied into the destination folder. The folder name itself is not created on storage. If you select more than one folder, their files share that destination."}
                  </span>
                </div>
              </>
            )}
          </>
        )}

        <div className="form-grid cols-2">
          <div className="field">
            <label>Repeat</label>
            <select
              value={schedule.repeat}
              onChange={(e) => setForm({ ...form, schedule: { ...schedule, repeat: e.target.value } })}
            >
              <option value="daily">Every day</option>
              <option value="weekdays">Weekdays (Mon–Fri)</option>
              <option value="weekly">Once a week</option>
              <option value="monthly">Once a month</option>
            </select>
          </div>
          <div className="field">
            <label>Time</label>
            <input
              type="time"
              value={schedule.time || "03:00"}
              onChange={(e) => setForm({ ...form, schedule: { ...schedule, time: e.target.value } })}
            />
          </div>
          <div className="field field--full">
            <label>Timezone</label>
            <select
              value={schedule.timezone || targets.timezone?.iana || "UTC"}
              onChange={(e) => setForm({ ...form, schedule: { ...schedule, timezone: e.target.value } })}
            >
              <optgroup label="Common">
                {(targets.timezones || [])
                  .filter((z) => z.popular)
                  .map((z) => (
                    <option key={z.iana} value={z.iana}>
                      {z.label} ({z.offset})
                    </option>
                  ))}
              </optgroup>
              <optgroup label="All timezones">
                {(targets.timezones || [])
                  .filter((z) => !z.popular)
                  .map((z) => (
                    <option key={z.iana} value={z.iana}>
                      {z.label} — {z.iana} ({z.offset})
                    </option>
                  ))}
              </optgroup>
            </select>
          </div>
          {schedule.repeat === "weekly" && (
            <div className="field">
              <label>Day</label>
              <select
                value={schedule.weekday ?? 1}
                onChange={(e) =>
                  setForm({ ...form, schedule: { ...schedule, weekday: Number(e.target.value) } })
                }
              >
                {WEEKDAYS.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </select>
            </div>
          )}
          {schedule.repeat === "monthly" && (
            <div className="field">
              <label>Day of month</label>
              <select
                value={schedule.monthday || 1}
                onChange={(e) =>
                  setForm({ ...form, schedule: { ...schedule, monthday: Number(e.target.value) } })
                }
              >
                {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        <div className="form-grid cols-2">
          <div className="field">
            <label>Save to</label>
            <select
              value={form.destination_remote}
              onChange={(e) => {
                setForm({ ...form, destination_remote: e.target.value });
                setBrowsePath("");
              }}
            >
              <option value="">Select storage…</option>
              {(targets.remotes || []).map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Folder on that storage</label>
            <select
              value={form.destination_folder}
              disabled={!form.destination_remote || foldersLoading}
              onChange={(e) => pickDestinationFolder(e.target.value)}
            >
              <option value="">Root of storage</option>
              {!!form.destination_folder && parentFolder(form.destination_folder) !== "" && (
                <option value={parentFolder(form.destination_folder)}>
                  ↑ {parentFolder(form.destination_folder)}
                </option>
              )}
              {!!form.destination_folder && (
                <option value={form.destination_folder}>{form.destination_folder}/</option>
              )}
              {remoteFolders
                .filter((f) => f.path !== form.destination_folder)
                .map((f) => (
                  <option key={f.path} value={f.path}>
                    {f.name}/
                  </option>
                ))}
            </select>
            <input
              value={form.destination_folder}
              onChange={(e) => setForm({ ...form, destination_folder: e.target.value })}
              onBlur={() => setBrowsePath(form.destination_folder || "")}
              disabled={!form.destination_remote}
            />
            <span className={foldersError ? "error" : "field-hint"}>
              {foldersLoading
                ? "Loading folders…"
                : foldersError
                  ? foldersError
                  : browsePath
                    ? `Folders inside ${browsePath}/ — pick one or type a new path.`
                    : "Pick an existing folder, or type a new path. App files land in this folder (not nested as storage/app/…)."}
            </span>
          </div>
        </div>
        {(targets.remotes || []).length === 0 && (
          <p className="muted">
            Connect Google Drive or another location first in <Link to="/rclone">Rclone → Add storage</Link>.
          </p>
        )}

        <label className="check-row">
          <input
            type="checkbox"
            checked={Boolean(form.notify?.enabled)}
            disabled={inNotifyGroup}
            onChange={(e) =>
              setForm({
                ...form,
                notify: {
                  ...(form.notify || {}),
                  enabled: e.target.checked,
                  account_id: form.notify?.account_id || (targets.mail_accounts || [])[0]?.id || "",
                  on: form.notify?.on || "always",
                },
              })
            }
          />
          Email me when this backup runs
        </label>
        {inNotifyGroup && assignedGroup && (
          <p className="muted">
            Notifications are sent via group <strong>{assignedGroup.name}</strong>. Edit the group above to change
            recipients or timing.
          </p>
        )}
        {form.notify?.enabled && !inNotifyGroup && (
          <>
            <div className="form-grid cols-2">
              <div className="field">
                <label>Send with</label>
                <select
                  value={form.notify?.account_id || ""}
                  onChange={(e) =>
                    setForm({ ...form, notify: { ...(form.notify || {}), account_id: e.target.value } })
                  }
                >
                  <option value="">Select a mail account…</option>
                  {(targets.mail_accounts || []).map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} ({a.from_email})
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label>When</label>
                <select
                  value={form.notify?.on || "always"}
                  onChange={(e) => setForm({ ...form, notify: { ...(form.notify || {}), on: e.target.value } })}
                >
                  <option value="always">Every run (success or fail)</option>
                  <option value="failure">Only if it fails</option>
                  <option value="success">Only if it succeeds</option>
                </select>
              </div>
            </div>
            <NotifyFields
              recipients={recipients}
              notify={form.notify}
              onChange={(notify) => setForm({ ...form, notify })}
            />
            {(form.notify?.on || "always") !== "failure" && (
              <>
                <p className="muted" style={{ fontSize: "0.85rem", marginBottom: 0 }}>
                  If the backup succeeds
                </p>
                <EmailCopyFields
                  outcome="success"
                  notify={form.notify}
                  onChange={(notify) => setForm({ ...form, notify })}
                />
              </>
            )}
            {(form.notify?.on || "always") !== "success" && (
              <>
                <p className="muted" style={{ fontSize: "0.85rem", marginBottom: 0 }}>
                  If the backup fails
                </p>
                <EmailCopyFields
                  outcome="failure"
                  notify={form.notify}
                  onChange={(notify) => setForm({ ...form, notify })}
                />
              </>
            )}
          </>
        )}
        {form.notify?.enabled && !inNotifyGroup && (targets.mail_accounts || []).length === 0 && (
          <p className="muted">
            Add a mailbox first in <Link to="/mail">Mail → Add mail account</Link>. The wizard walks you through Gmail
            and other providers.
          </p>
        )}

        <label className="check-row">
          <input
            type="checkbox"
            checked={form.enabled !== false}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          />
          Run automatically on this schedule
        </label>

        <div className="toolbar">
          <button className="primary" onClick={save} disabled={!canSave || saving || runningId}>
            Save backup
          </button>
          {form.id && (
            <button
              onClick={() => {
                setForm(emptyForm(targets));
                setBrowsePath("");
                setAppBrowsePath("");
                setCustomAppPath("");
              }}
              disabled={saving || runningId}
            >
              Cancel
            </button>
          )}
        </div>
      </div>
      </>
      )}

      <h2>Scheduled backups</h2>
      {jobs.length === 0 ? (
        <p className="muted">{canSaveBackup ? "None yet. Fill in the form above and save." : "None yet."}</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>What</th>
                <th>When</th>
                <th className="col-hide-sm">Where</th>
                <th>Last run</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <Td label="Name">
                    <span className="job-name">
                      <strong>{j.name}</strong>
                      {runningId === j.id && (
                        <span className="spinner" role="status" aria-label="Backup running" />
                      )}
                      {j.enabled === false && <span className="badge warn">paused</span>}
                      {groupByJobId[j.id] && <span className="badge">group</span>}
                      {j.notify?.enabled && !groupByJobId[j.id] && <span className="badge">email</span>}
                    </span>
                  </Td>
                  <Td label="What" className="muted">{describeSource(j, targets)}</Td>
                  <Td label="When" className="muted">{j.when || j.cron || "—"}</Td>
                  <Td label="Where" className="muted col-hide-sm">{j.destination || "—"}</Td>
                  <Td label="Last run">
                    {j.last_run ? (
                      <span className={`badge ${j.last_run.status === "success" ? "ok" : "danger"}`}>
                        {j.last_run.status}
                      </span>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <TdActions>
                    <div className="row-actions">
                      {canRunBackup && (
                        <button className="primary" onClick={() => run(j.id)} disabled={!!runningId}>
                          Run now
                        </button>
                      )}
                      {canSaveBackup && (
                        <button onClick={() => edit(j)}>Edit</button>
                      )}
                      {canDeleteBackup && (
                        <button className="danger" onClick={() => remove(j.id)}>
                          Delete
                        </button>
                      )}
                    </div>
                  </TdActions>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2>History</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Backup</th>
              <th>Status</th>
              <th>Finished</th>
            </tr>
          </thead>
          <tbody>
            {history.map((h) => (
              <tr key={h.id} onClick={() => setLog(h.log || "")} style={{ cursor: "pointer" }}>
                <Td label="Backup">{h.job_name || h.job_id}</Td>
                <Td label="Status">
                  <span className={`badge ${h.status === "success" ? "ok" : "danger"}`}>{h.status}</span>
                </Td>
                <Td label="Finished" className="muted">{h.finished_at}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {log && (
        <>
          <h2>Log</h2>
          <pre className="log">{log}</pre>
        </>
      )}
    </div>
  );
}
