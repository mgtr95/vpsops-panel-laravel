import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { PipelineView, badgeClass } from "../components/PipelineView";
import AppAddons, { emptyAddons } from "../components/AppAddons";
import PhpRuntimeFields, { emptyPhpLimits } from "../components/PhpRuntimeFields";
import { canDo, useAuth } from "../auth";

export default function AppDetail() {
  const { appId } = useParams();
  const navigate = useNavigate();
  const me = useAuth();
  const canDeploy = canDo(me, "apps", "deploy");
  const canCreate = canDo(me, "apps", "create");
  const canDomain = canDo(me, "apps", "domain");
  const canDelete = canDo(me, "apps", "delete");
  const canGithub = canDo(me, "apps", "github");
  const [app, setApp] = useState(null);
  const [pipeline, setPipeline] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [domain, setDomain] = useState("");
  const [www, setWww] = useState(true);
  const [dns, setDns] = useState(null);
  const [msg, setMsg] = useState("");
  const [proxy, setProxy] = useState(null);
  const [githubStatus, setGithubStatus] = useState(null);
  const [repos, setRepos] = useState([]);
  const [repoSearch, setRepoSearch] = useState("");
  const [linkOwner, setLinkOwner] = useState("");
  const [linkRepo, setLinkRepo] = useState("");
  const [linkBranch, setLinkBranch] = useState("main");
  const [linkAuto, setLinkAuto] = useState(true);
  const [branches, setBranches] = useState([]);
  const [adoptAddons, setAdoptAddons] = useState(emptyAddons());
  const [adoptMigrations, setAdoptMigrations] = useState(true);
  const [adoptMigrateData, setAdoptMigrateData] = useState(true);
  const [phpLimits, setPhpLimits] = useState(emptyPhpLimits());

  const pipelineApp = Boolean(app?.managed);
  const createdFromGithub = Boolean(app?.source === "github" && !app?.discovered);
  const gh = app?.github || {};
  const linked = Boolean(gh.full_name);
  const canAdopt = Boolean(app?.can_adopt);

  async function load() {
    const data = await api(`/apps/${appId}`);
    setApp(data);
    setDomain(data.domain || "");
    setWww(data.www !== false);
    setDns(data.dns || null);
    const seed = data.addons || data.suggested_addons;
    if (seed) setAdoptAddons(emptyAddons(seed));
    if (data.run_migrations !== undefined) setAdoptMigrations(Boolean(data.run_migrations));
    if (data.managed) setPhpLimits(emptyPhpLimits(data.php || {}));
    const latestPipe = data.latest_pipeline || (data.pipelines || [])[0];
    if (latestPipe?.id) {
      try {
        setPipeline(await api(`/apps/pipelines/${latestPipe.id}`));
      } catch {
        setPipeline(latestPipe);
      }
    }
    const latestJob = data.latest_job || (data.jobs || [])[0];
    if (latestJob?.id) {
      try {
        setJob(await api(`/apps/jobs/${latestJob.id}`));
      } catch {
        setJob(latestJob);
      }
    }
    return data;
  }

  useEffect(() => {
    let cancelled = false;
    load().catch((e) => {
      if (!cancelled) setError(e.message);
    });
    api("/proxy")
      .then((data) => {
        if (!cancelled) setProxy(data);
      })
      .catch(() => {});
    api("/github/status")
      .then(async (status) => {
        if (cancelled) return;
        setGithubStatus(status);
        if (status.connected) {
          try {
            const list = await api("/github/repos");
            if (!cancelled) setRepos(list);
          } catch {
            /* ignore */
          }
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [appId]);

  useEffect(() => {
    if (!pipeline?.id) return undefined;
    if (pipeline.status === "success" || pipeline.status === "failed") return undefined;
    const t = setInterval(async () => {
      try {
        const p = await api(`/apps/pipelines/${pipeline.id}`);
        setPipeline(p);
        if (p.status === "success" || p.status === "failed") clearInterval(t);
      } catch {
        /* ignore */
      }
    }, 1200);
    return () => clearInterval(t);
  }, [pipeline?.id, pipeline?.status]);

  useEffect(() => {
    if (!job?.id) return undefined;
    if (job.status === "success" || job.status === "failed") return undefined;
    const t = setInterval(async () => {
      try {
        const j = await api(`/apps/jobs/${job.id}`);
        setJob(j);
        if (j.status === "success" || j.status === "failed") clearInterval(t);
      } catch {
        /* ignore */
      }
    }, 1000);
    return () => clearInterval(t);
  }, [job?.id, job?.status]);

  async function loadBranches(owner, repo, keepBranch) {
    if (!owner || !repo) {
      setBranches([]);
      return;
    }
    try {
      const preview = await api(
        `/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/preview`
      );
      const list = preview?.branches?.length ? preview.branches : [keepBranch || "main"];
      setBranches(list);
      if (keepBranch && list.includes(keepBranch)) setLinkBranch(keepBranch);
      else if (!list.includes(linkBranch)) setLinkBranch(preview?.default_branch || list[0]);
    } catch {
      setBranches(keepBranch ? [keepBranch] : ["main"]);
    }
  }

  useEffect(() => {
    if (!linked) return undefined;
    loadBranches(gh.owner, gh.repo, gh.branch);
    return undefined;
  }, [gh.owner, gh.repo, gh.branch, linked]);

  const filteredRepos = useMemo(() => {
    const q = repoSearch.trim().toLowerCase();
    if (!q) return repos;
    return repos.filter(
      (r) =>
        r.full_name.toLowerCase().includes(q) ||
        (r.description || "").toLowerCase().includes(q)
    );
  }, [repos, repoSearch]);

  async function deploy() {
    setBusy(true);
    setError("");
    try {
      const p = await api(`/apps/${appId}/deploy`, { method: "POST", body: {} });
      if (p.steps) {
        setPipeline(p);
        setJob(null);
      } else {
        setJob(p);
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleAuto(auto_deploy) {
    setBusy(true);
    setError("");
    try {
      const data = await api(`/apps/${appId}`, { method: "PATCH", body: { auto_deploy } });
      setApp((prev) => ({ ...prev, ...data }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function changeBranch(branch) {
    setBusy(true);
    setError("");
    try {
      const data = await api(`/apps/${appId}`, { method: "PATCH", body: { branch } });
      setApp((prev) => ({ ...prev, ...data }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function linkGithub() {
    setBusy(true);
    setError("");
    try {
      await api(`/apps/${appId}/github`, {
        method: "POST",
        body: {
          owner: linkOwner,
          repo: linkRepo,
          branch: linkBranch,
          auto_deploy: linkAuto,
        },
      });
      await load();
      setMsg("GitHub linked. Laravel apps now deploy with the panel template (repo Docker is ignored).");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function adoptTemplate() {
    setBusy(true);
    setError("");
    setMsg("");
    try {
      const data = await api(`/apps/${appId}/adopt`, {
        method: "POST",
        body: {
          addons: adoptAddons,
          run_migrations: adoptMigrations,
          migrate_data: adoptAddons.database === "none" ? false : adoptMigrateData,
        },
      });
      if (data.pipeline?.id) setPipeline(data.pipeline);
      setMsg("Switching this app to the panel template. Production Docker from the repo is ignored.");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function unlinkGithub() {
    if (!confirm("Stop deploying this app from GitHub? The app stays on the server.")) return;
    setBusy(true);
    setError("");
    try {
      await api(`/apps/${appId}/github`, { method: "DELETE" });
      await load();
      setLinkOwner("");
      setLinkRepo("");
      setLinkBranch("main");
      setBranches([]);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function pickRepo(repo) {
    setLinkOwner(repo.owner);
    setLinkRepo(repo.name);
    setLinkBranch(repo.default_branch || "main");
    loadBranches(repo.owner, repo.name, repo.default_branch || "main");
  }

  async function checkDns() {
    setBusy(true);
    setError("");
    try {
      const data = await api(`/apps/${appId}/domain/check`, { method: "POST", body: { domain, www } });
      setDns(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function goLive() {
    setBusy(true);
    setError("");
    setMsg("");
    try {
      const data = await api(`/apps/${appId}/domain`, { method: "POST", body: { domain, www } });
      setDns(data.dns || dns);
      if (data.ok) setMsg("HTTPS is on. The domain now points at this app.");
      else setMsg(data.message || "DNS is not ready yet.");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function removeApp() {
    if (!confirm("Stop this app and remove it from the panel? Containers will be stopped.")) return;
    setBusy(true);
    try {
      await api(`/apps/${appId}?delete_files=true`, { method: "DELETE" });
      navigate("/apps");
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

  async function savePhpLimits() {
    setBusy(true);
    setError("");
    setMsg("");
    try {
      const payload = emptyPhpLimits(phpLimits);
      const data = await api(`/apps/${appId}/php`, { method: "PUT", body: payload });
      setApp(data);
      setPhpLimits(emptyPhpLimits(data.php || payload));
      if (data.nginx_reload && data.nginx_reload.ok === false) {
        setMsg(
          `PHP limits saved and containers restarted. Nginx update: ${
            data.nginx_reload.error || "failed"
          }`
        );
      } else {
        setMsg("PHP limits saved. App containers were restarted (no image rebuild).");
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!app && error) return <p className="error">{error}</p>;
  if (!app) return <p className="muted">Loading…</p>;

  const addons = app.addons || {};
  const hasAddons = Boolean(
    addons.database || addons.redis || addons.queue || addons.scheduler || addons.mailpit
  );
  const ip = dns?.public_ip || dns?.instructions?.ip;
  const latestStatus = pipelineApp ? pipeline?.status : job?.status;
  const branchOptions = branches.length ? branches : gh.branch ? [gh.branch] : [linkBranch];

  return (
    <div>
      <div className="page-header">
        <h1>{app.name}</h1>
        <div className="page-header-actions">
          <Link to="/apps">
            <button type="button">All apps</button>
          </Link>
          {canDeploy && (
            <button className="primary" type="button" disabled={busy} onClick={deploy}>
              Deploy
            </button>
          )}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {msg && <p className="muted">{msg}</p>}

      <div className="grid">
        <div className="card">
          <div className="stat-label">GitHub</div>
          <div>{linked ? `${gh.full_name} · ${gh.branch}` : "Not linked"}</div>
        </div>
        <div className="card">
          <div className="stat-label">Domain</div>
          <div>{app.domain || "Not set"}</div>
        </div>
        <div className="card">
          <div className="stat-label">Latest deploy</div>
          <div>
            <span className={`badge ${badgeClass(latestStatus)}`}>{latestStatus || "—"}</span>
          </div>
        </div>
      </div>

      <h2>GitHub</h2>
      {linked ? (
        <>
          <p className="muted">
            {gh.full_name} on branch {gh.branch}. The same repository can be linked to another app on a
            different branch.
          </p>
          {canDeploy && (
            <div className="field">
              <label>Branch</label>
              <select
                value={gh.branch || ""}
                disabled={busy}
                onChange={(e) => changeBranch(e.target.value)}
              >
                {branchOptions.map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </select>
            </div>
          )}
          {canDeploy && (
            <label className="check-row" style={{ marginTop: "1rem" }}>
              <input
                type="checkbox"
                checked={gh.auto_deploy !== false}
                disabled={busy}
                onChange={(e) => toggleAuto(e.target.checked)}
              />
              Deploy automatically when I push to {gh.branch || "this branch"}
            </label>
          )}
          {!createdFromGithub && canGithub && (
            <p style={{ marginTop: "1rem" }}>
              <button type="button" disabled={busy} onClick={unlinkGithub}>
                Unlink GitHub
              </button>
            </p>
          )}
        </>
      ) : createdFromGithub ? (
        <p className="muted">This app was created from GitHub in the panel.</p>
      ) : (
        <>
          <p className="muted">
            {pipelineApp
              ? "Link a GitHub repository so deploys pull new code. Production Docker stays the panel template."
              : "Link a Laravel repository. The next deploy uses the panel Docker template, not this folder’s compose files."}
          </p>
          {!githubStatus?.connected ? (
            canGithub ? (
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
              <p className="muted">GitHub is not connected on this panel.</p>
            )
          ) : (
            <>
              {githubStatus.html_url && (
                <p className="muted">
                  Missing a repo?{" "}
                  <a href={githubStatus.html_url} target="_blank" rel="noreferrer">
                    Change which repositories GitHub allows
                  </a>
                </p>
              )}
              {!linkOwner ? (
                <>
                  <div className="field">
                    <label>Search</label>
                    <input
                      value={repoSearch}
                      onChange={(e) => setRepoSearch(e.target.value)}
                      placeholder="owner/repo"
                    />
                  </div>
                  <div className="stack">
                    {filteredRepos.length === 0 && <p className="muted">No repositories match.</p>}
                    {filteredRepos.map((repo) => (
                      <button
                        key={repo.id}
                        type="button"
                        className="provider-card"
                        disabled={busy}
                        onClick={() => pickRepo(repo)}
                      >
                        <strong>{repo.full_name}</strong>
                        <span className="muted">
                          {repo.description || (repo.private ? "Private" : "Public")}
                        </span>
                      </button>
                    ))}
                  </div>
                </>
              ) : (
                <>
                  <p>
                    <strong>
                      {linkOwner}/{linkRepo}
                    </strong>
                  </p>
                  <div className="field">
                    <label>Branch</label>
                    <select value={linkBranch} onChange={(e) => setLinkBranch(e.target.value)} disabled={busy}>
                      {branchOptions.map((b) => (
                        <option key={b} value={b}>
                          {b}
                        </option>
                      ))}
                    </select>
                  </div>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={linkAuto}
                      onChange={(e) => setLinkAuto(e.target.checked)}
                    />
                    Deploy automatically when I push to this branch
                  </label>
                  <div className="row-actions">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setLinkOwner("");
                        setLinkRepo("");
                        setBranches([]);
                      }}
                    >
                      Back
                    </button>
                    {canGithub && (
                      <button className="primary" type="button" disabled={busy} onClick={linkGithub}>
                        Link GitHub
                      </button>
                    )}
                  </div>
                </>
              )}
            </>
          )}
        </>
      )}

      {canAdopt && canCreate && (
        <>
          <h2>Panel template</h2>
          {pipeline?.status === "failed" && (
            <p className="notice">
              The last template switch did not finish. You can run it again. The local database
              backup from the previous attempt is kept under <code>.panel-backups/</code>.
            </p>
          )}
          <p className="muted">
            This app still uses whatever Docker is in the folder. Switch it to the same FrankenPHP
            template as new Laravel apps (nginx + certbot stay in front). A local database backup is
            always written first; storage files stay on disk.
          </p>
          <AppAddons
            addons={adoptAddons}
            onChange={setAdoptAddons}
            runMigrations={adoptMigrations}
            onMigrations={setAdoptMigrations}
            migrateData={adoptMigrateData}
            onMigrateData={setAdoptMigrateData}
            sourceDatabase={app.source_database}
          />
          <div className="row-actions">
            <button className="primary" type="button" disabled={busy} onClick={adoptTemplate}>
              {pipeline?.status === "failed" ? "Retry switch to panel template" : "Switch to panel template"}
            </button>
          </div>
        </>
      )}

      {hasAddons && (
        <>
          <h2>Resources</h2>
          <p className="muted">
            Database: {addons.database || "none"}
            {addons.redis ? " · Redis" : ""}
            {addons.queue ? " · Queue workers" : ""}
            {addons.scheduler ? " · Scheduler" : ""}
            {addons.mailpit ? " · Mailpit" : ""}
            {(addons.packages || []).length ? ` · extra packages` : ""}
          </p>
        </>
      )}

      {pipelineApp && (
        <>
          <h2>PHP &amp; uploads</h2>
          <p className="muted">
            Upload and runtime limits for this app. Saving rewrites the panel PHP config and
            restarts app containers — no full image rebuild.
          </p>
          <PhpRuntimeFields value={phpLimits} onChange={setPhpLimits} disabled={busy || !canDeploy} />
          {canDeploy && (
            <div className="row-actions">
              <button className="primary" type="button" disabled={busy} onClick={savePhpLimits}>
                Save PHP limits
              </button>
            </div>
          )}
        </>
      )}

      {canDomain && (
        <>
      <h2>Domain</h2>
      {proxy && !proxy.ready ? (
        <div className="notice">
          Website hosting is not on yet. <Link to="/sites">Set it up on Websites</Link> before you
          can publish this app on a domain.
        </div>
      ) : (
        <>
      <p className="muted">Point an A record at this server, then go live. The panel issues HTTPS for you.</p>
      <div className="field">
        <label>Domain</label>
        <input value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="mysite.com" />
      </div>
      <label className="check-row">
        <input type="checkbox" checked={www} onChange={(e) => setWww(e.target.checked)} />
        Include www
      </label>
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
        <button type="button" disabled={busy || !domain.trim()} onClick={checkDns}>
          Check DNS
        </button>
        <button className="primary" type="button" disabled={busy || !domain.trim()} onClick={goLive}>
          Go live
        </button>
      </div>
        </>
      )}
        </>
      )}

      <h2>Deployments</h2>
      {pipelineApp ? (
        <>
          <PipelineView pipeline={pipeline} />
          {(app.pipelines || []).length > 1 && (
            <div className="table-wrap" style={{ marginTop: "1rem" }}>
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Status</th>
                    <th>Commit</th>
                    <th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {app.pipelines.map((p) => (
                    <tr
                      key={p.id}
                      style={{ cursor: "pointer" }}
                      onClick={async () => {
                        try {
                          setPipeline(await api(`/apps/pipelines/${p.id}`));
                        } catch (e) {
                          setError(e.message);
                        }
                      }}
                    >
                      <td>{p.id}</td>
                      <td>
                        <span className={`badge ${badgeClass(p.status)}`}>{p.status}</span>
                      </td>
                      <td>{(p.commit && p.commit.sha && String(p.commit.sha).slice(0, 7)) || "—"}</td>
                      <td className="muted">{p.started_at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : job ? (
        <>
          <p className="muted">
            Job {job.id}
            {job.sha ? ` · ${String(job.sha).slice(0, 7)}` : ""} ·{" "}
            <span className={`badge ${badgeClass(job.status)}`}>{job.status}</span>
          </p>
          <pre className="log">{job.log || "(waiting…)"}</pre>
        </>
      ) : (
        <p className="muted">No deployment yet.</p>
      )}

      {pipelineApp && canDelete && (
        <p style={{ marginTop: "2rem" }}>
          <button className="danger" type="button" disabled={busy} onClick={removeApp}>
            Remove app
          </button>
        </p>
      )}
    </div>
  );
}
