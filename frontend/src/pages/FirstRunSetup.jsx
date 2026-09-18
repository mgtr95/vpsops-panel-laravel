import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import Logo from "../components/Logo";

export default function FirstRunSetup() {
  const [username, setUsername] = useState("admin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    api("/auth/status")
      .then((s) => {
        if (!s.needs_setup) navigate("/login", { replace: true });
      })
      .catch(() => {});
  }, [navigate]);

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setLoading(true);
    try {
      const res = await api("/auth/setup", {
        method: "POST",
        body: { username, email, password },
      });
      if (res.needs_2fa_setup) navigate("/setup-2fa", { replace: true });
      else navigate("/", { replace: true });
    } catch (err) {
      setError(err.message || "Could not create the account");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-page">
      <form className="login-box stack" onSubmit={onSubmit} style={{ maxWidth: 420 }}>
        <div>
          <Logo size={36} className="logo-hero" />
          <h1>Create the owner account</h1>
          <p className="muted">
            First time on this panel. Choose a username, email, and a password of at least 12
            characters. This account has full access.
          </p>
        </div>
        <div className="field">
          <label>Username</label>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </div>
        <div className="field">
          <label>Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            placeholder="you@example.com"
            required
          />
        </div>
        <div className="field">
          <label>Password (min 12 chars)</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={12}
            autoComplete="new-password"
            required
          />
        </div>
        <div className="field">
          <label>Confirm password</label>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            minLength={12}
            autoComplete="new-password"
            required
          />
        </div>
        {error && <div className="error">{error}</div>}
        <button className="primary" type="submit" disabled={loading}>
          {loading ? "Creating…" : "Create account"}
        </button>
      </form>
    </div>
  );
}
