import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { Td } from "../components/Table";
import TableBrowser from "../components/db/TableBrowser";
import { isReadOnlySql } from "../components/db/helpers";
import { canDo, useAuth } from "../auth";

export default function DatabasesPage() {
  const me = useAuth();
  const canInsert = canDo(me, "db", "insert");
  const canUpdate = canDo(me, "db", "update");
  const canDeleteRow = canDo(me, "db", "delete_row");
  const canSql = canDo(me, "db", "sql");
  const canDump = canDo(me, "db", "dump");
  const canImport = canDo(me, "db", "import");
  const [connections, setConnections] = useState([]);
  const [connId, setConnId] = useState("");
  const [databases, setDatabases] = useState([]);
  const [database, setDatabase] = useState("");
  const [tables, setTables] = useState([]);
  const [table, setTable] = useState("");
  const [sql, setSql] = useState("");
  const [sqlResult, setSqlResult] = useState(null);
  const [sqlBusy, setSqlBusy] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [tab, setTab] = useState("browse");
  const [reloadToken, setReloadToken] = useState(0);
  const importRef = useRef(null);
  const dbReq = useRef(0);

  const current = connections.find((c) => c.id === connId);
  const isSqlite = current?.engine === "sqlite";

  function engineLabel(engine) {
    if (engine === "postgres") return "PostgreSQL";
    if (engine === "mysql") return "MySQL";
    if (engine === "sqlite") return "SQLite";
    return engine;
  }

  function selectConnection(id) {
    setConnId(id);
    setDatabases([]);
    setDatabase("");
    setTables([]);
    setTable("");
    setSqlResult(null);
    setError("");
  }

  useEffect(() => {
    api("/db/connections")
      .then((c) => {
        setConnections(c);
        if (c[0]) selectConnection(c[0].id);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!connId) return;
    const req = ++dbReq.current;
    api(`/db/${encodeURIComponent(connId)}/databases`)
      .then((dbs) => {
        if (req !== dbReq.current) return;
        setDatabases(dbs);
        setDatabase(dbs[0] || "");
      })
      .catch((e) => {
        if (req !== dbReq.current) return;
        setError(e.message);
      });
  }, [connId]);

  useEffect(() => {
    if (!connId) return;
    if (!isSqlite && !database) return;
    if (!isSqlite && databases.length > 0 && !databases.includes(database)) return;
    const req = dbReq.current;
    const q = !isSqlite && database ? `?database=${encodeURIComponent(database)}` : "";
    api(`/db/${encodeURIComponent(connId)}/tables${q}`)
      .then((t) => {
        if (req !== dbReq.current) return;
        setTables(t);
        setTable("");
      })
      .catch((e) => {
        if (req !== dbReq.current) return;
        setError(e.message);
      });
  }, [connId, database, databases, isSqlite]);

  async function runSql() {
    const text = sql.trim();
    if (!text) {
      setError("Type a query first.");
      return;
    }
    if (!isReadOnlySql(text)) {
      if (!confirm("This query can change data. Run it anyway?")) return;
    }
    setSqlBusy(true);
    setError("");
    try {
      setSqlResult(
        await api(`/db/${encodeURIComponent(connId)}/sql`, {
          method: "POST",
          body: { sql: text, database: database || null },
        })
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setSqlBusy(false);
    }
  }

  async function dumpDb() {
    setBusy("Preparing download");
    setError("");
    try {
      const params = database ? `?database=${encodeURIComponent(database)}` : "";
      const res = await fetch(`/api/db/${encodeURIComponent(connId)}/dump${params}`, {
        credentials: "include",
      });
      if (!res.ok) {
        setError(await res.text());
        return;
      }
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = isSqlite ? "database.sqlite" : `${database || "backup"}.sql`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function importFile(file) {
    if (!file) return;
    if (
      !confirm(
        `Import “${file.name}”? This can add or overwrite data in ${isSqlite ? "this database" : database || "the selected database"}.`
      )
    ) {
      return;
    }
    setBusy("Importing");
    setError("");
    const fd = new FormData();
    fd.append("file", file);
    const params = database ? `?database=${encodeURIComponent(database)}` : "";
    try {
      const res = await fetch(`/api/db/${encodeURIComponent(connId)}/import${params}`, {
        method: "POST",
        credentials: "include",
        body: fd,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Import failed");
      const q = database ? `?database=${encodeURIComponent(database)}` : "";
      setTables(await api(`/db/${encodeURIComponent(connId)}/tables${q}`));
      setReloadToken((n) => n + 1);
      setBusy("");
      setError("");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
      if (importRef.current) importRef.current.value = "";
    }
  }

  const sqlRows = sqlResult?.rows?.length || 0;
  const sqlChanged = sqlResult && !(sqlResult.columns && sqlResult.columns.length);

  return (
    <div>
      <div className="page-header">
        <h1>Databases</h1>
        <div className="page-header-actions toolbar--tabs">
          <button type="button" onClick={() => setTab("browse")} className={tab === "browse" ? "primary" : ""}>
            Browse
          </button>
          {canSql && (
            <button type="button" onClick={() => setTab("sql")} className={tab === "sql" ? "primary" : ""}>
              Advanced SQL
            </button>
          )}
          {canDump && (
            <button type="button" onClick={dumpDb} disabled={!connId || !!busy}>
              Download backup
            </button>
          )}
          {canImport && (
            <button type="button" disabled={!connId || !!busy} onClick={() => importRef.current?.click()}>
              Import
            </button>
          )}
          <input
            ref={importRef}
            type="file"
            hidden
            onChange={(e) => e.target.files?.[0] && importFile(e.target.files[0])}
          />
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {busy && (
        <p className="muted">
          <span className="spinner" aria-hidden="true" /> {busy}…
        </p>
      )}
      {connections.length === 0 && (
        <p className="muted">
          No databases found yet. The panel picks up MySQL, PostgreSQL, and SQLite from apps it discovers. You can
          also add connections in <code>config/databases.json</code>.
        </p>
      )}
      {isSqlite && (
        <p className="muted">
          This file-based database is used by a live app. Large edits can slow it down — download a backup first if you
          are changing a lot.
        </p>
      )}

      {connections.length > 0 && (
        <div className="toolbar toolbar--stack-sm">
          <label className="db-inline-field">
            <span className="muted">Connection</span>
            <select value={connId} onChange={(e) => selectConnection(e.target.value)} className="select-inline">
              {connections.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.engine ? ` (${engineLabel(c.engine)})` : ""}
                </option>
              ))}
            </select>
          </label>
          {databases.length > 0 && !isSqlite && (
            <label className="db-inline-field">
              <span className="muted">Database</span>
              <select value={database} onChange={(e) => setDatabase(e.target.value)} className="select-inline">
                {databases.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}

      {tab === "browse" && connId && (
        <TableBrowser
          connId={connId}
          database={isSqlite ? "" : database}
          tables={tables}
          table={table}
          onSelectTable={setTable}
          onError={setError}
          reloadToken={reloadToken}
          canInsert={canInsert}
          canUpdate={canUpdate}
          canDeleteRow={canDeleteRow}
        />
      )}

      {tab === "sql" && connId && canSql && (
        <div className="stack">
          <p className="muted">
            Use Browse to search and edit rows without writing SQL. This box is for custom queries.
          </p>
          <textarea
            value={sql}
            onChange={(e) => setSql(e.target.value)}
            rows={8}
            placeholder="SELECT * FROM …"
            spellCheck={false}
          />
          <button className="primary" type="button" onClick={runSql} disabled={sqlBusy}>
            {sqlBusy ? "Running…" : "Run"}
          </button>
          {sqlResult && (
            <>
              <p className="muted">
                {sqlResult.columns?.length
                  ? `${formatSqlCount(sqlRows)} returned`
                  : sqlChanged
                    ? `${formatSqlCount(Math.max(sqlResult.rowcount || 0, 0))} changed`
                    : "Done"}
              </p>
              {sqlResult.columns?.length > 0 && (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        {sqlResult.columns.map((c) => (
                          <th key={c}>{c}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {sqlResult.rows.map((r, i) => (
                        <tr key={i}>
                          {r.map((cell, j) => (
                            <Td key={j} label={sqlResult.columns[j]}>
                              {cell == null ? "NULL" : String(cell)}
                            </Td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function formatSqlCount(n) {
  const v = Number(n) || 0;
  return v === 1 ? "1 row" : `${v.toLocaleString()} rows`;
}
