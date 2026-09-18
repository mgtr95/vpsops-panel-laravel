import { Link } from "react-router-dom";

export function parseExtraEmails(text) {
  return String(text || "")
    .split(/[,;\n]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function extraEmailsText(emails) {
  return (emails || []).join(", ");
}

export function hasRecipients(notify) {
  return (notify?.recipient_ids || []).length > 0 || (notify?.to_emails || []).length > 0;
}

export default function NotifyFields({ recipients, notify, onChange }) {
  const recipientIds = new Set(notify?.recipient_ids || []);
  const toggleRecipient = (id) => {
    const next = new Set(recipientIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange({ ...(notify || {}), recipient_ids: [...next] });
  };
  return (
    <>
      <div className="field">
        <label>Recipients</label>
        {(recipients || []).length > 0 ? (
          <div className="stack">
            {recipients.map((r) => (
              <label key={r.id} className="check-row">
                <input
                  type="checkbox"
                  checked={recipientIds.has(r.id)}
                  onChange={() => toggleRecipient(r.id)}
                />
                {r.name} <span className="muted">({r.email})</span>
              </label>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            No saved recipients yet. Add them on the <Link to="/mail">Mail</Link> page.
          </p>
        )}
      </div>
      <div className="field">
        <label>Extra email addresses</label>
        <input
          value={extraEmailsText(notify?.to_emails)}
          onChange={(e) => onChange({ ...(notify || {}), to_emails: parseExtraEmails(e.target.value) })}
          placeholder="ops@example.com, dev@example.com"
        />
        <span className="field-hint">
          Comma-separated one-off addresses in addition to saved recipients. Pick at least one recipient or extra
          email.
        </span>
      </div>
    </>
  );
}
