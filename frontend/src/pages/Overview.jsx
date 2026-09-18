import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Td } from "../components/Table";
import { IssueBlurb, healthBadgeClass, issueSeverityClass } from "../components/IssuePanel";

function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

function fmtUptime(sec) {
  if (sec == null) return "—";
  const s = Math.max(0, Math.floor(sec));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtPct(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return `${Number(n).toFixed(1)}%`;
}

function usageTone(pct) {
  if (pct == null) return "";
  if (pct >= 90) return "danger";
  if (pct >= 75) return "warn";
  return "ok";
}

function Meter({ value }) {
  const pct = Math.max(0, Math.min(100, Number(value) || 0));
  const tone = usageTone(value);
  return (
    <div
      className="meter"
      role="meter"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div className={`meter-fill ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function StatCard({ value, label, percent }) {
  return (
    <div className="card">
      <div className={`stat-value ${usageTone(percent)}`}>{value}</div>
      <div className="stat-label">{label}</div>
      {percent != null && <Meter value={percent} />}
    </div>
  );
}

function loadHint(load1, cores) {
  if (load1 == null || !cores) return "";
  const ratio = load1 / cores;
  if (ratio >= 1) return "Busy — processes are waiting for CPU.";
  if (ratio >= 0.7) return "Getting busy.";
  return "Headroom available.";
}

export default function Overview() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      setData(await api("/overview"));
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

  if (error && !data) return <div className="error">{error}</div>;
  if (!data) return <p className="muted">Loading…</p>;

  const disk = data.disk || {};
  const can = data.can || { docker: true, sites: true, backups: true };
  const sys = data.system || {};
  const cpu = sys.cpu || {};
  const mem = sys.memory || {};
  const host = sys.host || {};
  const loadAvg = cpu.load || [];

  return (
    <div>
      <div className="page-header">
        <h1>Overview</h1>
        <div className="page-header-actions">
          {host.hostname && <span className="muted">{host.hostname}</span>}
          <button onClick={load}>Refresh</button>
        </div>
      </div>

      {error && <p className="error">{error}</p>}

      {can.sites && data.proxy && !data.proxy.ready && (
        <div className="notice">
          <strong>Before you publish a website, set up hosting.</strong>{" "}
          <Link to="/sites">Open Websites</Link> and start it — one email, one button. After that,
          each app only needs a domain pointed at this server.
        </div>
      )}

      <div className="grid">
        <StatCard
          value={fmtPct(cpu.percent)}
          label={cpu.count ? `CPU (${cpu.count} ${cpu.count === 1 ? "core" : "cores"})` : "CPU"}
          percent={cpu.percent}
        />
        <StatCard
          value={fmtPct(mem.percent)}
          label={`RAM (${fmtBytes(mem.used)} / ${fmtBytes(mem.total)})`}
          percent={mem.percent}
        />
        <StatCard
          value={disk.percent != null ? `${disk.percent}%` : "—"}
          label={`Disk (${fmtBytes(disk.used)} / ${fmtBytes(disk.total)})`}
          percent={disk.percent}
        />
        {can.docker && (
          <div className="card">
            <div className="stat-value">
              {data.containers_running}/{data.containers_total}
            </div>
            <div className="stat-label">Containers running</div>
          </div>
        )}
        {can.sites && (
          <div className="card">
            <div className="stat-value">{(data.certs_expiring || []).length}</div>
            <div className="stat-label">Certs ≤30 days</div>
          </div>
        )}
        {can.backups && (
          <div className="card">
            <div className="stat-value">{(data.backup_jobs || []).length}</div>
            <div className="stat-label">Backup jobs</div>
          </div>
        )}
      </div>

      <h2>Performance</h2>
      <div className="perf-grid">
        <div className="card perf-card">
          <div className="perf-card-head">
            <div>
              <div className="stat-label">CPU</div>
              <div className={`stat-value ${usageTone(cpu.percent)}`}>{fmtPct(cpu.percent)}</div>
            </div>
            <span className="badge">
              {cpu.count || "—"} {cpu.count === 1 ? "core" : "cores"}
            </span>
          </div>
          <Meter value={cpu.percent} />
          {cpu.model && <p className="muted perf-model">{cpu.model}</p>}
          <dl className="perf-dl">
            <div>
              <dt>User</dt>
              <dd>{fmtPct(cpu.user)}</dd>
            </div>
            <div>
              <dt>System</dt>
              <dd>{fmtPct(cpu.system)}</dd>
            </div>
            <div>
              <dt>I/O wait</dt>
              <dd>{fmtPct(cpu.iowait)}</dd>
            </div>
            <div>
              <dt>Steal</dt>
              <dd className={cpu.steal >= 10 ? "stat-value-inline danger" : ""}>{fmtPct(cpu.steal)}</dd>
            </div>
          </dl>
        </div>

        <div className="card perf-card">
          <div className="perf-card-head">
            <div>
              <div className="stat-label">Memory</div>
              <div className={`stat-value ${usageTone(mem.percent)}`}>{fmtPct(mem.percent)}</div>
            </div>
            <span className="muted">{fmtBytes(mem.available)} free</span>
          </div>
          <Meter value={mem.percent} />
          <dl className="perf-dl">
            <div>
              <dt>Used</dt>
              <dd>{fmtBytes(mem.used)}</dd>
            </div>
            <div>
              <dt>Total</dt>
              <dd>{fmtBytes(mem.total)}</dd>
            </div>
            <div>
              <dt>Cached</dt>
              <dd>{fmtBytes(mem.cached)}</dd>
            </div>
            <div>
              <dt>Swap</dt>
              <dd>
                {mem.swap_total
                  ? `${fmtBytes(mem.swap_used)} / ${fmtBytes(mem.swap_total)}`
                  : "None"}
              </dd>
            </div>
          </dl>
          {mem.swap_total ? <Meter value={mem.swap_percent} /> : null}
        </div>

        <div className="card perf-card">
          <div className="perf-card-head">
            <div>
              <div className="stat-label">Disk</div>
              <div className={`stat-value ${usageTone(disk.percent)}`}>
                {disk.percent != null ? fmtPct(disk.percent) : "—"}
              </div>
            </div>
            <span className="muted">{fmtBytes(disk.free)} free</span>
          </div>
          <Meter value={disk.percent} />
          <dl className="perf-dl">
            <div>
              <dt>Used</dt>
              <dd>{fmtBytes(disk.used)}</dd>
            </div>
            <div>
              <dt>Total</dt>
              <dd>{fmtBytes(disk.total)}</dd>
            </div>
            <div>
              <dt>Free</dt>
              <dd>{fmtBytes(disk.free)}</dd>
            </div>
            <div>
              <dt>Mount</dt>
              <dd>/</dd>
            </div>
          </dl>
        </div>

        <div className="card perf-card">
          <div className="perf-card-head">
            <div>
              <div className="stat-label">Load & uptime</div>
              <div className={`stat-value ${usageTone(cpu.load_percent)}`}>
                {loadAvg[0] != null ? loadAvg[0].toFixed(2) : "—"}
              </div>
            </div>
            <span className="muted">{fmtUptime(host.uptime_seconds)} up</span>
          </div>
          <Meter value={cpu.load_percent} />
          <p className="muted" style={{ margin: "0.45rem 0 0" }}>
            {loadAvg.length
              ? `1 / 5 / 15 min: ${loadAvg.map((n) => n.toFixed(2)).join(" · ")}`
              : "Load average unavailable."}{" "}
            {loadHint(loadAvg[0], cpu.count)}
          </p>
          <dl className="perf-dl">
            <div>
              <dt>Host</dt>
              <dd>{host.hostname || "—"}</dd>
            </div>
            <div>
              <dt>OS</dt>
              <dd>{host.os || "—"}</dd>
            </div>
          </dl>
        </div>
      </div>

      {can.docker && (
        <>
          <h2>Unhealthy / stopped</h2>
          {(data.unhealthy || []).length === 0 ? (
            <p className="muted">All clear.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>What's wrong</th>
                    <th>Status</th>
                    <th>Health</th>
                  </tr>
                </thead>
                <tbody>
                  {data.unhealthy.map((c) => (
                    <tr key={c.id}>
                      <Td label="Name">
                        <Link to="/docker">{c.name}</Link>
                      </Td>
                      <Td label="What's wrong" className="td-issue">
                        <IssueBlurb issue={c.issue} fallback="Not running normally." />
                      </Td>
                      <Td label="Status">
                        <span className={`badge ${issueSeverityClass(c.issue, c.status === "running" ? "ok" : "warn")}`}>
                          {c.status}
                        </span>
                      </Td>
                      <Td label="Health">
                        {c.health ? (
                          <span className={`badge ${healthBadgeClass(c.health)}`}>{c.health}</span>
                        ) : (
                          "—"
                        )}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {can.sites && (
        <>
          <h2>Certificates</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Domain</th>
                  <th>Days left</th>
                  <th>Expires</th>
                </tr>
              </thead>
              <tbody>
                {(data.certs || []).map((c) => (
                  <tr key={c.domain}>
                    <Td label="Domain">
                      <Link to="/sites">{c.domain}</Link>
                    </Td>
                    <Td label="Days left">
                      <span className={`badge ${c.days_left < 14 ? "danger" : c.days_left < 30 ? "warn" : "ok"}`}>
                        {c.days_left}
                      </span>
                    </Td>
                    <Td label="Expires" className="muted">{c.not_after}</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {can.backups && (
        <>
          <h2>Recent backups</h2>
          {(data.backups || []).length === 0 ? (
            <p className="muted">
              No runs yet. <Link to="/backups">Configure backups</Link>
            </p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Job</th>
                    <th>Status</th>
                    <th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {data.backups.map((b) => (
                    <tr key={b.id}>
                      <Td label="Job">{b.job_name || b.job_id}</Td>
                      <Td label="Status">
                        <span className={`badge ${b.status === "success" ? "ok" : "danger"}`}>{b.status}</span>
                      </Td>
                      <Td label="When" className="muted">{b.finished_at || b.started_at}</Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
