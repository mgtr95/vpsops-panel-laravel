import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Td, TdActions } from "../components/Table";
import RcloneWizard from "./RcloneWizard";
import { canDo, useAuth } from "../auth";

const TABS = [
  { id: "remotes", label: "Storage" },
  { id: "browser", label: "Files" },
  { id: "transfer", label: "Copy" },
  { id: "advanced", label: "Advanced" },
];

const LOCAL_FS = [
  { value: "/apps", label: "This server — /apps" },
  { value: "/data", label: "This server — /data" },
];

function splitFs(fs) {
  const value = fs || "";
  if (value.startsWith("/")) {
    const root = LOCAL_FS.map((x) => x.value).find((r) => value === r || value.startsWith(`${r}/`));
    if (root) return { base: root, path: value.slice(root.length).replace(/^\//, "") };
    return { base: value, path: "" };
  }
  const idx = value.indexOf(":");
  if (idx >= 0) return { base: value.slice(0, idx + 1), path: value.slice(idx + 1) };
  return { base: value, path: "" };
}

function joinFs(base, path) {
  const p = (path || "").replace(/^\/+/, "");
  if (!base) return p;
  if (base.endsWith(":")) return `${base}${p}`;
  if (!p) return base;
  return `${base}/${p}`;
}

function FsPicker({ remotes, value, onChange }) {
  const { base, path } = splitFs(value);
  const options = [
    ...remotes.map((r) => ({ value: `${r}:`, label: r })),
    ...LOCAL_FS,
  ];
  const known = options.some((o) => o.value === base);
  return (
    <div className="form-grid cols-2">
      <div className="field">
        <label>Location</label>
        <select
          value={known ? base : options[0]?.value || ""}
          onChange={(e) => onChange(joinFs(e.target.value, path))}
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>Folder (optional)</label>
        <input
          value={path}
          onChange={(e) => onChange(joinFs(known ? base : options[0]?.value || "", e.target.value))}
          placeholder="backups/2026"
        />
      </div>
    </div>
  );
}

export default function RclonePage() {
  const me = useAuth();
  const canAddRemote = canDo(me, "rclone", "add_remote");
  const canDeleteRemote = canDo(me, "rclone", "delete_remote");
  const canMkdir = canDo(me, "rclone", "mkdir");
  const canDelete = canDo(me, "rclone", "delete");
  const canTransfer = canDo(me, "rclone", "transfer");
  const canDownloadConfig = canDo(me, "rclone", "download_config");
  const canRawRc = canDo(me, "rclone", "raw_rc");
  const visibleTabs = TABS.filter((t) => {
    if (t.id === "transfer") return canTransfer;
    if (t.id === "advanced") return canRawRc;
    return true;
  });
  const [tab, setTab] = useState("remotes");
  const [remotes, setRemotes] = useState([]);
  const [providers, setProviders] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [fs, setFs] = useState("");
  const [path, setPath] = useState("");
  const [listing, setListing] = useState([]);

  const [op, setOp] = useState("copy");
  const [srcFs, setSrcFs] = useState("");
  const [dstFs, setDstFs] = useState("");
  const [transferResult, setTransferResult] = useState(null);
  const [stats, setStats] = useState(null);

  const [rcMethod, setRcMethod] = useState("core/version");
  const [rcParams, setRcParams] = useState("{}");
  const [rcResult, setRcResult] = useState("");

  async function loadRemotes() {
    const data = await api("/rclone/remotes");
    setRemotes(data.remotes || []);
    if (!fs && data.remotes?.[0]) setFs(`${data.remotes[0]}:`);
    if (!srcFs && data.remotes?.[0]) setSrcFs(`${data.remotes[0]}:`);
    if (!dstFs) setDstFs("/data");
  }

  async function loadProviders() {
    try {
      const data = await api("/rclone/providers");
      setProviders(data.providers || data || []);
    } catch {
      /* optional */
    }
  }

  useEffect(() => {
    (async () => {
      try {
        await loadRemotes();
        await loadProviders();
      } catch (e) {
        setError(e.message);
      }
    })();
  }, []);

  async function deleteRemote(r) {
    if (!confirm(`Remove “${r}” from this panel? Files in the cloud are not deleted.`)) return;
    await api(`/rclone/remotes/${encodeURIComponent(r)}`, { method: "DELETE" });
    await loadRemotes();
  }

  async function browse(nextPath = path, nextFs = fs) {
    setBusy(true);
    setError("");
    try {
      const data = await api(
        `/rclone/list?fs=${encodeURIComponent(nextFs)}&remote=${encodeURIComponent(nextPath)}`
      );
      setListing(data.list || []);
      setPath(nextPath);
      setFs(nextFs);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function mkdir() {
    const folder = prompt("Folder name");
    if (!folder) return;
    const remote = path ? `${path}/${folder}` : folder;
    await api("/rclone/mkdir", { method: "POST", body: { fs, remote } });
    await browse(path);
  }

  async function removeItem(item) {
    const remote = path ? `${path}/${item.Path || item.Name}` : item.Path || item.Name;
    if (item.IsDir) {
      if (!confirm(`Delete folder ${remote} and everything in it?`)) return;
      await api("/rclone/purge", { method: "POST", body: { fs, remote } });
    } else {
      if (!confirm(`Delete ${remote}?`)) return;
      await api("/rclone/deletefile", { method: "POST", body: { fs, remote } });
    }
    await browse(path);
  }

  async function runTransfer() {
    setBusy(true);
    setError("");
    try {
      const result = await api("/rclone/transfer", {
        method: "POST",
        body: { op, srcFs, dstFs, async_job: true },
      });
      setTransferResult(result);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function refreshStats() {
    try {
      setStats(await api("/rclone/stats"));
      if (transferResult?.jobid) {
        const st = await api(`/rclone/jobs/${transferResult.jobid}`);
        setTransferResult((prev) => ({ ...prev, status: st }));
      }
    } catch (e) {
      setError(e.message);
    }
  }

  async function runRaw() {
    setBusy(true);
    try {
      const params = JSON.parse(rcParams || "{}");
      const data = await api("/rclone/rc", { method: "POST", body: { method: rcMethod, params } });
      setRcResult(JSON.stringify(data, null, 2));
    } catch (e) {
      setError(e.message);
      setRcResult(e.message);
    } finally {
      setBusy(false);
    }
  }

  function downloadConfig() {
    fetch("/api/rclone/config/file", { credentials: "include" })
      .then(async (res) => {
        if (!res.ok) throw new Error(await res.text());
        return res.blob();
      })
      .then((blob) => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "rclone.conf";
        a.click();
      })
      .catch((e) => setError(e.message));
  }

  function openBrowser(remoteName) {
    const nextFs = `${remoteName}:`;
    setTab("browser");
    browse("", nextFs);
  }

  function onWizardDone(remoteName) {
    loadRemotes().then(() => {
      if (remoteName) openBrowser(remoteName);
      else setTab("remotes");
    });
  }

  return (
    <div>
      <div className="page-header">
        <h1>Rclone</h1>
        <div className="page-header-actions toolbar--tabs">
          {visibleTabs.map((t) => (
            <button key={t.id} className={tab === t.id ? "primary" : ""} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {busy && tab !== "setup" && <p className="muted">Working…</p>}

      {tab === "setup" && canAddRemote && (
        <RcloneWizard
          remotes={remotes}
          providers={providers}
          onDone={onWizardDone}
          onCancel={() => setTab("remotes")}
        />
      )}

      {tab === "remotes" && (
        <>
          <div className="toolbar">
            {canAddRemote && (
              <button className="primary" onClick={() => setTab("setup")}>
                Add storage
              </button>
            )}
            <button onClick={loadRemotes}>Refresh</button>
            {canDownloadConfig && (
              <button onClick={downloadConfig}>Download rclone.conf</button>
            )}
          </div>
          {remotes.length === 0 ? (
            <p className="muted">Nothing connected yet. Click Add storage to link Google Drive, OneDrive, S3, and more.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {remotes.map((r) => (
                    <tr key={r}>
                      <Td label="Name">
                        <strong>{r}</strong>
                      </Td>
                      <TdActions>
                        <div className="row-actions">
                          <button onClick={() => openBrowser(r)}>Browse</button>
                          {canDeleteRemote && (
                            <button className="danger" onClick={() => deleteRemote(r)}>
                              Remove
                            </button>
                          )}
                        </div>
                      </TdActions>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {tab === "browser" && (
        <>
          <div className="toolbar toolbar--stack-sm">
            <select
              value={fs}
              onChange={(e) => setFs(e.target.value)}
              className="select-inline"
            >
              {remotes.map((r) => (
                <option key={r} value={`${r}:`}>
                  {r}
                </option>
              ))}
              <option value="/apps">This server — /apps</option>
              <option value="/data">This server — /data</option>
            </select>
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="folder"
              className="input-inline"
            />
            <button className="primary" onClick={() => browse(path)}>
              Open
            </button>
            {canMkdir && (
              <button onClick={mkdir}>New folder</button>
            )}
            <button
              onClick={() => {
                const parts = path.split("/").filter(Boolean);
                parts.pop();
                browse(parts.join("/"));
              }}
            >
              Up
            </button>
          </div>
          {remotes.length === 0 && (
            <p className="muted">
              Add storage first.{" "}
              {canAddRemote && (
                <button className="list-btn" style={{ display: "inline", width: "auto" }} onClick={() => setTab("setup")}>
                  Add storage
                </button>
              )}
            </p>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Size</th>
                  <th>Modified</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {listing.map((item) => {
                  const label = item.Name || item.Path;
                  return (
                    <tr key={label}>
                      <Td label="Name">
                        {item.IsDir ? (
                          <button
                            className="list-btn"
                            style={{ display: "inline" }}
                            onClick={() => browse(path ? `${path}/${label}` : label)}
                          >
                            {label}/
                          </button>
                        ) : (
                          label
                        )}
                      </Td>
                      <Td label="Size" className="muted">{item.IsDir ? "—" : item.Size}</Td>
                      <Td label="Modified" className="muted">{item.ModTime || "—"}</Td>
                      <TdActions>
                        {canDelete && (
                          <button className="danger" onClick={() => removeItem(item)}>
                            Delete
                          </button>
                        )}
                      </TdActions>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {tab === "transfer" && canTransfer && (
        <div className="panel stack">
          <p className="muted">Copy or move files between connected storage and this server.</p>
          <div className="field">
            <label>What to do</label>
            <select value={op} onChange={(e) => setOp(e.target.value)}>
              <option value="copy">Copy (add missing files)</option>
              <option value="sync">Mirror (make destination match source)</option>
              <option value="move">Move</option>
              <option value="check">Check they match</option>
            </select>
          </div>
          <h3 style={{ margin: "0.5rem 0 0" }}>From</h3>
          <FsPicker remotes={remotes} value={srcFs} onChange={setSrcFs} />
          <h3 style={{ margin: "0.5rem 0 0" }}>To</h3>
          <FsPicker remotes={remotes} value={dstFs} onChange={setDstFs} />
          <div className="toolbar">
            <button className="primary" onClick={runTransfer} disabled={!srcFs || !dstFs}>
              Start
            </button>
            <button onClick={refreshStats}>Refresh progress</button>
          </div>
          {transferResult && <pre className="log">{JSON.stringify(transferResult, null, 2)}</pre>}
          {stats && <pre className="log">{JSON.stringify(stats, null, 2)}</pre>}
        </div>
      )}

      {tab === "advanced" && canRawRc && (
        <div className="panel stack">
          <p className="muted">
            Call any rclone RC method. See{" "}
            <a href="https://rclone.org/rc/" target="_blank" rel="noreferrer">
              rclone.org/rc
            </a>
            .
          </p>
          <div className="field">
            <label>Method</label>
            <input value={rcMethod} onChange={(e) => setRcMethod(e.target.value)} placeholder="config/listremotes" />
          </div>
          <div className="field">
            <label>Params JSON</label>
            <textarea value={rcParams} onChange={(e) => setRcParams(e.target.value)} />
          </div>
          <button className="primary" onClick={runRaw}>
            Call
          </button>
          <pre className="log">{rcResult}</pre>
        </div>
      )}
    </div>
  );
}
