import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";

export default function SettingsPage() {
  const [access, setAccess] = useState(null);
  const [domain, setDomain] = useState("");
  const [www, setWww] = useState(true);
  const [acmeEmail, setAcmeEmail] = useState("");
  const [dns, setDns] = useState(null);
  const [error, setError] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState("");

  async function load() {
    const data = await api("/access");
    setAccess(data);
    setDomain((prev) => prev || data.effective_domain || data.env_domain || "");
    if (data.source === "settings" && typeof data.www === "boolean") setWww(data.www);
    return data;
  }

  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, []);

  async function checkDns() {
    setBusy("check");
    setError("");
    setMsg("");
    try {
      const data = await api("/access/check", { method: "POST", body: { domain, www } });
      setDns(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function apply() {
    setBusy("apply");
    setError("");
    setMsg("");
    try {
      if (!access?.acme_email_set) {
        if (!acmeEmail.trim()) {
          throw new Error("Enter an email so Let's Encrypt can issue the certificate.");
        }
        await api("/proxy/setup", { method: "POST", body: { acme_email: acmeEmail.trim() } });
      }
      const data = await api("/access/apply", { method: "POST", body: { domain, www } });
      setDns(data.dns || dns);
      setAccess(data);
      if (data.ok && data.primary_url) {
        setMsg(`HTTPS is on. Opening ${data.primary_url}…`);
        window.location.assign(data.primary_url);
        return;
      }
      setMsg(data.message || "DNS is not ready yet.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function removeDomain() {
    if (!confirm("Stop using this domain for the panel? You can still open it by VPS IP on port 9090.")) {
      return;
    }
    setBusy("remove");
    setError("");
    setMsg("");
    try {
      const data = await api("/access/domain", { method: "DELETE" });
      setAccess(data);
      setDns(null);
      setDomain(data.env_domain || "");
      setMsg(data.message || "Panel domain removed.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  if (!access && !error) return <p className="muted">Loading…</p>;
  if (!access) return <p className="error">{error}</p>;

  const ip = dns?.public_ip || dns?.instructions?.ip;
  const hostingReady = Boolean(access?.websites_ready);
  const canApply = hostingReady && (Boolean(access?.acme_email_set) || Boolean(acmeEmail.trim()));

  return (
    <div>
      <div className="page-header">
        <h1>Settings</h1>
      </div>
      {error && <p className="error">{error}</p>}
      {msg && <p className="muted">{msg}</p>}

      <div className="panel stack" style={{ maxWidth: 640, marginBottom: "1rem" }}>
        <h2 style={{ marginTop: 0 }}>How you open this panel</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          When a domain is applied and healthy, opening the panel by VPS IP sends you to{" "}
          <code>https://</code> that hostname. If the domain expires, DNS stops pointing here, or
          the certificate is no longer valid, the IP URL stays as-is and does not redirect. You will
          need to sign in again on whichever address you land on.
        </p>
        {access?.http_ip_url && (
          <p>
            IP: <a href={access.http_ip_url}>{access.http_ip_url}</a>
            {access.redirect_to ? (
              <span className="badge ok" style={{ marginLeft: 8 }}>
                redirects to domain
              </span>
            ) : access.effective_domain ? (
              <span className="badge warn" style={{ marginLeft: 8 }}>
                fallback (domain not healthy)
              </span>
            ) : null}
          </p>
        )}
        {access?.https_ready && access?.primary_url?.startsWith("https://") && (
          <p>
            Domain: <a href={access.primary_url}>{access.primary_url}</a>
            {access.source === "settings" ? (
              <span className="badge ok" style={{ marginLeft: 8 }}>
                Settings
              </span>
            ) : access.source === "env" ? (
              <span className="badge" style={{ marginLeft: 8 }}>
                .env
              </span>
            ) : null}
          </p>
        )}
      </div>

      <div className="panel stack" style={{ maxWidth: 640 }}>
        <h2 style={{ marginTop: 0 }}>Panel domain</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Paste a hostname (for example <code>panel.example.com</code>), point its A record at this
          server, then Apply. Check <strong>Include www</strong> if <code>www</code> also points here
          — both names get a certificate, and www redirects to the main host.
        </p>

        {access?.env_domain && access.source !== "settings" && (
          <div className="notice">
            <code>PANEL_DOMAIN</code> in <code>.env</code> is <strong>{access.env_domain}</strong>.
            Apply below to issue a certificate through website hosting, or leave it as the env-only
            option.
          </div>
        )}
        {access?.env_domain && access.source === "settings" && access.env_domain !== access.effective_domain && (
          <p className="muted">
            Applied domain overrides <code>PANEL_DOMAIN={access.env_domain}</code> in <code>.env</code>{" "}
            until you remove it here.
          </p>
        )}

        {!hostingReady && (
          <div className="notice">
            Website hosting is not on yet.{" "}
            <Link to="/sites">Open Websites</Link> and start hosting before Apply can pull a
            certificate. You can still paste the domain and check DNS.
          </div>
        )}

        <div className="field">
          <label>Domain</label>
          <input
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            placeholder="panel.example.com"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <label className="check-row">
          <input
            type="checkbox"
            checked={www && !domain.trim().toLowerCase().startsWith("www.")}
            disabled={domain.trim().toLowerCase().startsWith("www.")}
            onChange={(e) => setWww(e.target.checked)}
          />
          Include www
        </label>

        {!access?.acme_email_set && (
          <div className="field">
            <label>Email for Let’s Encrypt</label>
            <input
              type="email"
              value={acmeEmail}
              onChange={(e) => setAcmeEmail(e.target.value)}
              placeholder="you@example.com"
              autoComplete="email"
            />
            <p className="field-hint">Used only for certificate notices, not for login.</p>
          </div>
        )}

        {dns?.instructions && (
          <ol className="oauth-steps">
            {dns.instructions.steps.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        )}
        {ip && <p className="muted">Server IP: {ip}</p>}
        {dns && (
          <p>
            {dns.ok ? (
              <span className="badge ok">DNS points here</span>
            ) : (
              <span className="badge warn">Waiting for DNS</span>
            )}
          </p>
        )}
        <div className="row-actions">
          <button type="button" disabled={Boolean(busy) || !domain.trim()} onClick={checkDns}>
            {busy === "check" ? "Checking…" : "Check DNS"}
          </button>
          <button
            className="primary"
            type="button"
            disabled={Boolean(busy) || !domain.trim() || !canApply}
            onClick={apply}
          >
            {busy === "apply" ? "Applying…" : "Apply"}
          </button>
        </div>

        {access?.source === "settings" && (
          <p style={{ marginTop: "1.25rem", marginBottom: 0 }}>
            <button className="danger" type="button" disabled={Boolean(busy)} onClick={removeDomain}>
              {busy === "remove" ? "Removing…" : "Remove domain"}
            </button>
          </p>
        )}
      </div>
    </div>
  );
}
