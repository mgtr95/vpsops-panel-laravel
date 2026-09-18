import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { Td, TdActions } from "../components/Table";
import { canDo } from "../auth";

const PRIVILEGE_LABELS = {
  docker: "Docker",
  apps: "Apps",
  sites: "Websites",
  db: "Databases",
  rclone: "Rclone",
  backups: "Backups",
  mail: "Mail",
  users: "Manage users",
};

function actionKey(moduleId, actionId) {
  return `${moduleId}.${actionId}`;
}

function keysForModule(module) {
  return [module.id, ...(module.actions || []).map((a) => actionKey(module.id, a.id))];
}

function summarizePrivileges(privileges, catalog) {
  if (!privileges?.length) return "none";
  if (catalog?.length) {
    return catalog
      .filter((m) => privileges.includes(m.id))
      .map((m) => {
        const total = (m.actions || []).length;
        if (!total) return m.label;
        const have = m.actions.filter((a) => privileges.includes(actionKey(m.id, a.id))).length;
        return `${m.label} (${have}/${total})`;
      })
      .join(", ") || "none";
  }
  return privileges
    .filter((p) => !p.includes("."))
    .map((p) => PRIVILEGE_LABELS[p] || p)
    .join(", ") || "none";
}

function emptyForm() {
  return {
    username: "",
    password: "",
    email: "",
    privileges: [],
  };
}

export default function SecurityPage() {
  const [me, setMe] = useState(null);
  const [email, setEmail] = useState("");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [setup, setSetup] = useState(null);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const [users, setUsers] = useState([]);
  const [catalog, setCatalog] = useState([]);
  const [usersError, setUsersError] = useState("");
  const [usersMsg, setUsersMsg] = useState("");
  const [form, setForm] = useState(null);
  const [editingId, setEditingId] = useState(null);

  const canManageUsers = Boolean(me?.is_owner || (me?.privileges || []).includes("users"));
  const canCreateUsers = canDo(me, "users", "create");
  const canUpdateUsers = canDo(me, "users", "update");
  const canDeleteUsers = canDo(me, "users", "delete");
  const canReset2fa = canDo(me, "users", "reset_2fa");
  const grantable = useMemo(() => {
    if (!me) return [];
    if (me.is_owner) {
      return catalog.flatMap((m) => keysForModule(m));
    }
    return me.privileges || [];
  }, [me, catalog]);
  const grantableSet = useMemo(() => new Set(grantable), [grantable]);

  async function loadMe() {
    const data = await api("/auth/me");
    setMe(data);
    setEmail(data.email || "");
    return data;
  }

  async function loadUsers() {
    const data = await api("/auth/users");
    setUsers(data.users || []);
    setCatalog(data.catalog || []);
  }

  useEffect(() => {
    loadMe()
      .then((data) => {
        if (data.is_owner || (data.privileges || []).includes("users")) {
          return loadUsers();
        }
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!me || me.totp_enabled) {
      setSetup(null);
      return;
    }
    api("/auth/2fa/setup")
      .then(setSetup)
      .catch((e) => setError(e.message));
  }, [me?.totp_enabled, me?.username]);

  async function saveEmail(e) {
    e.preventDefault();
    setMsg("");
    setError("");
    setBusy("email");
    try {
      const data = await api("/auth/email", { method: "POST", body: { email } });
      setEmail(data.email || "");
      setMsg("Email saved.");
      await loadMe();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function changePassword(e) {
    e.preventDefault();
    setMsg("");
    setError("");
    setBusy("password");
    try {
      await api("/auth/change-password", {
        method: "POST",
        body: { current_password: current, new_password: next },
      });
      setMsg("Password updated.");
      setCurrent("");
      setNext("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function enable2fa(e) {
    e.preventDefault();
    setMsg("");
    setError("");
    setBusy("2fa");
    try {
      await api("/auth/2fa/enable", { method: "POST", body: { code: totpCode } });
      setTotpCode("");
      setMsg("Two-factor authentication is on.");
      await loadMe();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function disable2fa(e) {
    e.preventDefault();
    setMsg("");
    setError("");
    setBusy("2fa");
    try {
      await api("/auth/2fa/disable", { method: "POST", body: { code: totpCode } });
      setTotpCode("");
      setMsg("Two-factor authentication is off.");
      await loadMe();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  function startCreate() {
    setUsersMsg("");
    setUsersError("");
    setEditingId(null);
    setForm(emptyForm());
  }

  function startEdit(user) {
    setUsersMsg("");
    setUsersError("");
    setEditingId(user.id);
    setForm({
      username: user.username,
      password: "",
      email: user.email || "",
      privileges: [...(user.privileges || [])],
    });
  }

  function toggleModule(module) {
    const keys = keysForModule(module);
    const controllable = keys.filter((k) => grantableSet.has(k));
    setForm((prev) => {
      const on = prev.privileges.includes(module.id);
      if (on) {
        const next = prev.privileges.filter((p) => !controllable.includes(p));
        const leftover = next.some((p) => p === module.id || p.startsWith(`${module.id}.`));
        if (leftover && !next.includes(module.id)) next.push(module.id);
        return { ...prev, privileges: next };
      }
      const next = new Set(prev.privileges);
      for (const k of controllable) next.add(k);
      return { ...prev, privileges: [...next] };
    });
  }

  function toggleAction(module, actionId) {
    const key = actionKey(module.id, actionId);
    setForm((prev) => {
      const on = prev.privileges.includes(key);
      const next = new Set(prev.privileges);
      if (on) next.delete(key);
      else {
        next.add(module.id);
        next.add(key);
      }
      return { ...prev, privileges: [...next] };
    });
  }

  async function saveUser(e) {
    e.preventDefault();
    setUsersError("");
    setUsersMsg("");
    setBusy("user");
    try {
      if (editingId) {
        const body = {
          email: form.email,
          privileges: form.privileges,
        };
        if (form.password) body.password = form.password;
        await api(`/auth/users/${encodeURIComponent(editingId)}`, { method: "PATCH", body });
        setUsersMsg("User updated.");
      } else {
        await api("/auth/users", {
          method: "POST",
          body: {
            username: form.username,
            password: form.password,
            email: form.email,
            privileges: form.privileges,
          },
        });
        setUsersMsg("User added.");
      }
      setForm(null);
      setEditingId(null);
      await loadUsers();
    } catch (err) {
      setUsersError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function resetUser2fa(user) {
    if (!confirm(`Clear 2FA for ${user.username}? They will need to enroll again.`)) return;
    setUsersError("");
    setUsersMsg("");
    try {
      await api(`/auth/users/${encodeURIComponent(user.id)}/reset-2fa`, { method: "POST" });
      setUsersMsg(`2FA cleared for ${user.username}.`);
      await loadUsers();
    } catch (err) {
      setUsersError(err.message);
    }
  }

  async function removeUser(user) {
    if (!confirm(`Delete ${user.username}? They will no longer be able to sign in.`)) return;
    setUsersError("");
    setUsersMsg("");
    try {
      await api(`/auth/users/${encodeURIComponent(user.id)}`, { method: "DELETE" });
      setUsersMsg("User deleted.");
      if (editingId === user.id) {
        setForm(null);
        setEditingId(null);
      }
      await loadUsers();
    } catch (err) {
      setUsersError(err.message);
    }
  }

  return (
    <div>
      <div className="page-header">
        <h1>Security</h1>
      </div>
      {error && <div className="error" style={{ marginBottom: "1rem" }}>{error}</div>}
      {msg && <div className="notice notice-ok">{msg}</div>}

      {me && (
        <div className="panel stack" style={{ marginBottom: "1rem" }}>
          <div>
            Signed in as <strong>{me.username}</strong>
            {me.is_owner ? <span className="badge ok" style={{ marginLeft: 8 }}>owner</span> : null}
          </div>
          <div className="muted">
            2FA: {me.totp_enabled ? "enabled" : "not enabled"}
            {me.require_2fa ? " (required on this panel)" : ""} · session ~{me.session_ttl_hours}h ·
            lockout after failed logins
          </div>
        </div>
      )}

      <form className="panel stack" style={{ marginBottom: "1rem" }} onSubmit={saveEmail}>
        <h2 style={{ marginTop: 0 }}>Email</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          Used for password reset. Add a mailbox under Mail first, or reset will have nothing to send
          with.
        </p>
        <div className="field">
          <label>Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            placeholder="you@example.com"
          />
        </div>
        <button className="primary" type="submit" disabled={busy === "email"}>
          {busy === "email" ? "Saving…" : "Save email"}
        </button>
      </form>

      {me && (
        <div className="panel stack" style={{ marginBottom: "1rem" }}>
          <h2 style={{ marginTop: 0 }}>Two-factor authentication</h2>
          {me.totp_enabled ? (
            me.require_2fa ? (
              <p className="muted" style={{ margin: 0 }}>
                2FA is required by this panel and cannot be turned off here.
              </p>
            ) : (
              <form className="stack" onSubmit={disable2fa}>
                <p className="muted" style={{ margin: 0 }}>
                  Enter a code from your authenticator app to turn 2FA off.
                </p>
                <div className="field">
                  <label>Authenticator code</label>
                  <input
                    value={totpCode}
                    onChange={(e) => setTotpCode(e.target.value)}
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    placeholder="123456"
                    minLength={6}
                    required
                  />
                </div>
                <button className="danger" type="submit" disabled={busy === "2fa" || totpCode.length < 6}>
                  {busy === "2fa" ? "Disabling…" : "Disable 2FA"}
                </button>
              </form>
            )
          ) : (
            <form className="stack" onSubmit={enable2fa}>
              <p className="muted" style={{ margin: 0 }}>
                Scan the QR with Google Authenticator, Aegis, 1Password, or similar, then confirm with a
                code.
              </p>
              {setup?.qr_png_base64 && (
                <img
                  src={`data:image/png;base64,${setup.qr_png_base64}`}
                  alt="TOTP QR code"
                  style={{
                    width: 180,
                    height: 180,
                    alignSelf: "flex-start",
                    background: "#fff",
                    padding: 8,
                    borderRadius: 8,
                  }}
                />
              )}
              {setup?.secret && (
                <p className="muted" style={{ fontSize: 13, wordBreak: "break-all", margin: 0 }}>
                  Manual key: <code>{setup.secret}</code>
                </p>
              )}
              <div className="field">
                <label>Confirm with a code</label>
                <input
                  value={totpCode}
                  onChange={(e) => setTotpCode(e.target.value)}
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder="123456"
                  minLength={6}
                  required
                />
              </div>
              <button className="primary" type="submit" disabled={busy === "2fa" || totpCode.length < 6}>
                {busy === "2fa" ? "Enabling…" : "Enable 2FA"}
              </button>
            </form>
          )}
        </div>
      )}

      <form className="panel stack" onSubmit={changePassword}>
        <h2 style={{ marginTop: 0 }}>Change password</h2>
        <div className="field">
          <label>Current password</label>
          <input
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>
        <div className="field">
          <label>New password (min 12 chars)</label>
          <input
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            minLength={12}
            autoComplete="new-password"
            required
          />
        </div>
        <button className="primary" type="submit" disabled={busy === "password"}>
          {busy === "password" ? "Updating…" : "Update password"}
        </button>
      </form>

      {canManageUsers && (
        <div style={{ marginTop: "2rem" }}>
          <div className="page-header">
            <h2 style={{ margin: 0 }}>Users</h2>
            <div className="page-header-actions">
              <button onClick={() => loadUsers().catch((e) => setUsersError(e.message))}>Refresh</button>
              <button className="primary" onClick={startCreate} disabled={!canCreateUsers || (!!form && !editingId)}>
                Add user
              </button>
            </div>
          </div>
          <p className="muted">
            Extra accounts can sign in to this panel. Turn a page on, then pick which actions they may use.
            Docker, apps, and databases are as powerful as root on this VPS.
          </p>
          {usersError && <p className="error">{usersError}</p>}
          {usersMsg && <p className="muted">{usersMsg}</p>}

          {form && (
            <form className="panel stack" style={{ marginBottom: "1rem" }} onSubmit={saveUser}>
              <h3 style={{ marginTop: 0 }}>{editingId ? `Edit ${form.username}` : "New user"}</h3>
              {!editingId && (
                <div className="field">
                  <label>Username</label>
                  <input
                    value={form.username}
                    onChange={(e) => setForm({ ...form, username: e.target.value })}
                    autoComplete="off"
                    required
                  />
                </div>
              )}
              <div className="field">
                <label>Email</label>
                <input
                  type="email"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  autoComplete="off"
                  placeholder="optional"
                />
              </div>
              <div className="field">
                <label>{editingId ? "New password (leave blank to keep)" : "Password (min 12 chars)"}</label>
                <input
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                  autoComplete="new-password"
                  minLength={editingId ? undefined : 12}
                  required={!editingId}
                />
              </div>
              <div>
                <div className="muted" style={{ marginBottom: "0.5rem" }}>Privileges</div>
                <div className="priv-modules">
                  {catalog.map((module) => {
                    const moduleAllowed = grantableSet.has(module.id);
                    const moduleOn = form.privileges.includes(module.id);
                    return (
                      <div key={module.id} className="priv-module">
                        <label className={`check-row${moduleAllowed ? "" : " is-disabled"}`}>
                          <input
                            type="checkbox"
                            checked={moduleOn}
                            disabled={!moduleAllowed}
                            onChange={() => toggleModule(module)}
                          />
                          <strong>{module.label}</strong>
                        </label>
                        {moduleOn && (module.actions || []).length > 0 && (
                          <div className="priv-actions">
                            {module.actions.map((action) => {
                              const key = actionKey(module.id, action.id);
                              const allowed = grantableSet.has(key);
                              return (
                                <label key={key} className={`check-row${allowed ? "" : " is-disabled"}`}>
                                  <input
                                    type="checkbox"
                                    checked={form.privileges.includes(key)}
                                    disabled={!allowed}
                                    onChange={() => toggleAction(module, action.id)}
                                  />
                                  {action.label}
                                </label>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
              <div className="row-actions">
                <button type="button" onClick={() => { setForm(null); setEditingId(null); }}>
                  Cancel
                </button>
                <button className="primary" type="submit" disabled={busy === "user"}>
                  {busy === "user" ? "Saving…" : "Save user"}
                </button>
              </div>
            </form>
          )}

          {users.length === 0 ? (
            <div className="panel">
              <p style={{ margin: 0 }}>No users loaded.</p>
            </div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Email</th>
                    <th>2FA</th>
                    <th>Privileges</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => (
                    <tr key={u.id}>
                      <Td label="User">
                        <strong>{u.username}</strong>
                        {u.is_owner ? (
                          <div className="muted" style={{ fontSize: "0.85rem" }}>owner</div>
                        ) : null}
                      </Td>
                      <Td label="Email" className="muted">{u.email || "—"}</Td>
                      <Td label="2FA">
                        {u.totp_enabled ? <span className="badge ok">on</span> : <span className="muted">off</span>}
                      </Td>
                      <Td label="Privileges" className="muted">
                        {u.is_owner
                          ? "all"
                          : summarizePrivileges(u.privileges, catalog)}
                      </Td>
                      <TdActions>
                        {u.is_owner ? (
                          <span className="muted">—</span>
                        ) : (
                          <div className="row-actions">
                            {canUpdateUsers && (
                              <button type="button" onClick={() => startEdit(u)}>Edit</button>
                            )}
                            {u.totp_enabled && canReset2fa && (
                              <button type="button" onClick={() => resetUser2fa(u)}>Clear 2FA</button>
                            )}
                            {canDeleteUsers && (
                              <button type="button" className="danger" onClick={() => removeUser(u)}>
                                Delete
                              </button>
                            )}
                          </div>
                        )}
                      </TdActions>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
