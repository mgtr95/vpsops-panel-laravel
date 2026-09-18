import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import Logo from "../components/Logo";

export default function Login() {
  const [user, setUser] = useState("admin");
  const [pass, setPass] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState("password"); // password | totp | forgot | forgot-done
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [resetAvailable, setResetAvailable] = useState(false);
  const [forgotId, setForgotId] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    api("/auth/me")
      .then((me) => {
        if (me.needs_2fa_setup) navigate("/setup-2fa", { replace: true });
        else navigate("/", { replace: true });
      })
      .catch(() => {});
    api("/auth/status")
      .then((s) => {
        if (s.needs_setup) navigate("/setup", { replace: true });
        setResetAvailable(Boolean(s.password_reset_available));
      })
      .catch(() => {});
  }, [navigate]);

  async function onPassword(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await api("/auth/login", {
        method: "POST",
        body: { username: user, password: pass },
      });
      if (res.needs_2fa) {
        setStep("totp");
        return;
      }
      if (res.needs_2fa_setup) {
        navigate("/setup-2fa", { replace: true });
        return;
      }
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message || "Login failed");
    } finally {
      setLoading(false);
    }
  }

  async function onTotp(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await api("/auth/verify-2fa", { method: "POST", body: { code } });
      navigate("/", { replace: true });
    } catch (err) {
      setError(err.message || "Invalid code");
    } finally {
      setLoading(false);
    }
  }

  async function onForgot(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await api("/auth/forgot-password", { method: "POST", body: { username: forgotId } });
      setStep("forgot-done");
    } catch (err) {
      setError(err.message || "Could not send reset email");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-page">
      {step === "password" ? (
        <form className="login-box stack" onSubmit={onPassword}>
          <div>
            <Logo size={36} className="logo-hero" />
            <p className="muted">Secure sign-in · password + authenticator</p>
          </div>
          <div className="field">
            <label>Username</label>
            <input value={user} onChange={(e) => setUser(e.target.value)} autoComplete="username" />
          </div>
          <div className="field">
            <label>Password</label>
            <input
              type="password"
              value={pass}
              onChange={(e) => setPass(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          {error && <div className="error">{error}</div>}
          <button className="primary" type="submit" disabled={loading}>
            {loading ? "Checking…" : "Continue"}
          </button>
          {resetAvailable && (
            <button type="button" className="btn-link" onClick={() => { setError(""); setStep("forgot"); }}>
              Forgot password?
            </button>
          )}
        </form>
      ) : step === "totp" ? (
        <form className="login-box stack" onSubmit={onTotp}>
          <div>
            <h1>Authenticator</h1>
            <p className="muted">Enter the 6-digit code from your app</p>
          </div>
          <div className="field">
            <label>Code</label>
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
              placeholder="123456"
            />
          </div>
          {error && <div className="error">{error}</div>}
          <button className="primary" type="submit" disabled={loading || code.length < 6}>
            {loading ? "Verifying…" : "Sign in"}
          </button>
          <button type="button" onClick={() => setStep("password")}>
            Back
          </button>
        </form>
      ) : step === "forgot" ? (
        <form className="login-box stack" onSubmit={onForgot}>
          <div>
            <h1>Reset password</h1>
            <p className="muted">Enter your username or the email on your account.</p>
          </div>
          <div className="field">
            <label>Username or email</label>
            <input
              value={forgotId}
              onChange={(e) => setForgotId(e.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </div>
          {error && <div className="error">{error}</div>}
          <button className="primary" type="submit" disabled={loading || !forgotId.trim()}>
            {loading ? "Sending…" : "Send reset link"}
          </button>
          <button type="button" onClick={() => { setError(""); setStep("password"); }}>
            Back
          </button>
        </form>
      ) : (
        <div className="login-box stack">
          <div>
            <h1>Check your email</h1>
            <p className="muted">
              If that account has an email on file and Mail is set up, a reset link is on its way. It
              expires in 30 minutes.
            </p>
          </div>
          <Link to="/login" onClick={() => setStep("password")}>
            Back to sign in
          </Link>
        </div>
      )}
    </div>
  );
}
