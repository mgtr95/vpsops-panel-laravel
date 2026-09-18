import { useEffect, useState } from "react";
import { api } from "../api/client";

export function PipelineView({ pipeline, onRefresh }) {
  if (!pipeline) return <p className="muted">No deployment yet.</p>;
  const steps = pipeline.steps || [];
  const commit = pipeline.commit || {};

  return (
    <div className="stack">
      <div className="toolbar toolbar--actions" style={{ marginBottom: 0 }}>
        <div>
          <span className={`badge ${badgeClass(pipeline.status)}`}>{pipeline.status}</span>
          {commit.sha && (
            <span className="muted" style={{ marginLeft: 8, fontSize: 13 }}>
              {String(commit.sha).slice(0, 7)} {commit.message || ""}
            </span>
          )}
        </div>
        {onRefresh && (
          <button type="button" onClick={onRefresh}>
            Refresh
          </button>
        )}
      </div>
      <ol className="pipeline-steps">
        {steps.map((step) => (
          <li key={step.id} className={`pipeline-step pipeline-step--${step.status || "pending"}`}>
            <div className="pipeline-step-head">
              <strong>{step.title}</strong>
              <span className={`badge ${badgeClass(step.status)}`}>{step.status}</span>
            </div>
            {step.log ? <pre className="log pipeline-log">{step.log}</pre> : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

export function badgeClass(status) {
  if (status === "success" || status === "skipped") return "ok";
  if (status === "failed") return "danger";
  if (status === "running" || status === "queued" || status === "pending") return "warn";
  return "";
}

export function usePipeline(pipelineId) {
  const [pipeline, setPipeline] = useState(null);

  useEffect(() => {
    if (!pipelineId) return undefined;
    let cancelled = false;
    async function load() {
      try {
        const data = await api(`/apps/pipelines/${pipelineId}`);
        if (!cancelled) setPipeline(data);
        return data;
      } catch {
        return null;
      }
    }
    load();
    const t = setInterval(async () => {
      const data = await load();
      if (data && (data.status === "success" || data.status === "failed")) {
        clearInterval(t);
      }
    }, 1200);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [pipelineId]);

  return pipeline;
}
