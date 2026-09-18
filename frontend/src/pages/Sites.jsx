import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Td } from "../components/Table";
import NotifyFields, { hasRecipients } from "../components/NotifyFields";
import { canDo, useAuth } from "../auth";

const CERT_EVENT_OPTIONS = [
  { key: "expiring_soon", label: "Certificate expiring soon (30, 14, 7, 1 days)" },
  { key: "expired", label: "Certificate expired" },
  { key: "renewal_success", label: "Renewal succeeded" },
  { key: "renewal_failed", label: "Renewal failed" },
  { key: "served_mismatch", label: "Disk vs served certificate mismatch" },
];

function emptyCertAlertsForm(mailAccounts) {
  return {
    enabled: false,
    notify: {
      account_id: (mailAccounts || [])[0]?.id || "",
      recipient_ids: [],
      to_emails: [],
    },
    events: {
      expiring_soon: true,
      expired: true,
      renewal_success: true,
      renewal_failed: true,
      served_mismatch: false,
    },
    thresholds_days: [30, 14, 7, 1],
    cooldown_minutes: 1440,
  };
}

function certAlertsFromConfig(config, mailAccounts) {
  const base = emptyCertAlertsForm(mailAccounts);
  if (!config) return base;
  return {
    enabled: Boolean(config.enabled),
    notify: {
      account_id: config.notify?.account_id || base.notify.account_id,
      recipient_ids: config.notify?.recipient_ids || [],
      to_emails: config.notify?.to_emails || [],
    },
    events: { ...base.events, ...(config.events || {}) },
    thresholds_days: config.thresholds_days || base.thresholds_days,
    cooldown_minutes: config.cooldown_minutes ?? 1440,
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

export default function SitesPage() {
  const me = useAuth();
  const canSetup = canDo(me, "sites", "setup");
  const canRenew = canDo(me, "sites", "renew");
  const canReload = canDo(me, "sites", "reload");
  const [proxy, setProxy] = useState(null);
  const [certs, setCerts] = useState([]);
  const [check, setCheck] = useState([]);
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [log, setLog] = useState("");
  const [busy, setBusy] = useState(false);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [alertsForm, setAlertsForm] = useState(emptyCertAlertsForm());
  const [mailAccounts, setMailAccounts] = useState([]);
  const [recipients, setRecipients] = useState([]);
  const [lastCheck, setLastCheck] = useState(null);
  const [alertsMsg, setAlertsMsg] = useState("");
  const [alertsBusy, setAlertsBusy] = useState("");

  async function loadAlerts() {
    try {
      const data = await api("/certs/alerts");
      setMailAccounts(data.mail_accounts || []);
      setRecipients(data.recipients || []);
      setLastCheck(data.last_check || null);
      setAlertsForm(certAlertsFromConfig(data.config, data.mail_accounts));
    } catch (e) {
      setError(e.message);
    }
  }

  async function saveAlerts() {
    setAlertsBusy("save");
    setAlertsMsg("");
    try {
      const data = await api("/certs/alerts", { method: "PUT", body: alertsForm });
      setAlertsForm(certAlertsFromConfig(data.config, mailAccounts));
      setAlertsMsg("Certificate alert settings saved.");
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
      await api("/certs/alerts", { method: "PUT", body: alertsForm });
      await api("/certs/alerts/test", { method: "POST" });
      setAlertsMsg("Test email sent.");
    } catch (e) {
      setError(e.message);
    } finally {
      setAlertsBusy("");
    }
  }

  function toggleCertEvent(key) {
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

  async function load() {
    const status = await api("/proxy");
    setProxy(status);
    if (status.acme_email && !email) setEmail(status.acme_email);
    if (status.ready) {
      try {
        setCerts(await api("/certs"));
        setCheck(await api("/certs/check"));
      } catch {
        setCerts([]);
        setCheck([]);
      }
    }
    return status;
  }

  useEffect(() => {
    load().catch((e) => setError(e.message));
    loadAlerts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function startHosting() {
    setBusy(true);
    setError("");
    setLog("");
    try {
      const result = await api("/proxy/setup", {
        method: "POST",
        body: { acme_email: email.trim() },
      });
      setLog(result.log || (result.steps || []).join("\n"));
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function renew() {
    setBusy(true);
    setError("");
    try {
      const r = await api("/certs/renew", { method: "POST" });
      setLog(r.log || JSON.stringify(r, null, 2));
      if (r.error) setError(r.error);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function reloadSites() {
    setBusy(true);
    setError("");
    try {
      const r = await api("/certs/reload-nginx", { method: "POST" });
      setLog(r.log || JSON.stringify(r, null, 2));
      if (r.error || r.ok === false) setError(r.error || r.log || "Reload failed.");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!proxy && !error) return <p className="muted">Loading…</p>;

  const blocked = proxy?.ports_blocked || {};
  const blockedList = Object.entries(blocked).filter(([, name]) => name);

  return (
    <div>
      <div className="page-header">
        <h1>Websites</h1>
        <div className="page-header-actions">
          {proxy?.ready && (
            <>
              {canRenew && (
                <button className="primary" disabled={busy} onClick={renew} type="button">
                  Renew certificates
                </button>
              )}
              {canReload && (
                <button disabled={busy} onClick={reloadSites} type="button">
                  Reload websites
                </button>
              )}
            </>
          )}
          <button onClick={() => load().catch((e) => setError(e.message))} type="button">
            Refresh
          </button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}

      {proxy && !proxy.ready && (
        <div className="panel" style={{ maxWidth: 640 }}>
          <h2 style={{ marginTop: 0 }}>Start website hosting</h2>
          {canSetup ? (
            <>
          <p className="muted">
            This turns on HTTPS for the apps you deploy. You only do this once. After it is on, each
            app just needs a domain pointed at this server.
          </p>
          <ol className="oauth-steps">
            <li>
              Ports 80 and 443 on this server must be free (nothing else answering the public web).
              {blockedList.length > 0 ? (
                <div className="error" style={{ marginTop: "0.4rem" }}>
                  Busy: {blockedList.map(([port, name]) => `${name} is using port ${port}`).join(". ")}.
                  Open Docker, stop that container, then come back here.
                </div>
              ) : (
                <div className="muted" style={{ marginTop: "0.35rem" }}>
                  Those ports look free.
                </div>
              )}
            </li>
            <li>
              Enter an email for Let’s Encrypt. They send expiry notices here — not used for login.
              <div className="field" style={{ marginTop: "0.6rem" }}>
                <label>Email</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  autoComplete="email"
                />
              </div>
            </li>
            <li>Click the button. The panel starts hosting and keeps certificates renewed.</li>
          </ol>
          <div className="row-actions">
            <button
              className="primary"
              type="button"
              disabled={busy || !email.trim() || blockedList.length > 0}
              onClick={startHosting}
            >
              {busy ? "Starting…" : "Start website hosting"}
            </button>
          </div>
            </>
          ) : (
            <p className="muted">You can view website status but not start hosting.</p>
          )}
        </div>
      )}

      {proxy?.ready && (
        <>
          <div className="notice notice-ok">
            <strong>Website hosting is on.</strong> Certificates renew automatically. Add a domain on
            an app, then Go live — the panel writes the site config and turns on HTTPS.
            {proxy.public_ip ? (
              <div className="muted" style={{ marginTop: "0.35rem" }}>
                Point DNS A records at {proxy.public_ip}.
              </div>
            ) : null}
          </div>
          {!proxy.acme_email && canSetup && (
            <div className="notice">
              <strong>Add an email for new HTTPS certificates.</strong> Let’s Encrypt sends expiry
              notices here. Existing sites keep working.
              <div className="field" style={{ marginTop: "0.6rem" }}>
                <label>Email</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                />
              </div>
              <button
                className="primary"
                type="button"
                disabled={busy || !email.trim()}
                onClick={startHosting}
              >
                Save email
              </button>
            </div>
          )}

          <div className="panel stack" style={{ marginBottom: "1.25rem" }}>
            <div className="page-header" style={{ marginBottom: 0 }}>
              <h2 style={{ margin: 0, fontSize: "1.1rem" }}>
                Certificate alerts
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
                  Enable email alerts for certificates
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
                        {CERT_EVENT_OPTIONS.map(({ key, label }) => (
                          <label key={key} className="check-row">
                            <input
                              type="checkbox"
                              checked={Boolean(alertsForm.events?.[key])}
                              onChange={() => toggleCertEvent(key)}
                            />
                            {label}
                          </label>
                        ))}
                      </div>
                    </div>
                    <p className="muted" style={{ margin: 0, fontSize: "0.85rem" }}>
                      Expiry warnings are sent at 30, 14, 7, and 1 days before expiration. Renewal
                      emails are sent only when a certificate is actually renewed or renewal fails.
                      Checks run every 6 hours.
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

          <h2>Certificates</h2>
          {certs.length === 0 ? (
            <p className="muted">
              None yet. Deploy an app, point a domain here, then use Go live on the app page.
            </p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Domain</th>
                    <th className="col-hide-sm">CN / SANs</th>
                    <th>Issuer</th>
                    <th>Days left</th>
                    <th>Valid until</th>
                  </tr>
                </thead>
                <tbody>
                  {certs.map((c) => (
                    <tr key={c.domain}>
                      <Td label="Domain">
                        <strong>{c.domain}</strong>
                      </Td>
                      <Td label="CN / SANs" className="col-hide-sm">
                        <div>{c.cn}</div>
                        <div className="muted" style={{ fontSize: 12 }}>
                          {(c.sans || []).join(", ")}
                        </div>
                      </Td>
                      <Td label="Issuer" className="muted" style={{ fontSize: 12 }}>
                        {c.issuer}
                      </Td>
                      <Td label="Days left">
                        <span className={`badge ${c.days_left < 14 ? "danger" : c.days_left < 30 ? "warn" : "ok"}`}>
                          {c.days_left}
                        </span>
                      </Td>
                      <Td label="Valid until" className="muted">
                        {c.not_after}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {check.length > 0 && (
            <>
              <h2>Disk vs served</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Domain</th>
                      <th>Match</th>
                      <th>Served enddate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {check.map((c) => (
                      <tr key={c.domain}>
                        <Td label="Domain">{c.domain}</Td>
                        <Td label="Match">
                          {c.match === true ? (
                            <span className="badge ok">OK</span>
                          ) : c.match === false ? (
                            <span className="badge danger">Mismatch</span>
                          ) : (
                            <span className="badge warn">Unknown</span>
                          )}
                        </Td>
                        <Td label="Served enddate" className="muted">
                          {c.served_enddate || "—"}
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}

      {log && (
        <>
          <h2>Activity</h2>
          <pre className="log">{log}</pre>
        </>
      )}

      {proxy?.ready && (
        <p className="muted" style={{ marginTop: "1.5rem" }}>
          Next: <Link to="/apps">open Apps</Link> and add a domain, or deploy a new Laravel app.
        </p>
      )}
    </div>
  );
}
