export const FILTER_OPS = [
  { id: "contains", label: "Contains" },
  { id: "equals", label: "Equals" },
  { id: "not_equals", label: "Not equal" },
  { id: "starts_with", label: "Starts with" },
  { id: "ends_with", label: "Ends with" },
  { id: "gt", label: "Greater than" },
  { id: "gte", label: "At least" },
  { id: "lt", label: "Less than" },
  { id: "lte", label: "At most" },
  { id: "empty", label: "Is empty" },
  { id: "not_empty", label: "Is not empty" },
];

export function colName(c) {
  return c?.Field || c?.name || "";
}

export function tableName(t) {
  return t?.Name || t?.name || "";
}

export function tableApproxRows(t) {
  const n = t?.Rows ?? t?.rows;
  return typeof n === "number" ? n : null;
}

export function isPk(c) {
  return c?.Key === "PRI";
}

export function isNullable(c) {
  return c?.Null === "YES";
}

export function isAutoInc(c) {
  return /auto_increment/i.test(c?.Extra || "");
}

export function isBinary(c) {
  return /blob|binary|varbinary/i.test(c?.Type || "");
}

export function isBool(c) {
  const t = (c?.Type || "").toLowerCase();
  return t.includes("tinyint(1)") || t === "boolean" || t === "bool";
}

export function isNumber(c) {
  return /int|decimal|numeric|float|double|real/i.test(c?.Type || "");
}

export function isDateTime(c) {
  return /datetime|timestamp/i.test(c?.Type || "");
}

export function isDateOnly(c) {
  return /^(date)\b/i.test(c?.Type || "");
}

export function isLongText(c) {
  return /text|json/i.test(c?.Type || "");
}

export function pkColumns(colMeta) {
  return (colMeta || []).filter(isPk).map(colName).filter(Boolean);
}

export function rowToObject(columns, row) {
  const obj = {};
  (columns || []).forEach((name, i) => {
    obj[name] = row[i];
  });
  return obj;
}

export function pkFromRow(columns, colMeta, row) {
  const obj = {};
  pkColumns(colMeta).forEach((name) => {
    const i = columns.indexOf(name);
    obj[name] = i >= 0 ? row[i] : null;
  });
  return obj;
}

export function formatCount(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString();
}

export function hiddenStorageKey(connId, database, table) {
  return `db-cols:${connId}:${database || ""}:${table}`;
}

export function loadHiddenColumns(connId, database, table) {
  try {
    const raw = localStorage.getItem(hiddenStorageKey(connId, database, table));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveHiddenColumns(connId, database, table, hidden) {
  try {
    localStorage.setItem(hiddenStorageKey(connId, database, table), JSON.stringify(hidden));
  } catch {
    /* ignore quota / private mode */
  }
}

export function activeFilterRules(columnFilters) {
  return Object.entries(columnFilters || {})
    .filter(([, f]) => {
      if (!f?.op) return false;
      if (f.op === "empty" || f.op === "not_empty") return true;
      return String(f.value ?? "").length > 0;
    })
    .map(([column, f]) => ({ column, op: f.op, value: f.value ?? "" }));
}

export function displayCell(value, col) {
  if (col && isBinary(col)) return "(binary)";
  if (value == null) return "NULL";
  if (typeof value === "boolean") return value ? "true" : "false";
  const s = String(value);
  return s.length > 120 ? `${s.slice(0, 117)}…` : s;
}

export function toDatetimeLocal(value) {
  if (value == null || value === "") return "";
  const s = String(value).trim().replace(" ", "T");
  return s.length >= 16 ? s.slice(0, 16) : s;
}

export function fromDatetimeLocal(value) {
  if (!value) return value;
  return String(value).replace("T", " ");
}

export function isReadOnlySql(sql) {
  const stripped = String(sql || "")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/--.*$/gm, " ")
    .replace(/^\s*#.*$/gm, " ")
    .trim();
  const first = stripped.split(/\s+/)[0]?.toUpperCase() || "";
  return ["SELECT", "SHOW", "EXPLAIN", "DESCRIBE", "DESC", "PRAGMA", "WITH"].includes(first);
}
