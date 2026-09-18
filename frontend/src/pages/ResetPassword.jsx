import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import Logo from "../components/Logo";

export default function ResetPassword() {
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setLoading(true);
    try {
      await api("/auth/reset-password", {
        method: "POST",
        body: { token, new_password: password },
      });
      setDone(true);
    } catch (err) {
      setError(err.message || "Could not reset password");
    } finally {
      setLoading(false);
    }
  }

  if (!token) {
    return (
      <div className="login-page">
        <div className="login-box stack">
          <Logo size={36} className="logo-hero" />
          <h1>Reset link missing</h1>
          <p className="muted">Open the link from your email, or request a new one from the sign-in page.</p>
          <Link to="/login">Back to sign in</Link>
        </div>
      </div>
    );
  }

  if (done) {
    return (
      <div className="login-page">
        <div className="login-box stack">
          <Logo size={36} className="logo-hero" />
          <h1>Password updated</h1>
          <p className="muted">You can sign in with the new password.</p>
          <Link to="/login">Sign in</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="login-page">
      <form className="login-box stack" onSubmit={onSubmit}>
        <div>
          <Logo size={36} className="logo-hero" />
          <h1>New password</h1>
          <p className="muted">Choose a password of at least 12 characters.</p>
        </div>
        <div className="field">
          <label>New password</label>
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
          {loading ? "Saving…" : "Save password"}
        </button>
        <Link to="/login">Back to sign in</Link>
      </form>
    </div>
  );
}
