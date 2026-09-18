import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, checkSession } from "./api/client";
import { AuthContext, canAccess } from "./auth";
import Login from "./pages/Login";
import FirstRunSetup from "./pages/FirstRunSetup";
import Setup2FA from "./pages/Setup2FA";
import ResetPassword from "./pages/ResetPassword";
import Overview from "./pages/Overview";
import DockerPage from "./pages/Docker";
import AppsPage from "./pages/Apps";
import AppWizard from "./pages/AppWizard";
import AppDetail from "./pages/AppDetail";
import SitesPage from "./pages/Sites";
import CertsPage from "./pages/Certs";
import DatabasesPage from "./pages/Databases";
import RclonePage from "./pages/Rclone";
import BackupsPage from "./pages/Backups";
import MailPage from "./pages/Mail";
import SecurityPage from "./pages/Security";
import SettingsPage from "./pages/Settings";
import Logo from "./components/Logo";

const NAV_ITEMS = [
  { to: "/", label: "Overview", end: true },
  { to: "/docker", label: "Docker", priv: "docker" },
  { to: "/apps", label: "Apps", priv: "apps" },
  { to: "/sites", label: "Websites", priv: "sites" },
  { to: "/db", label: "Databases", priv: "db" },
  { to: "/rclone", label: "Rclone", priv: "rclone" },
  { to: "/backups", label: "Backups", priv: "backups" },
  { to: "/mail", label: "Mail", priv: "mail" },
  { to: "/settings", label: "Settings", owner: true },
  { to: "/security", label: "Security" },
];

function Shell({ children, me }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const items = NAV_ITEMS.filter((item) => {
    if (item.owner && !me?.is_owner) return false;
    return canAccess(me, item.priv);
  });

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!menuOpen) {
      document.body.classList.remove("nav-drawer-open");
      return;
    }

    const scrollY = window.scrollY;
    document.body.classList.add("nav-drawer-open");
    document.body.style.position = "fixed";
    document.body.style.top = `-${scrollY}px`;
    document.body.style.left = "0";
    document.body.style.right = "0";
    document.body.style.width = "100%";

    return () => {
      document.body.classList.remove("nav-drawer-open");
      document.body.style.position = "";
      document.body.style.top = "";
      document.body.style.left = "";
      document.body.style.right = "";
      document.body.style.width = "";
      window.scrollTo(0, scrollY);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!menuOpen) return;
    function onKey(e) {
      if (e.key === "Escape") setMenuOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  async function logout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    navigate("/login");
  }

  const navLinkClass = ({ isActive }) => `nav-link${isActive ? " active" : ""}`;
  const drawerLinkClass = ({ isActive }) => `nav-drawer-link${isActive ? " active" : ""}`;

  return (
    <div className={`app-shell${menuOpen ? " nav-drawer-open" : ""}`}>
      <nav className="topnav">
        <NavLink to="/" className="brand" end onClick={() => setMenuOpen(false)}>
          <Logo />
        </NavLink>

        <div className="nav-desktop">
          {items.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={navLinkClass}>
              {item.label}
            </NavLink>
          ))}
          <span className="muted nav-desktop-user">{me.username}</span>
          <button onClick={logout}>Logout</button>
        </div>

        <button
          type="button"
          className="nav-toggle"
          aria-expanded={menuOpen}
          aria-controls="nav-drawer"
          aria-label={menuOpen ? "Close menu" : "Open menu"}
          onClick={() => setMenuOpen((open) => !open)}
        >
          <span className="nav-toggle-icon" aria-hidden="true" />
        </button>
      </nav>

      <div
        className="nav-overlay"
        aria-hidden="true"
        onClick={() => setMenuOpen(false)}
      />

      <div id="nav-drawer" className="nav-drawer" role="dialog" aria-label="Navigation menu">
        <div className="nav-drawer-links">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={drawerLinkClass}
              onClick={() => setMenuOpen(false)}
            >
              {item.label}
            </NavLink>
          ))}
        </div>
        <div className="nav-drawer-footer">
          <span className="muted nav-drawer-user">{me.username}</span>
          <button type="button" onClick={logout}>
            Logout
          </button>
        </div>
      </div>

      <main className="main">{children}</main>
    </div>
  );
}

function Private({ children, priv, owner }) {
  const [state, setState] = useState({ loading: true, me: null, setup: false });

  useEffect(() => {
    Promise.all([checkSession(), api("/auth/status").catch(() => ({}))]).then(([me, status]) => {
      setState({ loading: false, me, setup: Boolean(status.needs_setup) });
    });
  }, []);

  if (state.loading) return <p className="muted" style={{ padding: "2rem" }}>Loading…</p>;
  if (state.setup) return <Navigate to="/setup" replace />;
  if (!state.me) return <Navigate to="/login" replace />;
  if (state.me.needs_2fa_setup) return <Navigate to="/setup-2fa" replace />;
  if (owner && !state.me.is_owner) return <Navigate to="/" replace />;
  if (priv && !canAccess(state.me, priv)) return <Navigate to="/" replace />;
  return (
    <AuthContext.Provider value={state.me}>
      <Shell me={state.me}>{children}</Shell>
    </AuthContext.Provider>
  );
}

function DomainRedirect({ children }) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("stay")) {
      setReady(true);
      return undefined;
    }

    let cancelled = false;
    fetch("/api/access", { credentials: "include" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        const target = data?.redirect_to;
        const domain = (data?.effective_domain || "").toLowerCase();
        const here = window.location.hostname.toLowerCase();
        if (target && domain && here !== domain) {
          const dest = new URL(target);
          dest.pathname = window.location.pathname;
          dest.search = window.location.search;
          dest.hash = window.location.hash;
          window.location.replace(dest.toString());
          return;
        }
        setReady(true);
      })
      .catch(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!ready) return <p className="muted" style={{ padding: "2rem" }}>Loading…</p>;
  return children;
}

function Public({ children }) {
  const [state, setState] = useState({ loading: true, setup: false });

  useEffect(() => {
    api("/auth/status")
      .then((s) => setState({ loading: false, setup: Boolean(s.needs_setup) }))
      .catch(() => setState({ loading: false, setup: false }));
  }, []);

  if (state.loading) return <p className="muted" style={{ padding: "2rem" }}>Loading…</p>;
  if (state.setup) return <Navigate to="/setup" replace />;
  return children;
}

export default function App() {
  return (
    <DomainRedirect>
      <Routes>
        <Route path="/setup" element={<FirstRunSetup />} />
        <Route path="/login" element={<Public><Login /></Public>} />
        <Route path="/setup-2fa" element={<Setup2FA />} />
        <Route path="/reset-password" element={<Public><ResetPassword /></Public>} />
        <Route path="/" element={<Private><Overview /></Private>} />
        <Route path="/docker" element={<Private priv="docker"><DockerPage /></Private>} />
        <Route path="/apps" element={<Private priv="apps"><AppsPage /></Private>} />
        <Route path="/apps/new" element={<Private priv="apps"><AppWizard /></Private>} />
        <Route path="/apps/:appId" element={<Private priv="apps"><AppDetail /></Private>} />
        <Route path="/sites" element={<Private priv="sites"><SitesPage /></Private>} />
        <Route path="/certs" element={<Private priv="sites"><CertsPage /></Private>} />
        <Route path="/db" element={<Private priv="db"><DatabasesPage /></Private>} />
        <Route path="/rclone" element={<Private priv="rclone"><RclonePage /></Private>} />
        <Route path="/backups" element={<Private priv="backups"><BackupsPage /></Private>} />
        <Route path="/mail" element={<Private priv="mail"><MailPage /></Private>} />
        <Route path="/settings" element={<Private owner><SettingsPage /></Private>} />
        <Route path="/security" element={<Private><SecurityPage /></Private>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </DomainRedirect>
  );
}
