import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Td, TdActions } from "../components/Table";
import MailWizard from "./MailWizard";
import { canDo, useAuth } from "../auth";

function formatWhen(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function MailPage() {
  const me = useAuth();
  const canSave = canDo(me, "mail", "save");
  const canDelete = canDo(me, "mail", "delete");
  const canTest = canDo(me, "mail", "test");
  const [accounts, setAccounts] = useState([]);
  const [recipients, setRecipients] = useState([]);
  const [providers, setProviders] = useState([]);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [busyId, setBusyId] = useState("");
  const [wizard, setWizard] = useState(null);
  const [recipientForm, setRecipientForm] = useState(null);
  const [recipientSaving, setRecipientSaving] = useState(false);

  async function load() {
    try {
      const [list, presets, recips] = await Promise.all([
        api("/mail/accounts"),
        api("/mail/providers"),
        api("/mail/recipients"),
      ]);
      setAccounts(list);
      setProviders(presets);
      setRecipients(recips);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function remove(id) {
    if (!confirm("Remove this mail account? Backups that used it will stop sending email.")) return;
    setError("");
    try {
      await api(`/mail/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function testSaved(id) {
    setBusyId(id);
    setError("");
    setMsg("");
    try {
      const data = await api(`/mail/accounts/${encodeURIComponent(id)}/test`, { method: "POST" });
      setMsg(`Test email sent to ${data.to}.`);
      await load();
    } catch (e) {
      setError(e.message);
      await load();
    } finally {
      setBusyId("");
    }
  }

  async function saveRecipient() {
    if (!recipientForm?.name || !recipientForm?.email) return;
    setRecipientSaving(true);
    setError("");
    try {
      await api("/mail/recipients", { method: "POST", body: recipientForm });
      setRecipientForm(null);
      setMsg("Recipient saved.");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setRecipientSaving(false);
    }
  }

  async function removeRecipient(id) {
    if (!confirm("Remove this recipient? Backup and group notifications that used it will be updated.")) return;
    setError("");
    try {
      await api(`/mail/recipients/${encodeURIComponent(id)}`, { method: "DELETE" });
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  if (wizard) {
    return (
      <div>
        <div className="page-header">
          <h1>Mail</h1>
        </div>
        <MailWizard
          providers={providers}
          account={wizard.account}
          canTest={canTest}
          onCancel={() => setWizard(null)}
          onDone={() => {
            setWizard(null);
            setMsg("Mail account saved.");
            load();
          }}
        />
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <h1>Mail</h1>
        <div className="page-header-actions">
          <button onClick={load}>Refresh</button>
          {canSave && (
            <button className="primary" onClick={() => setWizard({ account: null })}>
              Add mail account
            </button>
          )}
        </div>
      </div>
      <p className="muted">
        Mail accounts send notifications from your server. Recipients are managed separately and can be reused for
        backups, reports, and other modules.
      </p>
      {error && <p className="error">{error}</p>}
      {msg && <p className="muted">{msg}</p>}

      <h2>Mail accounts</h2>
      <p className="muted">SMTP mailboxes used to send email. Test goes to the From address.</p>
      {accounts.length === 0 ? (
        <div className="panel stack">
          <p style={{ margin: 0 }}>No mail accounts yet.</p>
          <p className="muted" style={{ margin: 0 }}>
            {canSave
              ? "Click Add mail account. The wizard walks through Gmail, Outlook, Yahoo, iCloud, or a custom mail server."
              : "No mailbox is configured yet."}
          </p>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>From</th>
                <th>Last test</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id}>
                  <Td label="Name">
                    <strong>{a.name}</strong>
                    <div className="muted" style={{ fontSize: "0.85rem" }}>
                      {a.provider}
                    </div>
                  </Td>
                  <Td label="From" className="muted">{a.from_email}</Td>
                  <Td label="Last test">
                    {a.last_test?.ok === true ? (
                      <span className="badge ok" title={formatWhen(a.last_test.at)}>
                        ok
                      </span>
                    ) : a.last_test?.ok === false ? (
                      <span className="badge danger" title={formatWhen(a.last_test.at)}>
                        failed
                      </span>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <TdActions>
                    <div className="row-actions">
                      {canTest && (
                        <button onClick={() => testSaved(a.id)} disabled={!!busyId}>
                          {busyId === a.id ? "Sending…" : "Test"}
                        </button>
                      )}
                      {canSave && (
                        <button onClick={() => setWizard({ account: a })}>Edit</button>
                      )}
                      {canDelete && (
                        <button className="danger" onClick={() => remove(a.id)}>
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

      <h2 style={{ marginTop: "1.5rem" }}>Recipients</h2>
      <p className="muted">
        People who receive notifications. Pick them when configuring backups or groups on the{" "}
        <Link to="/backups">Backups</Link> page.
      </p>
      <div className="panel stack">
        {recipientForm ? (
          <>
            <div className="form-grid cols-2">
              <div className="field">
                <label>Name</label>
                <input
                  value={recipientForm.name}
                  onChange={(e) => setRecipientForm({ ...recipientForm, name: e.target.value })}
                  placeholder="Ops team"
                />
              </div>
              <div className="field">
                <label>Email</label>
                <input
                  value={recipientForm.email}
                  onChange={(e) => setRecipientForm({ ...recipientForm, email: e.target.value })}
                  placeholder="ops@example.com"
                />
              </div>
            </div>
            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={recipientForm.include_details !== false}
                  onChange={(e) =>
                    setRecipientForm({ ...recipientForm, include_details: e.target.checked })
                  }
                />
                Include backup details and logs
              </label>
              <span className="field-hint">
                When off, this person only receives the subject, title, and custom message — no backup name, times,
                destination, or log.
              </span>
            </div>
            <div className="toolbar">
              <button
                className="primary"
                onClick={saveRecipient}
                disabled={recipientSaving || !recipientForm.name || !recipientForm.email}
              >
                {recipientForm.id ? "Update recipient" : "Add recipient"}
              </button>
              <button onClick={() => setRecipientForm(null)} disabled={recipientSaving}>
                Cancel
              </button>
            </div>
          </>
        ) : (
          canSave && (
            <button onClick={() => setRecipientForm({ name: "", email: "", include_details: true })}>Add recipient</button>
          )
        )}
        {recipients.length > 0 ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Details</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {recipients.map((r) => (
                  <tr key={r.id}>
                    <Td label="Name">{r.name}</Td>
                    <Td label="Email" className="muted">{r.email}</Td>
                    <Td label="Details">
                      {r.include_details === false ? (
                        <span className="muted">Message only</span>
                      ) : (
                        <span className="muted">Full</span>
                      )}
                    </Td>
                    <TdActions>
                      <div className="row-actions">
                        {canSave && (
                          <button
                            onClick={() =>
                              setRecipientForm({
                                id: r.id,
                                name: r.name,
                                email: r.email,
                                include_details: r.include_details !== false,
                              })
                            }
                          >
                            Edit
                          </button>
                        )}
                        {canDelete && (
                          <button className="danger" onClick={() => removeRecipient(r.id)}>
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
        ) : (
          !recipientForm && <p className="muted" style={{ margin: 0 }}>No recipients yet.</p>
        )}
      </div>
    </div>
  );
}
