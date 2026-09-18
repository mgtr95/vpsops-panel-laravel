import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";

const PHASES = ["pick", "name", "guide", "details", "test"];

function providerMeta(providers, id) {
  return (providers || []).find((p) => p.id === id) || null;
}

function payloadFrom(state) {
  return {
    id: state.id || undefined,
    name: state.name.trim(),
    provider: state.provider,
    host: state.host.trim(),
    port: Number(state.port) || 587,
    encryption: state.encryption,
    username: state.fromEmail.trim(),
    password: state.password,
    from_email: state.fromEmail.trim(),
    from_name: state.fromName.trim() || "VPS Backups",
  };
}

export default function MailWizard({ providers, account, onDone, onCancel, canTest = true }) {
  const editing = Boolean(account?.id);
  const [phase, setPhase] = useState(editing ? "details" : "pick");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tested, setTested] = useState(false);
  const [testTo, setTestTo] = useState("");
  const [providerList, setProviderList] = useState(providers || []);
  const [state, setState] = useState(() => ({
    id: account?.id || "",
    provider: account?.provider || "",
    name: account?.name || "",
    host: account?.host || "",
    port: account?.port || 587,
    encryption: account?.encryption || "starttls",
    fromEmail: account?.from_email || "",
    fromName: account?.from_name || "VPS Backups",
    password: "",
  }));

  useEffect(() => {
    if (providers?.length) setProviderList(providers);
  }, [providers]);

  useEffect(() => {
    if (providers?.length) return undefined;
    let cancelled = false;
    api("/mail/providers")
      .then((list) => {
        if (!cancelled) setProviderList(list);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [providers]);

  const meta = useMemo(() => providerMeta(providerList, state.provider), [providerList, state.provider]);
  const stepIndex = Math.max(0, PHASES.indexOf(phase));
  const passwordChanged = Boolean(state.password.trim());
  const needsTest = canTest && (!editing || passwordChanged);
  const canSave = tested || !needsTest;

  function update(partial) {
    setState((prev) => ({ ...prev, ...partial }));
    setTested(false);
    setError("");
  }

  function pickProvider(p) {
    setState((prev) => ({
      ...prev,
      provider: p.id,
      host: p.host || prev.host,
      port: p.port || 587,
      encryption: p.encryption || "starttls",
      name: prev.name || p.title || "",
    }));
    setPhase("name");
    setError("");
    setTested(false);
  }

  async function sendTest() {
    setBusy(true);
    setError("");
    try {
      const body = payloadFrom(state);
      let data;
      if (editing && !passwordChanged) {
        data = await api(`/mail/accounts/${encodeURIComponent(state.id)}/test`, { method: "POST" });
      } else {
        data = await api("/mail/test", { method: "POST", body });
      }
      setTested(true);
      setTestTo(data.to || body.from_email);
    } catch (e) {
      setTested(false);
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!canSave) return;
    setBusy(true);
    setError("");
    try {
      await api("/mail/accounts", {
        method: "POST",
        body: { ...payloadFrom(state), tested: tested || undefined },
      });
      onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const detailsReady =
    state.name.trim() &&
    state.fromEmail.trim() &&
    (editing || state.password.trim()) &&
    (state.provider !== "custom" || state.host.trim());

  return (
    <div className="panel stack">
      <div className="toolbar" style={{ marginBottom: 0 }}>
        <h2 style={{ margin: 0, flex: 1 }}>{editing ? "Edit mail account" : "Add a mail account"}</h2>
        <button onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
      <p className="muted" style={{ margin: 0 }}>
        Step {stepIndex + 1} of {PHASES.length}
        {meta ? ` · ${meta.title}` : ""}
      </p>
      {error && <p className="error">{error}</p>}

      {phase === "pick" && (
        <>
          <p className="muted">
            Choose the inbox that will send backup emails. Most people should pick Gmail, Outlook, Yahoo, or iCloud —
            you will get click-by-click instructions next.
          </p>
          <div className="provider-grid">
            {(providerList || []).map((p) => (
              <button key={p.id} className="provider-card" type="button" onClick={() => pickProvider(p)}>
                <strong>{p.title}</strong>
                <span className="muted">{p.blurb}</span>
              </button>
            ))}
          </div>
        </>
      )}

      {phase === "name" && (
        <>
          <p className="muted">
            Give this mailbox a short name you will recognize when you attach it to a backup (for example “My Gmail”).
          </p>
          <div className="field">
            <label>Name</label>
            <input
              value={state.name}
              onChange={(e) => update({ name: e.target.value })}
              placeholder={meta?.title || "My mailbox"}
              autoFocus
            />
          </div>
          <div className="row-actions">
            <button type="button" onClick={() => setPhase("pick")} disabled={busy}>
              Back
            </button>
            <button
              className="primary"
              type="button"
              disabled={!state.name.trim() || busy}
              onClick={() => setPhase("guide")}
            >
              Continue
            </button>
          </div>
        </>
      )}

      {phase === "guide" && meta && (
        <>
          <h3 style={{ margin: "0.25rem 0" }}>
            {state.provider === "custom" ? "Mail server details" : `How to get a ${meta.password_label || "password"}`}
          </h3>
          <p className="muted" style={{ marginTop: 0 }}>
            {meta.note}
          </p>
          <ol className="oauth-steps">
            {(meta.steps || []).map((step, i) => (
              <li key={i}>
                {step.text}
                {step.href && (
                  <div className="row-actions" style={{ marginTop: 8 }}>
                    <button
                      className="primary"
                      type="button"
                      onClick={() => window.open(step.href, "_blank", "noopener,noreferrer")}
                    >
                      {step.link || "Open this page"}
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ol>
          {state.provider === "custom" && (
            <div className="form-grid cols-2">
              <div className="field">
                <label>SMTP server</label>
                <input
                  value={state.host}
                  onChange={(e) => update({ host: e.target.value })}
                  placeholder="mail.example.com"
                  autoFocus
                />
                <span className="field-hint">From your mail host. Not a website address like https://…</span>
              </div>
              <div className="field">
                <label>Port</label>
                <input
                  type="number"
                  value={state.port}
                  onChange={(e) => update({ port: e.target.value })}
                />
              </div>
              <div className="field">
                <label>Encryption</label>
                <select value={state.encryption} onChange={(e) => update({ encryption: e.target.value })}>
                  <option value="starttls">STARTTLS (usual, port 587)</option>
                  <option value="ssl">SSL (usual, port 465)</option>
                  <option value="none">None (not recommended)</option>
                </select>
              </div>
            </div>
          )}
          <div className="row-actions">
            <button type="button" onClick={() => setPhase("name")} disabled={busy}>
              Back
            </button>
            <button
              className="primary"
              type="button"
              disabled={busy || (state.provider === "custom" && !state.host.trim())}
              onClick={() => setPhase("details")}
            >
              I have the password
            </button>
          </div>
        </>
      )}

      {phase === "details" && (
        <>
          <p className="muted">
            {editing
              ? "Update the addresses if needed. Leave the password blank to keep the one already saved."
              : "Paste the app password from the previous step. The panel stores it encrypted and never shows it again."}
          </p>
          <div className="field">
            <label>Email address (this mailbox)</label>
            <input
              type="email"
              value={state.fromEmail}
              onChange={(e) => {
                update({ fromEmail: e.target.value });
              }}
              placeholder="you@example.com"
              autoFocus
            />
            <span className="field-hint">{meta?.username_hint || "Your full email address"}</span>
          </div>
          <div className="field">
            <label>{meta?.password_label || "Password"}</label>
            <input
              type="password"
              value={state.password}
              onChange={(e) => update({ password: e.target.value })}
              placeholder={editing ? "Leave blank to keep the current password" : "Paste the 16-character app password"}
              autoComplete="new-password"
            />
          </div>
          <div className="field">
            <label>Name shown as the sender</label>
            <input value={state.fromName} onChange={(e) => update({ fromName: e.target.value })} />
          </div>
          <div className="row-actions">
            <button type="button" onClick={() => setPhase("guide")} disabled={busy}>
              Back
            </button>
            <button
              className="primary"
              type="button"
              disabled={!detailsReady || busy}
              onClick={() => setPhase("test")}
            >
              Continue
            </button>
          </div>
        </>
      )}

      {phase === "test" && (
        <>
          <h3 style={{ margin: "0.25rem 0" }}>Send a test email</h3>
          <p className="muted">
            {needsTest
              ? "We send a short test to the From address so you know SMTP works. Check spam if you do not see it within a minute."
              : "Optional — send a test if you want to confirm this mailbox still works. You can save without sending another test."}
          </p>
          {tested && (
            <p>
              <span className="badge ok">Test sent</span>
              <span className="muted"> to {testTo}. You can save this account now.</span>
            </p>
          )}
          <div className="row-actions">
            <button type="button" onClick={() => setPhase("details")} disabled={busy}>
              Back
            </button>
            {canTest && (
              <button type="button" onClick={sendTest} disabled={busy || !detailsReady}>
                {busy ? "Sending…" : "Send test email"}
              </button>
            )}
            <button className="primary" type="button" onClick={save} disabled={busy || !canSave}>
              Save account
            </button>
          </div>
          {needsTest && !tested && (
            <span className="field-hint">The Save button unlocks after a successful test.</span>
          )}
        </>
      )}
    </div>
  );
}
