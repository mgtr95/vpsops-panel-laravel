import { useMemo, useState } from "react";
import { api } from "../api/client";

const FEATURED = [
  { prefix: "drive", title: "Google Drive", blurb: "Docs, photos, and backups in Google" },
  { prefix: "onedrive", title: "OneDrive", blurb: "Personal or Microsoft 365" },
  { prefix: "dropbox", title: "Dropbox", blurb: "Dropbox files" },
  { prefix: "s3", title: "Amazon S3", blurb: "S3 and compatible storage" },
  { prefix: "b2", title: "Backblaze B2", blurb: "B2 buckets" },
  { prefix: "sftp", title: "SFTP", blurb: "A server over SSH" },
  { prefix: "webdav", title: "WebDAV", blurb: "Nextcloud, ownCloud, and similar" },
  { prefix: "local", title: "This server", blurb: "A folder on the VPS" },
];

const NAME_OK = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

function providerList(providers) {
  const arr = Array.isArray(providers) ? providers : providers?.providers || [];
  return arr
    .map((p) => {
      if (typeof p === "string") return { Prefix: p, Name: p, Description: "" };
      return p;
    })
    .filter((p) => p.Prefix || p.Name);
}

function optionTitle(option) {
  const help = option?.Help || "";
  const first = help.split("\n")[0].replace(/\.$/, "").trim();
  return first || option?.Name || "Option";
}

function optionBody(option) {
  const lines = (option?.Help || "").split("\n");
  return lines.slice(1).join("\n").trim();
}

function defaultAnswer(option) {
  if (!option) return "";
  if (option.Default === true || option.Value === true) return "true";
  if (option.Default === false || option.Value === false) return "false";
  if (option.Default != null && option.Default !== "") return String(option.Default);
  if (option.Value != null && option.Value !== "") return String(option.Value);
  return "";
}

export default function RcloneWizard({ remotes, providers, onDone, onCancel }) {
  const allProviders = useMemo(() => providerList(providers), [providers]);
  const [phase, setPhase] = useState("pick"); // pick | name | question | oauth | done
  const [type, setType] = useState("");
  const [name, setName] = useState("");
  const [askAll, setAskAll] = useState(false);
  const [search, setSearch] = useState("");
  const [showMore, setShowMore] = useState(false);
  const [rcloneState, setRcloneState] = useState("");
  const [option, setOption] = useState(null);
  const [oauth, setOauth] = useState(null);
  const [answer, setAnswer] = useState("");
  const [paste, setPaste] = useState("");
  const [hints, setHints] = useState({ client_id: "", client_secret: "", scope: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(false);
  const [testOk, setTestOk] = useState(null);

  const selectedMeta = allProviders.find((p) => (p.Prefix || p.Name) === type);
  const selectedTitle = FEATURED.find((f) => f.prefix === type)?.title || selectedMeta?.Name || type;

  const filteredMore = allProviders.filter((p) => {
    const prefix = p.Prefix || "";
    if (FEATURED.some((f) => f.prefix === prefix)) return false;
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      prefix.toLowerCase().includes(q) ||
      String(p.Name || "")
        .toLowerCase()
        .includes(q) ||
      String(p.Description || "")
        .toLowerCase()
        .includes(q)
    );
  });

  function nextHints(optName, value) {
    if (!["client_id", "client_secret", "scope"].includes(optName)) return hints;
    const updated = { ...hints, [optName]: value };
    setHints(updated);
    return updated;
  }

  async function sendAnswer(result, extraHints = hints) {
    setBusy(true);
    setError("");
    try {
      const data = await api("/rclone/wizard/step", {
        method: "POST",
        body: {
          name,
          type,
          state: rcloneState,
          result,
          all: askAll,
          ...extraHints,
        },
      });
      await applyStep(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function submitQuestion(value) {
    const optName = option?.Name || "";
    const result = value === undefined ? answer : value;
    await sendAnswer(result, nextHints(optName, result));
  }

  async function finishOauth() {
    setBusy(true);
    setError("");
    try {
      let result = paste.trim();
      if (oauth?.mode !== "code") {
        const data = await api("/rclone/wizard/oauth", {
          method: "POST",
          body: { type, redirect_url: paste, ...hints },
        });
        result = data.token;
      }
      const data = await api("/rclone/wizard/step", {
        method: "POST",
        body: {
          name,
          type,
          state: rcloneState,
          result,
          all: askAll,
          ...hints,
        },
      });
      await applyStep(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function applyStep(data) {
    if (data.error) setError(data.error);
    else setError("");
    setRcloneState(data.state || "");
    setOption(data.option || null);
    setOauth(data.oauth || null);
    if (data.done) {
      setPhase("done");
      await testRemote();
      return;
    }
    if (data.oauth?.auth_url) {
      setPaste("");
      setPhase("oauth");
      return;
    }
    const nextDefault = defaultAnswer(data.option);
    setAnswer(nextDefault);
    setPhase("question");
  }

  async function testRemote() {
    try {
      await api(`/rclone/list?fs=${encodeURIComponent(`${name}:`)}&remote=`);
      setTestOk(true);
    } catch {
      setTestOk(false);
    }
  }

  async function startWizard() {
    const trimmed = name.trim();
    if (!NAME_OK.test(trimmed)) {
      setError("Use a short name with letters, numbers, dash or underscore — e.g. gdrive.");
      return;
    }
    if (remotes.includes(trimmed)) {
      setError(`Something named “${trimmed}” already exists. Pick another name.`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const data = await api("/rclone/wizard/step", {
        method: "POST",
        body: { name: trimmed, type, all: askAll, ...hints },
      });
      setName(trimmed);
      setStarted(true);
      await applyStep(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (started && name) {
      try {
        await api("/rclone/wizard/cancel", { method: "POST", body: { name } });
      } catch {
        /* still leave */
      }
    }
    onCancel();
  }

  function pickType(prefix) {
    setType(prefix);
    setPhase("name");
    setError("");
    if (!name) {
      const hint = prefix === "drive" ? "gdrive" : prefix === "local" ? "local" : prefix.replace(/[^a-z0-9]+/gi, "");
      setName(hint);
    }
  }

  return (
    <div className="panel stack">
      <div className="toolbar" style={{ marginBottom: 0 }}>
        <h2 style={{ margin: 0, flex: 1 }}>Add storage</h2>
        <button onClick={cancel} disabled={busy}>
          Cancel
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {busy && <p className="muted">Working…</p>}

      {phase === "pick" && (
        <>
          <p className="muted">Choose where files should live. You can sign in with your browser for Google, Microsoft, and Dropbox.</p>
          <div className="provider-grid">
            {FEATURED.map((f) => (
              <button key={f.prefix} className="provider-card" onClick={() => pickType(f.prefix)}>
                <strong>{f.title}</strong>
                <span className="muted">{f.blurb}</span>
              </button>
            ))}
          </div>
          <button onClick={() => setShowMore((v) => !v)}>{showMore ? "Hide more types" : "More storage types…"}</button>
          {showMore && (
            <>
              <div className="field">
                <label>Search</label>
                <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Wasabi, Mega, crypt…" />
              </div>
              <div className="provider-grid">
                {filteredMore.map((p) => {
                  const prefix = p.Prefix || p.Name;
                  return (
                    <button key={prefix} className="provider-card" onClick={() => pickType(prefix)}>
                      <strong>{p.Name || prefix}</strong>
                      <span className="muted">{p.Description || prefix}</span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </>
      )}

      {phase === "name" && (
        <>
          <p className="muted">
            Connecting to <strong>{selectedTitle}</strong>. Give it a short nickname you will recognize later.
          </p>
          <div className="field">
            <label>Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="gdrive"
              autoFocus
            />
          </div>
          <label className="check-row">
            <input type="checkbox" checked={askAll} onChange={(e) => setAskAll(e.target.checked)} />
            Show advanced options
          </label>
          <div className="row-actions">
            <button onClick={() => setPhase("pick")}>Back</button>
            <button className="primary" disabled={!name.trim() || busy} onClick={startWizard}>
              Continue
            </button>
          </div>
        </>
      )}

      {phase === "question" && option && (
        <QuestionStep
          option={option}
          answer={answer}
          setAnswer={setAnswer}
          busy={busy}
          onNext={() => submitQuestion()}
          onPick={(value) => submitQuestion(value)}
        />
      )}

      {phase === "oauth" && oauth && (
        <>
          <h3 style={{ margin: "0.25rem 0" }}>{oauth.sign_in_label || "Sign in"}</h3>
          <ol className="oauth-steps">
            <li>
              Click the button. A new tab opens so you can allow access.
              <div className="row-actions" style={{ marginTop: 8 }}>
                <button
                  className="primary"
                  type="button"
                  onClick={() => window.open(oauth.auth_url, "_blank", "noopener,noreferrer")}
                >
                  {oauth.sign_in_label || "Open sign-in page"}
                </button>
              </div>
            </li>
            <li>Sign in and click Allow.</li>
            <li>
              Your browser will say the page can’t be reached — that is expected. Copy the full address from the
              address bar and paste it below.
            </li>
          </ol>
          <div className="field">
            <label>Address after signing in</label>
            <textarea
              value={paste}
              onChange={(e) => setPaste(e.target.value)}
              placeholder="http://127.0.0.1:53682/?code=…"
            />
          </div>
          <div className="row-actions">
            <button className="primary" disabled={!paste.trim() || busy} onClick={finishOauth}>
              I’ve signed in
            </button>
          </div>
        </>
      )}

      {phase === "done" && (
        <>
          <p>
            <strong>{name}</strong> is ready
            {testOk === true ? " and we could list its files." : testOk === false ? ", but listing files failed — you can still try browsing it." : "."}
          </p>
          <div className="row-actions">
            <button className="primary" onClick={() => onDone(name)}>
              Browse files
            </button>
            <button onClick={() => onDone(null)}>Back to storage</button>
          </div>
        </>
      )}
    </div>
  );
}

function QuestionStep({ option, answer, setAnswer, busy, onNext, onPick }) {
  const examples = option.Examples || [];
  const exclusive = option.Exclusive;
  const isBool = String(option.Type || "").toLowerCase() === "bool" || option.Name?.startsWith("config_is_");
  const isPassword = option.IsPassword;
  const body = optionBody(option);

  return (
    <>
      <h3 style={{ margin: "0.25rem 0" }}>{optionTitle(option)}</h3>
      {body && <pre className="wizard-help">{body}</pre>}
      {isBool ? (
        <div className="row-actions">
          <button className="primary" disabled={busy} onClick={() => onPick("true")}>
            Yes
          </button>
          <button disabled={busy} onClick={() => onPick("false")}>
            No
          </button>
        </div>
      ) : (
        <>
          {examples.length > 0 && (
            <div className="example-grid">
              {examples.map((ex) => (
                <button
                  key={String(ex.Value)}
                  className={answer === String(ex.Value) ? "provider-card selected" : "provider-card"}
                  disabled={busy}
                  onClick={() => {
                    setAnswer(String(ex.Value));
                    if (exclusive) onPick(String(ex.Value));
                  }}
                >
                  <strong>{ex.Value}</strong>
                  {ex.Help && <span className="muted">{ex.Help}</span>}
                </button>
              ))}
            </div>
          )}
          {(!exclusive || examples.length === 0) && (
            <div className="field">
              <label>{isPassword ? "Password" : "Value"}</label>
              <input
                type={isPassword ? "password" : "text"}
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder={option.Required ? "Required" : "Leave blank for default"}
                onKeyDown={(e) => {
                  if (e.key === "Enter") onNext();
                }}
              />
            </div>
          )}
          {!(exclusive && examples.length > 0) && (
            <button className="primary" disabled={busy || (option.Required && !answer)} onClick={onNext}>
              Next
            </button>
          )}
        </>
      )}
    </>
  );
}
