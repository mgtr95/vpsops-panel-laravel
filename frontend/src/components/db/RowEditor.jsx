import { useEffect, useMemo, useState } from "react";
import {
  colName,
  displayCell,
  fromDatetimeLocal,
  isAutoInc,
  isBinary,
  isBool,
  isDateOnly,
  isDateTime,
  isLongText,
  isNullable,
  isNumber,
  isPk,
  pkColumns,
  toDatetimeLocal,
} from "./helpers";

function defaultValue(col) {
  const d = col.Default;
  if (d == null || d === "NULL") return "";
  if (typeof d === "string" && /current_timestamp|now\(\)/i.test(d)) return "";
  return String(d);
}

function FieldInput({ col, value, nullOn, onValue, onNull }) {
  const name = colName(col);
  const nullable = isNullable(col);
  const disabled = nullOn;

  let control;
  if (isBinary(col)) {
    control = <p className="muted field-hint">Binary values can’t be edited here.</p>;
  } else if (isBool(col)) {
    control = (
      <label className="check-row">
        <input
          type="checkbox"
          checked={value === true || value === 1 || value === "1"}
          disabled={disabled}
          onChange={(e) => onValue(e.target.checked ? 1 : 0)}
        />
        Yes
      </label>
    );
  } else if (isDateTime(col)) {
    control = (
      <input
        type="datetime-local"
        value={toDatetimeLocal(value)}
        disabled={disabled}
        onChange={(e) => onValue(fromDatetimeLocal(e.target.value))}
      />
    );
  } else if (isDateOnly(col)) {
    control = (
      <input
        type="date"
        value={String(value || "").slice(0, 10)}
        disabled={disabled}
        onChange={(e) => onValue(e.target.value)}
      />
    );
  } else if (isLongText(col)) {
    control = (
      <textarea
        rows={4}
        value={value ?? ""}
        disabled={disabled}
        onChange={(e) => onValue(e.target.value)}
        style={{ fontFamily: "var(--font)", minHeight: 80 }}
      />
    );
  } else {
    control = (
      <input
        type={isNumber(col) ? "number" : "text"}
        step={isNumber(col) ? "any" : undefined}
        value={value ?? ""}
        disabled={disabled}
        onChange={(e) => {
          if (!isNumber(col)) {
            onValue(e.target.value);
            return;
          }
          onValue(e.target.value === "" ? "" : e.target.value);
        }}
      />
    );
  }

  return (
    <div className={`field${isLongText(col) ? " field--full" : ""}`}>
      <label>
        {name}
        {isPk(col) ? " (key)" : ""}
        {isAutoInc(col) ? " · auto" : ""}
      </label>
      {control}
      {nullable && !isBinary(col) && (
        <label className="check-row">
          <input type="checkbox" checked={nullOn} onChange={(e) => onNull(e.target.checked)} />
          Empty (NULL)
        </label>
      )}
    </div>
  );
}

export default function RowEditor({ mode, colMeta, initialValues, busy, error, onSave, onCancel }) {
  const cols = useMemo(() => (colMeta || []).filter((c) => colName(c)), [colMeta]);
  const [values, setValues] = useState({});
  const [nulls, setNulls] = useState({});

  useEffect(() => {
    const next = {};
    const nextNulls = {};
    cols.forEach((col) => {
      const name = colName(col);
      if (mode === "edit") {
        const v = initialValues?.[name];
        if (v == null) {
          next[name] = "";
          nextNulls[name] = true;
        } else {
          next[name] = v;
          nextNulls[name] = false;
        }
      } else if (isAutoInc(col)) {
        next[name] = "";
        nextNulls[name] = false;
      } else if (initialValues && Object.prototype.hasOwnProperty.call(initialValues, name)) {
        next[name] = initialValues[name] ?? "";
        nextNulls[name] = initialValues[name] == null;
      } else {
        next[name] = defaultValue(col);
        nextNulls[name] = false;
      }
    });
    setValues(next);
    setNulls(nextNulls);
  }, [mode, cols, initialValues]);

  function submit(e) {
    e.preventDefault();
    const payload = {};
    cols.forEach((col) => {
      const name = colName(col);
      if (isBinary(col)) return;
      if (mode === "add" && isAutoInc(col) && (nulls[name] || values[name] === "" || values[name] == null)) {
        return;
      }
      if (nulls[name]) {
        payload[name] = null;
        return;
      }
      let v = values[name];
      if (isNumber(col) && !isBool(col) && v !== "" && v != null) {
        const n = Number(v);
        v = Number.isNaN(n) ? v : n;
      }
      payload[name] = v;
    });
    onSave(payload);
  }

  return (
    <div className="card db-editor">
      <h2>{mode === "edit" ? "Edit row" : "Add row"}</h2>
      {error && <p className="error">{error}</p>}
      <form onSubmit={submit}>
        <div className="form-grid cols-2">
          {cols.map((col) => (
            <FieldInput
              key={colName(col)}
              col={col}
              value={values[colName(col)]}
              nullOn={!!nulls[colName(col)]}
              onValue={(v) => {
                setValues((prev) => ({ ...prev, [colName(col)]: v }));
                setNulls((prev) => ({ ...prev, [colName(col)]: false }));
              }}
              onNull={(on) => setNulls((prev) => ({ ...prev, [colName(col)]: on }))}
            />
          ))}
        </div>
        {pkColumns(colMeta).length === 0 && mode === "add" && (
          <p className="muted field-hint">This table has no primary key. You can still add a row.</p>
        )}
        <div className="row-actions">
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
          <button type="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}

export function PreviewCell({ value, col }) {
  const text = displayCell(value, col);
  const title = value == null ? "NULL" : String(value);
  return (
    <span className={value == null ? "muted" : undefined} title={title}>
      {text}
    </span>
  );
}
