export function issueSeverityClass(issue, fallback = "") {
  if (issue?.severity === "error") return "danger";
  if (issue?.severity === "warning") return "warn";
  return fallback;
}

export function healthBadgeClass(health) {
  if (health === "unhealthy") return "danger";
  if (health === "healthy") return "ok";
  if (health === "starting") return "warn";
  return "";
}

export function IssueBlurb({ issue, fallback = "—" }) {
  if (!issue) return fallback;
  return (
    <div className="issue-blurb">
      <div className="issue-title">{issue.title}</div>
      {issue.summary && <div className="issue-summary">{issue.summary}</div>}
      {issue.suggestion && <div className="issue-suggestion">{issue.suggestion}</div>}
    </div>
  );
}

export default function IssuePanel({ name, issue, healthChecks = [] }) {
  if (!issue) return null;
  const tone = issueSeverityClass(issue, "warn");
  const lastOutput =
    issue.facts?.last_output ||
    [...healthChecks].reverse().find((c) => c.output)?.output;

  return (
    <div className={`issue-panel ${tone}`}>
      <h2>Why{name ? ` · ${name}` : ""}</h2>
      <div className="issue-kicker">
        <span className={`badge ${tone}`}>{issue.title}</span>
      </div>
      {issue.summary && <p className="issue-lead">{issue.summary}</p>}
      {issue.suggestion && <p className="issue-suggestion">{issue.suggestion}</p>}
      {lastOutput && (
        <>
          <div className="issue-output-label">Last check said</div>
          <pre className="issue-output">{lastOutput}</pre>
        </>
      )}
    </div>
  );
}
