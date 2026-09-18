import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { Td } from "../components/Table";
import { badgeClass } from "../components/PipelineView";
import AppAddons, { emptyAddons } from "../components/AppAddons";
import { canDo, useAuth } from "../auth";

export default function AppsPage() {
  const me = useAuth();
  const canCreate = canDo(me, "apps", "create");
  const canDeploy = canDo(me, "apps", "deploy");
  const canRestart = canDo(me, "apps", "restart");
  const canGithub = canDo(me, "apps", "github");
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [apps, setApps] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [github, setGithub] = useState(null);
  const [activeJob, setActiveJob] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderPath, setFolderPath] = useState("");
  const [folderAddons, setFolderAddons] = useState(emptyAddons({ database: "mysql" }));
  const [folderMigrations, setFolderMigrations] = useState(true);
  const [folderMigrateData, setFolderMigrateData] = useState(true);

  async function load() {
    try {
      const [list, jobList, gh] = await Promise.all([
        api("/apps"),
        api("/apps/jobs"),
        api("/github/status"),
      ]);
      setApps(list);
      setJobs(jobList);
      setGithub(gh);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    const flag = searchParams.get("github");
    if (!flag) return;
    if (flag === "error") setError("GitHub connect did not finish. Try again.");
    else setNotice("GitHub is connected. You can deploy a Laravel app.");
    load();
  }, [searchParams]);

  useEffect(() => {
    if (!activeJob) return;
    const t = setInterval(async () => {
      try {
        const j = await api(`/apps/jobs/${activeJob}`);
        setJobs((prev) => {
          const rest = prev.filter((x) => x.id !== j.id);
          return [j, ...rest];
        });
        if (j.status === "success" || j.status === "failed") {
          clearInterval(t);
        }
      } catch {
        /* ignore */
      }
    }, 1000);
    return () => clearInterval(t);
  }, [activeJob]);

  async function deploy(appId, opts) {
    setError("");
    try {
      const job = await api(`/apps/${appId}/deploy`, { method: "POST", body: opts });
      if (job.steps) {
        navigate(`/apps/${appId}`);
        return;
      }
      setActiveJob(job.id);
      await load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function deployFolder() {
    setError("");
    try {
      const data = await api("/apps/from-folder", {
        method: "POST",
        body: {
          path: folderPath.trim(),
          addons: folderAddons,
          run_migrations: folderMigrations,
          migrate_data: folderAddons.database === "none" ? false : folderMigrateData,
        },
      });
      navigate(`/apps/${data.app.id}`);
    } catch (e) {
      setError(e.message);
    }
  }

  const current = jobs.find((j) => j.id === activeJob) || jobs[0];
  const laravelApps = apps.filter((a) => a.managed);
  const otherApps = apps.filter((a) => !a.managed);

  return (
    <div>
      <div className="page-header">
        <h1>Apps</h1>
        <div className="page-header-actions">
          {github?.connected
            ? canCreate && (
                <Link to="/apps/new">
                  <button className="primary" type="button">
                    New Laravel app
                  </button>
                </Link>
              )
            : canGithub && (
                <button
                  className="primary"
                  type="button"
                  onClick={() => {
                    window.location.href = "/api/github/connect";
                  }}
                >
                  Connect GitHub
                </button>
              )}
          {canCreate && (
            <button type="button" onClick={() => setFolderOpen((v) => !v)}>
              Deploy folder
            </button>
          )}
          <button type="button" onClick={load}>
            Refresh
          </button>
        </div>
      </div>
      {folderOpen && canCreate && (
        <div className="panel stack" style={{ marginBottom: "1.5rem" }}>
          <p style={{ margin: 0 }}>
            Deploy a Laravel folder already on this VPS. It does not need Docker files — the panel
            template is used.
          </p>
          <div className="field">
            <label>Folder name (under the apps directory)</label>
            <input
              value={folderPath}
              onChange={(e) => setFolderPath(e.target.value)}
              placeholder="my-laravel-app"
            />
          </div>
          <AppAddons
            addons={folderAddons}
            onChange={setFolderAddons}
            runMigrations={folderMigrations}
            onMigrations={setFolderMigrations}
            migrateData={folderMigrateData}
            onMigrateData={setFolderMigrateData}
          />
          <div className="row-actions">
            <button className="primary" type="button" disabled={!folderPath.trim()} onClick={deployFolder}>
              Deploy with panel template
            </button>
          </div>
        </div>
      )}
      {error && <p className="error">{error}</p>}
      {notice && <p className="muted">{notice}</p>}
      {github && (
        <p className="muted">
          {github.connected
            ? `GitHub connected${github.account ? ` as ${github.account}` : ""}.`
            : "Connect GitHub to deploy a Laravel app or link an app already on this server."}{" "}
          {github.html_url && github.connected && (
            <a href={github.html_url} target="_blank" rel="noreferrer">
              Change repo access
            </a>
          )}
        </p>
      )}

      {apps.length === 0 && (
        <div className="panel stack">
          <p style={{ margin: 0 }}>No apps on this server yet.</p>
          <p className="muted" style={{ margin: 0 }}>
            Compose apps already running under your apps folder show up here automatically. New Laravel
            apps should use <strong>New Laravel app</strong> or <strong>Deploy folder</strong> — they
            do not need Docker files in the repo.
          </p>
        </div>
      )}

      {laravelApps.length > 0 && (
        <div className="stack">
          {laravelApps.map((app) => (
            <div className="card" key={app.id}>
              <div className="toolbar toolbar--actions" style={{ marginBottom: 0 }}>
                <div>
                  <Link to={`/apps/${app.id}`}>
                    <strong>{app.name}</strong>
                  </Link>
                  <div className="muted path-break" style={{ fontSize: 13 }}>
                    {app.github?.full_name} · {app.github?.branch}
                    {app.domain ? ` · ${app.domain}` : ""}
                  </div>
                </div>
                {app.running !== undefined && (
                  <span className={`badge ${app.running ? "ok" : "warn"}`}>
                    {app.running ? "running" : "stopped"}
                  </span>
                )}
                {app.latest_pipeline && (
                  <span className={`badge ${badgeClass(app.latest_pipeline.status)}`}>
                    {app.latest_pipeline.status}
                  </span>
                )}
                {canDeploy && (
                  <button
                    className="primary"
                    type="button"
                    onClick={() => deploy(app.id, { pull: true, rebuild: true, restart: false })}
                  >
                    Deploy
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {otherApps.length > 0 && (
        <>
          <h2>On this server</h2>
          <div className="stack">
            {otherApps.map((app) => (
              <div className="card" key={app.id}>
                <div className="toolbar toolbar--actions" style={{ marginBottom: 0 }}>
                  <div>
                    <Link to={`/apps/${app.id}`}>
                      <strong>{app.name}</strong>
                    </Link>
                    <div className="muted path-break" style={{ fontSize: 13 }}>
                      {app.github?.full_name
                      ? `${app.github.full_name} · ${app.github.branch}`
                      : app.kind === "laravel"
                        ? "Laravel — switch to the panel template"
                        : "GitHub not linked"}
                      {app.domain ? ` · ${app.domain}` : ""}
                    </div>
                  </div>
                  <span className={`badge ${app.running ? "ok" : "warn"}`}>
                    {app.running ? "running" : "stopped"}
                  </span>
                  {app.latest_job && (
                    <span className={`badge ${badgeClass(app.latest_job.status)}`}>
                      {app.latest_job.status}
                    </span>
                  )}
                  {app.kind === "laravel" ? (
                    <Link to={`/apps/${app.id}`}>
                      <button className="primary" type="button">
                        Switch to template
                      </button>
                    </Link>
                  ) : (
                    canDeploy && (
                      <button
                        className="primary"
                        type="button"
                        onClick={() => deploy(app.id, { pull: true, rebuild: true, restart: false })}
                      >
                        Pull + Rebuild
                      </button>
                    )
                  )}
                  {app.kind !== "laravel" && canDeploy && (
                    <button type="button" onClick={() => deploy(app.id, { pull: true, rebuild: false, restart: false })}>
                      Git pull only
                    </button>
                  )}
                  {canRestart && (
                    <button type="button" onClick={() => deploy(app.id, { pull: false, rebuild: false, restart: true })}>
                      Restart
                    </button>
                  )}
                  {app.kind !== "laravel" && canDeploy && (
                    <button type="button" onClick={() => deploy(app.id, { pull: false, rebuild: true, restart: false })}>
                      Rebuild only
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {otherApps.length > 0 && (
        <>
          <h2>Job log</h2>
          {current ? (
            <>
              <p className="muted">
                Job {current.id} · {current.app_id} ·{" "}
                <span
                  className={`badge ${current.status === "success" ? "ok" : current.status === "failed" ? "danger" : "warn"}`}
                >
                  {current.status}
                </span>
              </p>
              <pre className="log">{current.log || "(waiting…)"}</pre>
            </>
          ) : (
            <p className="muted">No jobs yet.</p>
          )}

          <h2>Recent jobs</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>App</th>
                  <th>Status</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id} style={{ cursor: "pointer" }} onClick={() => setActiveJob(j.id)}>
                    <Td label="ID">{j.id}</Td>
                    <Td label="App">{j.app_id}</Td>
                    <Td label="Status">{j.status}</Td>
                    <Td label="Started" className="muted">
                      {j.started_at}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
