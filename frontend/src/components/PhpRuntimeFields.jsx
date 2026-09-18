export function emptyPhpLimits(seed = {}) {
  return {
    upload_max_filesize_mb: Number(seed.upload_max_filesize_mb ?? 20),
    post_max_size_mb: Number(seed.post_max_size_mb ?? 30),
    memory_limit_mb: Number(seed.memory_limit_mb ?? 512),
    max_execution_time: Number(seed.max_execution_time ?? 120),
    max_input_time: Number(seed.max_input_time ?? 120),
    client_max_body_size_mb: Number(seed.client_max_body_size_mb ?? 32),
  };
}

const FIELDS = [
  {
    key: "upload_max_filesize_mb",
    label: "Upload max (MB)",
    hint: "PHP upload_max_filesize",
  },
  {
    key: "post_max_size_mb",
    label: "Post max (MB)",
    hint: "Must be ≥ upload max",
  },
  {
    key: "memory_limit_mb",
    label: "Memory limit (MB)",
    hint: "PHP memory_limit",
  },
  {
    key: "max_execution_time",
    label: "Max execution (sec)",
    hint: "0 = unlimited",
  },
  {
    key: "max_input_time",
    label: "Max input (sec)",
    hint: "0 = unlimited",
  },
  {
    key: "client_max_body_size_mb",
    label: "Nginx body max (MB)",
    hint: "Must be ≥ post max",
  },
];

export default function PhpRuntimeFields({ value, onChange, disabled }) {
  function set(key, raw) {
    const n = raw === "" ? "" : Number(raw);
    onChange({ ...value, [key]: n });
  }

  return (
    <div className="form-grid cols-2">
      {FIELDS.map((field) => (
        <div className="field" key={field.key}>
          <label htmlFor={`php-${field.key}`}>{field.label}</label>
          <input
            id={`php-${field.key}`}
            type="number"
            min={0}
            step={1}
            disabled={disabled}
            value={value[field.key] ?? ""}
            onChange={(e) => set(field.key, e.target.value)}
          />
          <p className="muted" style={{ marginTop: "0.35rem", marginBottom: 0 }}>
            {field.hint}
          </p>
        </div>
      ))}
    </div>
  );
}
