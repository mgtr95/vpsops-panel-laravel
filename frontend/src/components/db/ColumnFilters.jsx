import { FILTER_OPS } from "./helpers";

export default function ColumnFilters({ columns, filters, onChange, variant = "header", extra = null }) {
  if (!columns.length) return null;

  if (variant === "stack") {
    return (
      <div className="db-mobile-filters">
        {columns.map((name) => (
          <FilterControl key={name} name={name} filters={filters} onChange={onChange} labeled />
        ))}
      </div>
    );
  }

  return (
    <tr className="db-filter-row">
      {columns.map((name) => (
        <th key={name}>
          <FilterControl name={name} filters={filters} onChange={onChange} />
        </th>
      ))}
      {extra}
    </tr>
  );
}

function FilterControl({ name, filters, onChange, labeled }) {
  const f = filters[name] || { op: "contains", value: "" };
  const needsValue = f.op !== "empty" && f.op !== "not_empty";
  return (
    <div className={labeled ? "field" : "db-col-filter"}>
      {labeled && <label>{name}</label>}
      <select
        value={f.op}
        aria-label={`${name} match type`}
        onChange={(e) => onChange(name, { ...f, op: e.target.value })}
      >
        {FILTER_OPS.map((op) => (
          <option key={op.id} value={op.id}>
            {op.label}
          </option>
        ))}
      </select>
      {needsValue && (
        <input
          value={f.value ?? ""}
          aria-label={`Filter ${name}`}
          placeholder={labeled ? `Filter ${name}` : "Filter…"}
          onChange={(e) => onChange(name, { ...f, value: e.target.value })}
        />
      )}
    </div>
  );
}
