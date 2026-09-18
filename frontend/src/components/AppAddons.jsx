export const DATABASES = [
  { id: "mysql", title: "MySQL", blurb: "Usual for Laravel apps" },
  { id: "postgres", title: "PostgreSQL", blurb: "If the app expects Postgres" },
  { id: "sqlite", title: "SQLite", blurb: "Simple file database on this server" },
  { id: "none", title: "None", blurb: "Keep an existing database, or attach one later" },
];

export function emptyAddons(seed = {}) {
  const packages = Array.isArray(seed.packages) ? seed.packages.join(" ") : seed.packages || "";
  const php = Array.isArray(seed.php_extensions)
    ? seed.php_extensions.join(" ")
    : seed.php_extensions || "";
  return {
    database: seed.database || "mysql",
    redis: Boolean(seed.redis),
    queue: Boolean(seed.queue),
    scheduler: Boolean(seed.scheduler),
    mailpit: Boolean(seed.mailpit),
    packages,
    php_extensions: php,
  };
}

function databaseLabel(id) {
  return DATABASES.find((db) => db.id === id)?.title || id;
}

export default function AppAddons({
  addons,
  onChange,
  runMigrations,
  onMigrations,
  migrateData,
  onMigrateData,
  sourceDatabase,
}) {
  function set(partial) {
    onChange({ ...addons, ...partial });
  }

  const target = addons.database;
  const sourceEngine = sourceDatabase?.engine || "";
  const sameEngine = sourceEngine && target && sourceEngine === target;
  const converting = sourceEngine && target && target !== "none" && sourceEngine !== target;

  return (
    <>
      <h2>Database</h2>
      <div className="provider-grid">
        {DATABASES.map((db) => (
          <button
            key={db.id}
            type="button"
            className={`provider-card${addons.database === db.id ? " selected" : ""}`}
            onClick={() => set({ database: db.id })}
          >
            <strong>{db.title}</strong>
            <span className="muted">{db.blurb}</span>
          </button>
        ))}
      </div>
      {onMigrateData && (
        <>
          {sourceDatabase && (
            <p className="muted">
              Current database: {sourceDatabase.label}
              {sourceDatabase.database ? ` “${sourceDatabase.database}”` : ""}
              {sourceDatabase.host ? ` on ${sourceDatabase.host}` : ""}
              {sourceDatabase.path ? ` (${sourceDatabase.path})` : ""}.
            </p>
          )}
          {target === "none" ? (
            <p className="muted">
              None keeps the current database running. A local dump is still written first, and
              files in storage/ are left on disk.
            </p>
          ) : (
            <label className="check-row">
              <input
                type="checkbox"
                checked={Boolean(migrateData)}
                onChange={(e) => onMigrateData(e.target.checked)}
              />
              Copy existing data into the new {databaseLabel(target)} database
            </label>
          )}
          {target !== "none" && migrateData && converting && (
            <p className="muted">
              This converts {sourceDatabase?.label || sourceEngine} → {databaseLabel(target)}:
              Laravel migrations create the new schema, then rows are copied table by table.
            </p>
          )}
          {target !== "none" && migrateData && sameEngine && (
            <p className="muted">
              The current dump is imported into the new {databaseLabel(target)} container so the
              app keeps its data.
            </p>
          )}
          <p className="muted">
            Before the switch starts, a local database backup is written under{" "}
            <code>.panel-backups/</code>. Host <code>storage/</code> is never deleted; it is copied
            into the new volumes.
          </p>
        </>
      )}
      <h2>Also run</h2>
      <label className="check-row">
        <input
          type="checkbox"
          checked={addons.redis}
          onChange={(e) => set({ redis: e.target.checked })}
        />
        Redis — cache, sessions, and queues
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={addons.queue}
          onChange={(e) => set({ queue: e.target.checked })}
        />
        Queue workers — background jobs (emails, imports)
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={addons.scheduler}
          onChange={(e) => set({ scheduler: e.target.checked })}
        />
        Scheduler — Laravel scheduled tasks
      </label>
      <label className="check-row">
        <input
          type="checkbox"
          checked={addons.mailpit}
          onChange={(e) => set({ mailpit: e.target.checked })}
        />
        Mailpit — catch outgoing mail (demos/staging, not real production SMTP)
      </label>
      {typeof runMigrations === "boolean" && onMigrations && (
        <label className="check-row">
          <input
            type="checkbox"
            checked={runMigrations}
            onChange={(e) => onMigrations(e.target.checked)}
          />
          Run database migrations after each deploy
        </label>
      )}
      <div className="field">
        <label>Extra apt packages (optional)</label>
        <input
          value={addons.packages || ""}
          onChange={(e) => set({ packages: e.target.value })}
          placeholder="tesseract-ocr imagemagick"
        />
        <span className="field-hint">Space-separated. Injected into the panel Dockerfile.</span>
      </div>
      <div className="field">
        <label>Extra PHP extensions (optional)</label>
        <input
          value={addons.php_extensions || ""}
          onChange={(e) => set({ php_extensions: e.target.value })}
          placeholder="imagick"
        />
        <span className="field-hint">Passed to install-php-extensions (for example imagick).</span>
      </div>
    </>
  );
}
