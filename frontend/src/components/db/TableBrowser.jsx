import { useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import { Td, TdActions } from "../Table";
import ColumnFilters from "./ColumnFilters";
import RowEditor, { PreviewCell } from "./RowEditor";
import {
  activeFilterRules,
  colName,
  formatCount,
  isPk,
  loadHiddenColumns,
  pkColumns,
  pkFromRow,
  rowToObject,
  saveHiddenColumns,
  tableApproxRows,
  tableName,
} from "./helpers";

export default function TableBrowser({
  connId,
  database,
  tables,
  table,
  onSelectTable,
  onError,
  reloadToken = 0,
  canInsert = true,
  canUpdate = true,
  canDeleteRow = true,
}) {
  const [tableQuery, setTableQuery] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [q, setQ] = useState("");
  const [columnFilters, setColumnFilters] = useState({});
  const [debouncedFilters, setDebouncedFilters] = useState({});
  const [match, setMatch] = useState("all");
  const [sort, setSort] = useState("");
  const [order, setOrder] = useState("asc");
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [colMeta, setColMeta] = useState([]);
  const [rows, setRows] = useState(null);
  const [loading, setLoading] = useState(false);
  const [hidden, setHidden] = useState([]);
  const [showCols, setShowCols] = useState(false);
  const [editor, setEditor] = useState(null);
  const [formBusy, setFormBusy] = useState(false);
  const [formError, setFormError] = useState("");

  const filteredTables = useMemo(() => {
    const qn = tableQuery.trim().toLowerCase();
    return (tables || []).filter((t) => {
      const name = tableName(t);
      return !qn || name.toLowerCase().includes(qn);
    });
  }, [tables, tableQuery]);

  const pkCols = useMemo(() => pkColumns(colMeta), [colMeta]);
  const hasPk = pkCols.length > 0;
  const colByName = useMemo(() => {
    const map = {};
    colMeta.forEach((c) => {
      map[colName(c)] = c;
    });
    return map;
  }, [colMeta]);

  const visibleColumns = useMemo(() => {
    const all = rows?.columns || colMeta.map(colName).filter(Boolean);
    const hiddenSet = new Set(hidden);
    return all.filter((c) => !hiddenSet.has(c));
  }, [rows, colMeta, hidden]);

  useEffect(() => {
    setSearchInput("");
    setQ("");
    setColumnFilters({});
    setDebouncedFilters({});
    setMatch("all");
    setSort("");
    setOrder("asc");
    setOffset(0);
    setEditor(null);
    setFormError("");
    setShowCols(false);
    setRows(null);
    setColMeta([]);
    if (connId && table) setHidden(loadHiddenColumns(connId, database, table));
    else setHidden([]);
  }, [connId, database, table]);

  useEffect(() => {
    const t = setTimeout(() => setQ(searchInput), 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedFilters((prev) =>
        JSON.stringify(prev) === JSON.stringify(columnFilters) ? prev : columnFilters
      );
    }, 300);
    return () => clearTimeout(t);
  }, [columnFilters]);

  const filtersKey = JSON.stringify(activeFilterRules(debouncedFilters));

  useEffect(() => {
    setOffset(0);
  }, [q, filtersKey, match, sort, order, limit]);

  useEffect(() => {
    if (!connId || !table) return;
    let cancelled = false;
    const params = database ? `?database=${encodeURIComponent(database)}` : "";
    api(`/db/${encodeURIComponent(connId)}/tables/${encodeURIComponent(table)}/columns${params}`)
      .then((cols) => {
        if (!cancelled) setColMeta(Array.isArray(cols) ? cols : []);
      })
      .catch((e) => {
        if (!cancelled) onError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [connId, database, table, onError, reloadToken]);

  useEffect(() => {
    if (!connId || !table) return;
    let cancelled = false;
    async function load() {
      setLoading(true);
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(offset),
        match,
      });
      if (database) params.set("database", database);
      if (q.trim()) params.set("q", q.trim());
      if (sort) {
        params.set("sort", sort);
        params.set("order", order);
      }
      const rules = activeFilterRules(debouncedFilters);
      if (rules.length) params.set("filters", JSON.stringify(rules));
      try {
        const data = await api(
          `/db/${encodeURIComponent(connId)}/tables/${encodeURIComponent(table)}/rows?${params}`
        );
        if (!cancelled) {
          setRows(data);
          onError("");
        }
      } catch (e) {
        if (!cancelled) {
          setRows(null);
          onError(e.message);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [connId, database, table, q, sort, order, debouncedFilters, match, limit, offset, onError, reloadToken]);

  function toggleSort(col) {
    if (sort === col) setOrder((o) => (o === "asc" ? "desc" : "asc"));
    else {
      setSort(col);
      setOrder("asc");
    }
    setOffset(0);
  }

  function setFilter(name, next) {
    setColumnFilters((prev) => ({ ...prev, [name]: next }));
  }

  function clearFilters() {
    setSearchInput("");
    setQ("");
    setColumnFilters({});
    setDebouncedFilters({});
    setMatch("all");
    setOffset(0);
  }

  function toggleHidden(name) {
    setHidden((prev) => {
      const next = prev.includes(name) ? prev.filter((c) => c !== name) : [...prev, name];
      saveHiddenColumns(connId, database, table, next);
      return next;
    });
  }

  async function reloadCurrent() {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
      match,
    });
    if (database) params.set("database", database);
    if (q.trim()) params.set("q", q.trim());
    if (sort) {
      params.set("sort", sort);
      params.set("order", order);
    }
    const rules = activeFilterRules(debouncedFilters);
    if (rules.length) params.set("filters", JSON.stringify(rules));
    setRows(
      await api(`/db/${encodeURIComponent(connId)}/tables/${encodeURIComponent(table)}/rows?${params}`)
    );
  }

  async function saveRow(values) {
    setFormBusy(true);
    setFormError("");
    try {
      const body = { values, database: database || null };
      if (editor?.mode === "edit") {
        body.pk = editor.pk;
        await api(`/db/${encodeURIComponent(connId)}/tables/${encodeURIComponent(table)}/row`, {
          method: "PUT",
          body,
        });
      } else {
        await api(`/db/${encodeURIComponent(connId)}/tables/${encodeURIComponent(table)}/row`, {
          method: "POST",
          body,
        });
      }
      setEditor(null);
      await reloadCurrent();
    } catch (e) {
      setFormError(e.message);
    } finally {
      setFormBusy(false);
    }
  }

  async function deleteRow(row) {
    const pk = pkFromRow(rows.columns, colMeta, row);
    const label = Object.values(pk).map((v) => String(v)).join(", ");
    if (!confirm(`Delete this row${label ? ` (${label})` : ""}? This cannot be undone.`)) return;
    try {
      await api(`/db/${encodeURIComponent(connId)}/tables/${encodeURIComponent(table)}/row`, {
        method: "DELETE",
        body: { pk, database: database || null },
      });
      await reloadCurrent();
    } catch (e) {
      onError(e.message);
    }
  }

  const total = rows?.total ?? 0;
  const from = total === 0 ? 0 : (rows?.offset ?? offset) + 1;
  const to = Math.min((rows?.offset ?? offset) + (rows?.limit ?? limit), total);
  const filterActive = q.trim() || activeFilterRules(columnFilters).length > 0;

  return (
    <div className="split split-2">
      <div className="card split-sidebar">
        <strong>Tables</strong>
        <input
          className="db-table-search"
          value={tableQuery}
          onChange={(e) => setTableQuery(e.target.value)}
          placeholder="Find a table…"
          aria-label="Find a table"
        />
        <div className="db-table-list">
          {filteredTables.length === 0 && <p className="muted">No tables match that name.</p>}
          {filteredTables.map((t) => {
            const name = tableName(t);
            const count = tableApproxRows(t);
            return (
              <button
                key={name}
                type="button"
                className={`list-btn list-btn--table${table === name ? " active" : ""}`}
                onClick={() => onSelectTable(name)}
              >
                <span>{name}</span>
                {count != null && <span className="muted">{formatCount(count)} rows</span>}
              </button>
            );
          })}
        </div>
      </div>
      <div className="db-browse-main">
        {!table && <p className="muted">Pick a table on the left to see its data.</p>}
        {table && (
          <>
            <div className="toolbar toolbar--stack-sm">
              <input
                className="input-inline"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Search this table…"
                aria-label="Search this table"
              />
              {canInsert && (
                <button type="button" className="primary" onClick={() => { setFormError(""); setEditor({ mode: "add" }); }}>
                  Add row
                </button>
              )}
              <div className="db-col-menu-wrap">
                <button type="button" onClick={() => setShowCols((v) => !v)} aria-expanded={showCols}>
                  Columns
                </button>
                {showCols && (
                  <div className="card db-col-menu">
                    {(rows?.columns || colMeta.map(colName).filter(Boolean)).map((name) => (
                      <label key={name} className="check-row">
                        <input
                          type="checkbox"
                          checked={!hidden.includes(name)}
                          onChange={() => toggleHidden(name)}
                        />
                        {name}
                        {colByName[name] && isPk(colByName[name]) ? " (key)" : ""}
                      </label>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div className="toolbar toolbar--stack-sm">
              <span className="muted">
                {table}
                {loading && (
                  <>
                    {" "}
                    <span className="spinner" aria-hidden="true" />
                  </>
                )}
                {!loading && ` · ${from}–${to} of ${formatCount(total)}`}
              </span>
              <select
                className="select-inline"
                value={match}
                onChange={(e) => {
                  setMatch(e.target.value);
                  setOffset(0);
                }}
                aria-label="Combine filters"
              >
                <option value="all">All filters</option>
                <option value="any">Any filter</option>
              </select>
              <button type="button" onClick={clearFilters} disabled={!filterActive && match === "all"}>
                Clear filters
              </button>
              <select
                className="select-inline"
                value={sort}
                onChange={(e) => {
                  setSort(e.target.value);
                  setOrder("asc");
                  setOffset(0);
                }}
                aria-label="Sort by column"
              >
                <option value="">Default order</option>
                {visibleColumns.map((c) => (
                  <option key={c} value={c}>
                    Sort: {c}
                  </option>
                ))}
              </select>
              {sort && (
                <button
                  type="button"
                  onClick={() => {
                    setOrder((o) => (o === "asc" ? "desc" : "asc"));
                    setOffset(0);
                  }}
                >
                  {order === "asc" ? "A → Z" : "Z → A"}
                </button>
              )}
              <select
                className="select-inline"
                value={limit}
                onChange={(e) => {
                  setLimit(Number(e.target.value));
                  setOffset(0);
                }}
                aria-label="Rows per page"
              >
                <option value={25}>25 per page</option>
                <option value={50}>50 per page</option>
                <option value={100}>100 per page</option>
              </select>
              <button type="button" disabled={!rows || rows.offset <= 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
                Prev
              </button>
              <button
                type="button"
                disabled={!rows || rows.offset + rows.limit >= total}
                onClick={() => setOffset(offset + limit)}
              >
                Next
              </button>
            </div>
            {!hasPk && (
              <p className="muted">This table has no primary key, so rows can’t be edited or deleted here. You can still add a row or use Advanced SQL.</p>
            )}
            {editor && ((editor.mode === "add" && canInsert) || (editor.mode === "edit" && canUpdate)) && (
              <RowEditor
                mode={editor.mode}
                colMeta={colMeta}
                initialValues={editor.values}
                busy={formBusy}
                error={formError}
                onSave={saveRow}
                onCancel={() => setEditor(null)}
              />
            )}
            <ColumnFilters variant="stack" columns={visibleColumns} filters={columnFilters} onChange={setFilter} />
            {rows && visibleColumns.length === 0 && <p className="muted">All columns are hidden. Use Columns to show some.</p>}
            {rows && rows.rows.length === 0 && (
              <p className="muted">{filterActive ? "No rows match these filters." : "This table is empty."}</p>
            )}
            {rows && visibleColumns.length > 0 && rows.rows.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      {visibleColumns.map((c) => (
                        <th key={c}>
                          <button type="button" className="db-th-sort" onClick={() => toggleSort(c)}>
                            {c}
                            {sort === c ? (order === "asc" ? " ↑" : " ↓") : ""}
                          </button>
                        </th>
                      ))}
                      {hasPk && (canUpdate || canDeleteRow) && <th>Actions</th>}
                    </tr>
                    <ColumnFilters
                      columns={visibleColumns}
                      filters={columnFilters}
                      onChange={setFilter}
                      extra={hasPk && (canUpdate || canDeleteRow) ? <th /> : null}
                    />
                  </thead>
                  <tbody>
                    {rows.rows.map((r, i) => (
                      <tr key={hasPk ? JSON.stringify(pkFromRow(rows.columns, colMeta, r)) : i}>
                        {visibleColumns.map((c) => {
                          const idx = rows.columns.indexOf(c);
                          return (
                            <Td key={c} label={c}>
                              <PreviewCell value={idx >= 0 ? r[idx] : null} col={colByName[c]} />
                            </Td>
                          );
                        })}
                        {hasPk && (canUpdate || canDeleteRow) && (
                          <TdActions>
                            <div className="row-actions">
                              {canUpdate && (
                                <button
                                  type="button"
                                  onClick={() => {
                                    setFormError("");
                                    setEditor({
                                      mode: "edit",
                                      pk: pkFromRow(rows.columns, colMeta, r),
                                      values: rowToObject(rows.columns, r),
                                    });
                                  }}
                                >
                                  Edit
                                </button>
                              )}
                              {canDeleteRow && (
                                <button type="button" className="danger" onClick={() => deleteRow(r)}>
                                  Delete
                                </button>
                              )}
                            </div>
                          </TdActions>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {!rows && loading && (
              <p className="muted">
                <span className="spinner" aria-hidden="true" /> Loading rows…
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
