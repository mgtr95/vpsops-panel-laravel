import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { PipelineView, usePipeline } from "../components/PipelineView";
import AppAddons, { emptyAddons } from "../components/AppAddons";
import { canDo, useAuth } from "../auth";

const PHASES = ["github", "repo", "extras", "domain", "deploy"];

export default function AppWizard() {
  const navigate = useNavigate();
  const me = useAuth();
  const canCreate = canDo(me, "apps", "create");
  const canGithub = canDo(me, "apps", "github");
  const [phase, setPhase] = useState("github");
  const [github, setGithub] = useState(null);
  const [repos, setRepos] = useState([]);
  const [search, setSearch] = useState("");
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dns, setDns] = useState(null);
  const [created, setCreated] = useState(null);
  const [proxy, setProxy] = useState(null);
  const [state, setState] = useState({
    owner: "",
    repo: "",
    fullName: "",
    branch: "main",
    name: "",
    domain: "",
    www: true,
    auto_deploy: true,
    run_migrations: true,
    addons: emptyAddons({ database: "mysql" }),
  });

  const pipeline = usePipeline(created?.pipeline?.id);

  async function loadGithub() {
    const status = await api("/github/status");
    setGithub(status);
    if (status.connected) {
      setRepos(await api("/github/repos"));
    }
    return status;
  }

  useEffect(() => {
    let cancelled = false;
    api("/github/status")
      .then(async (status) => {
        if (cancelled) return;
        setGithub(status);
        if (status.connected) {
          const list = await api("/github/repos");
          if (!cancelled) {
            setRepos(list);
            setPhase("repo");
          }
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    const params = new URLSearchParams(window.location.search);
    const ghFlag = params.get("github");
    if (ghFlag === "error") {
      setError("GitHub connect did not finish. Try Connect GitHub again.");
    } else if (ghFlag) {
      loadGithub()
        .then((status) => {
          if (status?.connected) setPhase("repo");
        })
        .catch((e) => setError(e.message));
    }
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api("/apps/site/dns")
      .then((data) => {
        if (!cancelled) setDns(data);
      })
      .catch(() => {});
    api("/proxy")
      .then((data) => {
        if (!cancelled) setProxy(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return repos;
    return repos.filter(
      (r) =>
        r.full_name.toLowerCase().includes(q) ||
        (r.description || "").toLowerCase().includes(q)
    );
  }, [repos, search]);

  const stepIndex = Math.max(0, PHASES.indexOf(phase));

  function update(partial) {
    setState((prev) => ({ ...prev, ...partial }));
    setError("");
  }

  async function pickRepo(repo) {
    setBusy(true);
    setError("");
    try {
      const data = await api(
        `/github/repos/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}/preview?branch=${encodeURIComponent(repo.default_branch || "main")}`
      );
      setPreview(data);
      update({
        owner: repo.owner,
        repo: repo.name,
        fullName: repo.full_name,
        branch: data.default_branch || repo.default_branch || "main",
        name: repo.name,
      });
      if (!data.laravel) {
        setError(data.reason || "This is not a Laravel app.");
        return;
      }
      setPhase("extras");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function changeBranch(branch) {
    update({ branch });
    if (!state.owner || !state.repo) return;
    setBusy(true);
    try {
      const data = await api(
        `/github/repos/${encodeURIComponent(state.owner)}/${encodeURIComponent(state.repo)}/preview?branch=${encodeURIComponent(branch)}`
      );
      setPreview(data);
      if (!data.laravel) setError(data.reason || "This branch is not a Laravel app.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function checkDomain() {
    if (!state.domain.trim()) return;
    setBusy(true);
    setError("");
    try {
      const data = await api(
        `/apps/site/dns?domain=${encodeURIComponent(state.domain.trim())}&www=${state.www}`
      );
      setDns(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function createApp() {
    setBusy(true);
    setError("");
    const domain = state.domain.trim();
    if (domain && !proxy?.ready) {
      setError("Set up website hosting first (Websites), or leave the domain blank and add it later.");
      setBusy(false);
      return;
    }
    try {
      const data = await api("/apps", {
        method: "POST",
        body: {
          owner: state.owner,
          repo: state.repo,
          branch: state.branch,
          name: state.name,
          domain,
          www: state.www,
          auto_deploy: state.auto_deploy,
          run_migrations: state.run_migrations,
          addons: state.addons,
        },
      });
      setCreated(data);
      setPhase("deploy");
      if (domain) {
        try {
          const live = await api(`/apps/${data.app.id}/domain`, {
            method: "POST",
            body: { domain, www: state.www },
          });
          if (!live.ok) {
            setError(live.message || "The app is deploying. HTTPS is waiting until DNS points here.");
          }
        } catch (e) {
          setError(e.message);
        }
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const ip = dns?.public_ip || dns?.instructions?.ip || "YOUR_SERVER_IP";

  if (!canCreate) {
    return (
      <div>
        <div className="page-header">
          <h1>New Laravel app</h1>
          <div className="page-header-actions">
            <button type="button" onClick={() => navigate("/apps")}>
              Back to apps
            </button>
          </div>
        </div>
        <p className="muted">You can view apps but not create new ones.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="page-header">
        <h1>New Laravel app</h1>
        <div className="page-header-actions">
          <button type="button" onClick={() => navigate("/apps")}>
            Cancel
          </button>
        </div>
      </div>
      <p className="muted wizard-progress">
        Step {Math.min(stepIndex + 1, PHASES.length)} of {PHASES.length}
      </p>
      {error && <p className="error">{error}</p>}

      {phase === "github" && (
        <div className="panel stack">
          <p style={{ margin: 0 }}>
            Connect GitHub so this panel can see the repositories you choose — not your whole account.
          </p>
          <p className="muted" style={{ margin: 0 }}>
            GitHub will ask you to create a small app for this server, then pick which repositories to allow.
          </p>
          <div className="row-actions">
            {canGithub ? (
              <button
                className="primary"
                type="button"
                onClick={() => {
                  window.location.href = "/api/github/connect";
                }}
              >
                Connect GitHub
              </button>
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                You do not have permission to connect GitHub.
              </p>
            )}
          </div>
        </div>
      )}

      {phase === "repo" && (
        <>
          <p className="muted">Pick a Laravel repository. Other kinds of projects are not supported yet.</p>
          <div className="field">
            <label>Search</label>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="owner/repo"
              autoFocus
            />
          </div>
          {github?.html_url && (
            <p className="muted">
              Missing a repo?{" "}
              <a href={github.html_url} target="_blank" rel="noreferrer">
                Change which repositories GitHub allows
              </a>
            </p>
          )}
          <div className="stack">
            {filtered.length === 0 && <p className="muted">No repositories match.</p>}
            {filtered.map((repo) => (
              <button
                key={repo.id}
                type="button"
                className="provider-card"
                disabled={busy}
                onClick={() => pickRepo(repo)}
              >
                <strong>{repo.full_name}</strong>
                <span className="muted">{repo.description || (repo.private ? "Private" : "Public")}</span>
              </button>
            ))}
          </div>
        </>
      )}

      {phase === "extras" && (
        <>
          <p className="muted">
            {preview?.laravel
              ? `Laravel detected (PHP ${preview.php}${preview.has_frontend ? ", with a frontend build" : ""}). The panel generates Docker — the repo does not need a Dockerfile.`
              : "Choose what should run with the app."}
          </p>
          <div className="field">
            <label>App name</label>
            <input value={state.name} onChange={(e) => update({ name: e.target.value })} />
          </div>
          <div className="field">
            <label>Branch</label>
            <select value={state.branch} onChange={(e) => changeBranch(e.target.value)}>
              {(preview?.branches || [state.branch]).map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </div>
          <AppAddons
            addons={state.addons}
            onChange={(addons) => update({ addons })}
            runMigrations={state.run_migrations}
            onMigrations={(run_migrations) => update({ run_migrations })}
          />
          <label className="check-row">
            <input
              type="checkbox"
              checked={state.auto_deploy}
              onChange={(e) => update({ auto_deploy: e.target.checked })}
            />
            Deploy automatically when I push to this branch
          </label>
          <div className="row-actions">
            <button type="button" onClick={() => setPhase("repo")} disabled={busy}>
              Back
            </button>
            <button
              className="primary"
              type="button"
              disabled={busy || !preview?.laravel}
              onClick={() => setPhase("domain")}
            >
              Continue
            </button>
          </div>
        </>
      )}

      {phase === "domain" && (
        <>
          {proxy && !proxy.ready && (
            <div className="notice">
              Website hosting is not on yet.{" "}
              <Link to="/sites">Set it up on Websites</Link> first if you want HTTPS now, or leave
              the domain blank and add it later.
            </div>
          )}
          <p className="muted">
            The only thing you do by hand is point the domain at this server. You can skip this and add a domain later.
          </p>
          <div className="field">
            <label>Domain (optional)</label>
            <input
              value={state.domain}
              onChange={(e) => update({ domain: e.target.value })}
              placeholder="mysite.com"
              disabled={proxy && !proxy.ready}
            />
            <span className="field-hint">Without https:// — just the name.</span>
          </div>
          <label className="check-row">
            <input type="checkbox" checked={state.www} onChange={(e) => update({ www: e.target.checked })} disabled={proxy && !proxy.ready} />
            Also use www.{state.domain.trim() || "example.com"}
          </label>
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>DNS instructions</h3>
            <ol className="oauth-steps">
              {(dns?.instructions?.steps || []).map((step, i) => (
                <li key={i}>{step}</li>
              ))}
            </ol>
            <p className="muted">A record target: {ip}</p>
          </div>
          {dns?.records?.length > 0 && (
            <p>
              {dns.ok ? (
                <span className="badge ok">DNS points here</span>
              ) : (
                <span className="badge warn">DNS not pointing here yet</span>
              )}
            </p>
          )}
          <div className="row-actions">
            <button type="button" onClick={() => setPhase("extras")} disabled={busy}>
              Back
            </button>
            <button type="button" disabled={busy || !state.domain.trim() || (proxy && !proxy.ready)} onClick={checkDomain}>
              Check DNS
            </button>
            <button className="primary" type="button" disabled={busy} onClick={createApp}>
              Deploy
            </button>
          </div>
        </>
      )}

      {phase === "deploy" && created && (
        <>
          <p className="muted">
            Building {created.app?.name}. This is the CI/CD pipeline — each push to{" "}
            {created.app?.github?.branch || "the branch"} will run it again unless you turn that off.
          </p>
          <PipelineView pipeline={pipeline || created.pipeline} />
          {(pipeline?.status === "success" || pipeline?.status === "failed") && (
            <div className="row-actions">
              <button className="primary" type="button" onClick={() => navigate(`/apps/${created.app.id}`)}>
                Open app
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
