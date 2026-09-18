import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";

export default function Setup2FA() {
  const [setup, setSetup] = useState(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    api("/auth/me")
      .then((me) => {
        if (me.totp_enabled) {
          navigate("/", { replace: true });
          return;
        }
        return api("/auth/2fa/setup");
      })
      .then((data) => {
        if (data) setSetup(data);
      })
      .catch((e) => {
        setError(e.message);
        if (String(e.message).includes("Unauthorized")) navigate("/login");
      });
  }, [navigate]);

  async function enable(e) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      await api("/auth/2fa/enable", { method: "POST", body: { code } });
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  if (!setup && !error) return <p className="muted" style={{ padding: "2rem" }}>Loading 2FA setup…</p>;

  return (
    <div className="login-page">
      <form className="login-box stack" onSubmit={enable} style={{ maxWidth: 420 }}>
        <div>
          <h1>Enable 2FA</h1>
          <p className="muted">
            Scan the QR with Google Authenticator, Aegis, 1Password, etc. This panel requires 2FA
            before you can continue.
          </p>
        </div>
        {setup?.qr_png_base64 && (
          <img
            src={`data:image/png;base64,${setup.qr_png_base64}`}
            alt="TOTP QR code"
            style={{ width: 200, height: 200, alignSelf: "center", background: "#fff", padding: 8, borderRadius: 8 }}
          />
        )}
        {setup?.secret && (
          <p className="muted" style={{ fontSize: 13, wordBreak: "break-all" }}>
            Manual key: <code>{setup.secret}</code>
          </p>
        )}
        <div className="field">
          <label>Confirm with a code</label>
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode="numeric"
            autoComplete="one-time-code"
            placeholder="123456"
          />
        </div>
        {error && <div className="error">{error}</div>}
        <button className="primary" type="submit" disabled={loading || code.length < 6}>
          {loading ? "Enabling…" : "Enable 2FA & continue"}
        </button>
      </form>
    </div>
  );
}
