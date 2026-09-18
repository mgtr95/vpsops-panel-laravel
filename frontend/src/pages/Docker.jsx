import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Td, TdActions } from "../components/Table";
import IssuePanel, { healthBadgeClass, issueSeverityClass } from "../components/IssuePanel";
import NotifyFields, { hasRecipients } from "../components/NotifyFields";
import { canDo, useAuth } from "../auth";

const EVENT_OPTIONS = [
  { key: "crash", label: "Crash or fatal stop" },
  { key: "restart_loop", label: "Restart loop" },
  { key: "unhealthy", label: "Unhealthy health check" },
  { key: "oom", label: "Out of memory (OOM)" },
  { key: "restart_count", label: "Container restarted" },
];

function emptyAlertsForm(mailAccounts) {
  return {
    enabled: false,
    notify: {
      account_id: (mailAccounts || [])[0]?.id || "",
      recipient_ids: [],
      to_emails: [],
    },
    events: {
      crash: true,
      restart_loop: true,
      unhealthy: true,
      oom: true,
      restart_count: true,
    },
    scan_logs: false,
    cooldown_minutes: 15,
  };
}

function alertsFromConfig(config, mailAccounts) {
  const base = emptyAlertsForm(mailAccounts);
  if (!config) return base;
  return {
    enabled: Boolean(config.enabled),
    notify: {
      account_id: config.notify?.account_id || base.notify.account_id,
      recipient_ids: config.notify?.recipient_ids || [],
      to_emails: config.notify?.to_emails || [],
    },
    events: { ...base.events, ...(config.events || {}) },
    scan_logs: Boolean(config.scan_logs),
    cooldown_minutes: config.cooldown_minutes ?? 15,
  };
}

function formatWhen(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function DockerPage() {
  const me = useAuth();
  const canStart = canDo(me, "docker", "start");
  const canStop = canDo(me, "docker", "stop");
  const canRestart = canDo(me, "docker", "restart");
  const canRemove = canDo(me, "docker", "remove");
  const canLogs = canDo(me, "docker", "logs");
  const canInspect = canDo(me, "docker", "inspect");
  const canPrune = canDo(me, "docker", "prune");
  const [containers, setContainers] = useState([]);
  const [images, setImages] = useState([]);
  const [compose, setCompose] = useState([]);
  const [logs, setLogs] = useState("");
  const [inspect, setInspect] = useState(null);
  const [why, setWhy] = useState(null);
  const [tab, setTab] = useState("containers");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [alertsForm, setAlertsForm] = useState(emptyAlertsForm());
  const [mailAccounts, setMailAccounts] = useState([]);
  const [recipients, setRecipients] = useState([]);
  const [lastCheck, setLastCheck] = useState(null);
  const [alertsMsg, setAlertsMsg] = useState("");
  const [alertsBusy, setAlertsBusy] = useState("");

  async function loadAlerts() {
    try {
      const data = await api("/docker/alerts");
      setMailAccounts(data.mail_accounts || []);
      setRecipients(data.recipients || []);
      setLastCheck(data.last_check || null);
      setAlertsForm(alertsFromConfig(data.config, data.mail_accounts));
    } catch (e) {
      setError(e.message);
    }
  }

  async function load() {
    try {
      const [c, i, p] = await Promise.all([
        api("/docker/containers"),
        api("/docker/images"),
        api("/docker/compose"),
      ]);
      setContainers(c);
      setImages(i);
      setCompose(p);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
    loadAlerts();
  }, []);

  async function saveAlerts() {
    setAlertsBusy("save");
    setAlertsMsg("");
    try {
      const data = await api("/docker/alerts", { method: "PUT", body: alertsForm });
      setAlertsForm(alertsFromConfig(data.config, mailAccounts));
      setAlertsMsg("Alert settings saved.");
    } catch (e) {
      setError(e.message);
    } finally {
      setAlertsBusy("");
    }
  }

  async function testAlerts() {
    setAlertsBusy("test");
    setAlertsMsg("");
    try {
      await api("/docker/alerts", { method: "PUT", body: alertsForm });
      await api("/docker/alerts/test", { method: "POST" });
      setAlertsMsg("Test email sent.");
    } catch (e) {
      setError(e.message);
    } finally {
      setAlertsBusy("");
    }
  }

  function toggleEvent(key) {
    setAlertsForm({
      ...alertsForm,
      events: { ...alertsForm.events, [key]: !alertsForm.events[key] },
    });
  }

  const canSaveAlerts =
    !alertsForm.enabled ||
    (alertsForm.notify?.account_id && hasRecipients(alertsForm.notify));

  const canTestAlerts =
    alertsForm.enabled && alertsForm.notify?.account_id && hasRecipients(alertsForm.notify);

  async function act(name, action) {
    setBusy(`${action} ${name}`);
    try {
      await api(`/docker/containers/${encodeURIComponent(name)}/action`, {
        method: "POST",
        body: { action },
      });
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function showLogs(name) {
    setBusy(`logs ${name}`);
    try {
      const data = await api(`/docker/containers/${encodeURIComponent(name)}/logs?tail=300`);
      setLogs(data.logs || "");
      setInspect(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function doInspect(name) {
    setBusy(`inspect ${name}`);
    try {
      setInspect(await api(`/docker/containers/${encodeURIComponent(name)}`));
      setLogs("");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function showWhy(c) {
    setWhy({ name: c.name, issue: c.issue, healthChecks: [] });
    setBusy(`why ${c.name}`);
    try {
      const data = await api(`/docker/containers/${encodeURIComponent(c.name)}`);
      setWhy({
        name: data.name,
        issue: data.issue || c.issue,
        healthChecks: data.health_checks || [],
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function prune(kind) {
    setBusy(`prune ${kind}`);
    try {
      await api(`/docker/prune/${kind}`, { method: "POST" });
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <div className="page-header">
        <h1>Docker</h1>
        <div className="page-header-actions toolbar--tabs">
          <button onClick={() => setTab("containers")} className={tab === "containers" ? "primary" : ""}>
            Containers
          </button>
          <button onClick={() => setTab("images")} className={tab === "images" ? "primary" : ""}>
            Images
          </button>
          <button onClick={() => setTab("compose")} className={tab === "compose" ? "primary" : ""}>
            Compose
          </button>
          <button onClick={load}>Refresh</button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {busy && <p className="muted">{busy}…</p>}

      {tab === "containers" && (
        <>
          <div className="panel stack" style={{ marginBottom: "1.25rem" }}>
            <div className="page-header" style={{ marginBottom: 0 }}>
              <h2 style={{ margin: 0, fontSize: "1.1rem" }}>
                Container alerts
                {alertsForm.enabled && <span className="badge ok">monitoring</span>}
              </h2>
              <button type="button" onClick={() => setAlertsOpen((v) => !v)}>
                {alertsOpen ? "Hide" : "Configure"}
              </button>
            </div>
            {alertsForm.enabled && lastCheck && (
              <p className="muted" style={{ margin: 0, fontSize: "0.85rem" }}>
                Last check: {formatWhen(lastCheck)}
              </p>
            )}
            {alertsOpen && (
              <>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={Boolean(alertsForm.enabled)}
                    onChange={(e) =>
                      setAlertsForm({
                        ...alertsForm,
                        enabled: e.target.checked,
                        notify: {
                          ...alertsForm.notify,
                          account_id:
                            alertsForm.notify?.account_id || (mailAccounts || [])[0]?.id || "",
                        },
                      })
                    }
                  />
                  Enable email alerts for all containers
                </label>
                {alertsForm.enabled && (
                  <>
                    <div className="form-grid cols-2">
                      <div className="field">
                        <label>Mail account</label>
                        <select
                          value={alertsForm.notify?.account_id || ""}
                          onChange={(e) =>
                            setAlertsForm({
                              ...alertsForm,
                              notify: { ...alertsForm.notify, account_id: e.target.value },
                            })
                          }
                        >
                          <option value="">Select a mail account…</option>
                          {(mailAccounts || []).map((a) => (
                            <option key={a.id} value={a.id}>
                              {a.name} ({a.from_email})
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <NotifyFields
                      recipients={recipients}
                      notify={alertsForm.notify}
                      onChange={(notify) => setAlertsForm({ ...alertsForm, notify })}
                    />
                    <div className="field">
                      <label>Alert on</label>
                      <div className="stack">
                        {EVENT_OPTIONS.map(({ key, label }) => (
                          <label key={key} className="check-row">
                            <input
                              type="checkbox"
                              checked={Boolean(alertsForm.events?.[key])}
                              onChange={() => toggleEvent(key)}
                            />
                            {label}
                          </label>
                        ))}
                      </div>
                    </div>
                    <label className="check-row">
                      <input
                        type="checkbox"
                        checked={Boolean(alertsForm.scan_logs)}
                        onChange={(e) =>
                          setAlertsForm({ ...alertsForm, scan_logs: e.target.checked })
                        }
                      />
                      Scan logs for errors (skips frontend asset names and successful HTTP requests)
                    </label>
                    <p className="muted" style={{ margin: 0, fontSize: "0.85rem" }}>
                      Alerts are sent when something changes (not on every poll). The same event is
                      limited to once every {alertsForm.cooldown_minutes || 15} minutes per container.
                      Queue workers that recycle on a timer (--max-time / --max-jobs) and a single
                      brief Docker restart are not emailed.
                    </p>
                    {(mailAccounts || []).length === 0 && (
                      <p className="muted">
                        Add a mailbox first in <Link to="/mail">Mail → Add mail account</Link>.
                      </p>
                    )}
                    <div className="toolbar">
                      <button
                        type="button"
                        className="primary"
                        disabled={!canSaveAlerts || alertsBusy === "save"}
                        onClick={saveAlerts}
                      >
                        Save alerts
                      </button>
                      <button
                        type="button"
                        disabled={!canTestAlerts || alertsBusy === "test"}
                        onClick={testAlerts}
                      >
                        Send test email
                      </button>
                    </div>
                    {alertsMsg && <p className="notice">{alertsMsg}</p>}
                  </>
                )}
                {!alertsForm.enabled && (
                  <div className="toolbar">
                    <button
                      type="button"
                      className="primary"
                      disabled={alertsBusy === "save"}
                      onClick={saveAlerts}
                    >
                      Save
                    </button>
                  </div>
                )}
              </>
            )}
          </div>

          <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Image</th>
                <th>Status</th>
                <th>Health</th>
                <th className="col-hide-sm">Ports</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {containers.map((c) => (
                <tr key={c.id}>
                  <Td label="Name">
                    <strong>{c.name}</strong>
                    <div className="muted" style={{ fontSize: 12 }}>
                      {c.compose_project || ""}
                    </div>
                  </Td>
                  <Td label="Image" className="muted">{c.image}</Td>
                  <Td label="Status">
                    <span
                      className={`badge ${issueSeverityClass(
                        c.issue,
                        c.status === "running" ? "ok" : "warn",
                      )}`}
                    >
                      {c.status}
                    </span>
                    {c.issue && <div className="issue-inline">{c.issue.title}</div>}
                  </Td>
                  <Td label="Health">
                    {c.health ? (
                      <span className={`badge ${healthBadgeClass(c.health)}`}>{c.health}</span>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <Td label="Ports" className="muted col-hide-sm" style={{ fontSize: 12 }}>
                    {(c.ports || [])
                      .map((p) => (p.host ? `${p.host}→${p.container}` : p.container))
                      .join(", ") || "—"}
                  </Td>
                  <TdActions>
                    <div className="row-actions">
                      {canRestart && (
                        <button onClick={() => act(c.name, "restart")}>Restart</button>
                      )}
                      {c.status === "running"
                        ? canStop && (
                            <button onClick={() => act(c.name, "stop")}>Stop</button>
                          )
                        : canStart && (
                            <button onClick={() => act(c.name, "start")}>Start</button>
                          )}
                      {c.issue && canInspect && (
                        <button onClick={() => showWhy(c)}>Why</button>
                      )}
                      {canLogs && (
                        <button onClick={() => showLogs(c.name)}>Logs</button>
                      )}
                      {canInspect && (
                        <button onClick={() => doInspect(c.name)}>Inspect</button>
                      )}
                      {canRemove && (
                        <button className="danger" onClick={() => act(c.name, "remove")}>
                          Remove
                        </button>
                      )}
                    </div>
                  </TdActions>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </>
      )}

      {tab === "images" && (
        <>
          {canPrune && (
            <div className="toolbar">
              <button onClick={() => prune("images")}>Prune dangling</button>
              <button onClick={() => prune("builder")}>Prune builder</button>
            </div>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Tags</th>
                  <th>ID</th>
                  <th>Size</th>
                </tr>
              </thead>
              <tbody>
                {images.map((img) => (
                  <tr key={img.id}>
                    <Td label="Tags">{(img.tags || []).join(", ")}</Td>
                    <Td label="ID" className="muted">{img.id}</Td>
                    <Td label="Size">{img.size ? `${(img.size / 1e6).toFixed(0)} MB` : "—"}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "compose" && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Project</th>
                <th>File</th>
                <th>Directory</th>
              </tr>
            </thead>
            <tbody>
              {compose.map((p) => (
                <tr key={p.path}>
                  <Td label="Project">{p.name}</Td>
                  <Td label="File">{p.file}</Td>
                  <Td label="Directory" className="muted">{p.dir}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {why?.issue && (
        <IssuePanel name={why.name} issue={why.issue} healthChecks={why.healthChecks} />
      )}
      {logs && (
        <>
          <h2>Logs</h2>
          <pre className="log">{logs}</pre>
        </>
      )}
      {inspect && (
        <>
          <h2>Inspect · {inspect.name}</h2>
          <pre className="log">{JSON.stringify(inspect, null, 2)}</pre>
        </>
      )}
    </div>
  );
}
