import html
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import core.analyzer
import core.fetcher
import core.pr_creator
import core.tracker


@asynccontextmanager
async def lifespan(app: FastAPI):
    core.tracker.on_fix_callback = _record_fix
    core.tracker.load_registry()
    poller_task = asyncio.create_task(core.tracker.poll_forever(30))
    try:
        yield
    finally:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Patch", lifespan=lifespan)


class IssuePayload(BaseModel):
    repo_name: str
    file_path: str
    issue_title: str
    error_context: str
    ref: str = "main"


def _dashboard_html() -> str:
    return DASHBOARD_TEMPLATE


# ---------------------------------------------------------------------------
# Dashboard: ZERO external dependencies. Plain HTML + inline <style> + inline
# vanilla <script>. No Tailwind, no Alpine, no Lucide, no Prism, no CDN -- so
# nothing can fail to load or half-hydrate in the browser.
# ---------------------------------------------------------------------------

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>Patch</title>
<style>
  :root {
    --bg: #0f172a;           /* slate-950 */
    --panel: #1e293b;        /* slate-800 */
    --border: #334155;       /* slate-700 */
    --text: #f1f5f9;         /* slate-100 */
    --muted: #94a3b8;        /* slate-400 */
    --faint: #64748b;        /* slate-500 */
    --accent: #34d399;       /* emerald-400 */
    --accent-dim: rgba(52, 211, 153, 0.12);
    --blue: #38bdf8;         /* sky-400 */
    --rose: #fb7185;         /* rose-400 */
    --mono: 'Consolas', 'Cascadia Code', 'JetBrains Mono', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
    min-height: 100vh; line-height: 1.5;
  }
  .wrap { max-width: 860px; margin: 0 auto; padding: 40px 20px; position: relative; z-index: 1; }

  /* Header */
  header { display: flex; justify-content: space-between; align-items: center;
           border-bottom: 1px solid var(--border); padding-bottom: 20px; margin-bottom: 28px; }
  h1 { font-size: 22px; font-weight: 600; letter-spacing: -0.5px; color: var(--text); }
  .tagline { color: var(--faint); font-size: 13px; margin-top: 2px; }
  .pill {
    border: 1px solid rgba(52,211,153,.4); color: var(--accent);
    background: var(--accent-dim); border-radius: 999px; padding: 4px 12px;
    font-size: 11px; font-family: var(--mono);
    animation: glow 1.8s ease-in-out infinite;
  }
  @keyframes glow {
    0%, 100% { box-shadow: 0 0 6px rgba(52,211,153,.25); }
    50%      { box-shadow: 0 0 14px rgba(52,211,153,.55); }
  }

  /* Cards */
  .card {
    background: rgba(30,41,59,.6); border: 1px solid var(--border);
    border-radius: 10px; padding: 22px; margin-bottom: 22px;
  }
  .card h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: 1.4px;
    color: var(--faint); margin-bottom: 14px; font-weight: 600;
    display: flex; align-items: center; gap: 8px;
  }
  .row { display: flex; gap: 10px; }
  .row > * { flex: 1; }
  label { display: block; margin-bottom: 14px; }
  label span { display: block; font-size: 11px; color: var(--muted); margin-bottom: 5px; font-weight: 500; }
  input, textarea {
    width: 100%; background: rgba(15,23,42,.7); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 9px 12px; font-size: 13px; font-family: inherit;
    outline: none; transition: border-color .15s;
  }
  input:focus, textarea:focus { border-color: var(--accent); }
  input.mono, textarea.mono { font-family: var(--mono); }
  textarea { resize: vertical; min-height: 120px; }

  button {
    cursor: pointer; border: 1px solid rgba(52,211,153,.5);
    background: var(--accent-dim); color: var(--accent);
    border-radius: 8px; padding: 10px 18px; font-size: 13px; font-weight: 600;
    transition: background .15s; width: 100%;
  }
  button:hover { background: rgba(52,211,153,.22); }
  button:disabled { opacity: .4; cursor: not-allowed; }
  button.small { width: auto; padding: 6px 14px; font-size: 11px; }
  button.blue { border-color: rgba(56,189,248,.5); background: rgba(56,189,248,.12); color: var(--blue); }
  button.blue:hover { background: rgba(56,189,248,.22); }

  .hint { font-size: 11px; color: var(--faint); margin-top: 8px; }
  .error-text { color: var(--rose); font-size: 12px; margin-top: 8px; display: none; }
  .error-text.show { display: block; }
  .ok-text { color: var(--accent); font-size: 12px; margin-top: 8px; display: none; }
  .ok-text.show { display: block; }

  /* Log box */
  #logbox {
    background: rgba(0,0,0,.4); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px; height: 200px; overflow-y: auto; display: none;
  }
  #logbox.show { display: block; }
  .logline { font-family: var(--mono); font-size: 12px; padding: 2px 0; color: var(--muted); }
  .logline .k { display: inline-block; width: 16px; }
  .logline.ok .k { color: var(--accent); }
  .logline.err .k { color: var(--rose); }
  .logline.run .k { color: var(--blue); }
  .spin { display: inline-block; animation: rotate 1.4s linear infinite; }
  @keyframes rotate { from { transform: rotate(0deg);} to { transform: rotate(360deg);} }

  /* Diff viewer */
  #diffbox { display: none; }
  #diffbox.show { display: block; }
  #diffbox pre {
    background: #0b1120; border: 1px solid var(--border); border-radius: 10px;
    padding: 14px; font-family: var(--mono); font-size: 12px;
    max-height: 320px; overflow: auto; white-space: pre;
  }
  .d-add { color: var(--accent); background: rgba(52,211,153,.08); display: block; }
  .d-del { color: var(--rose); background: rgba(251,113,133,.08); display: block; }
  .d-hdr { color: var(--blue); font-weight: 600; display: block; }
  .d-ctx { display: block; }

  /* PR button */
  #prbox { display: none; margin-top: 12px; }
  #prbox.show { display: block; }
  #prbox a {
    display: flex; justify-content: center; align-items: center; gap: 8px;
    border: 1px solid rgba(52,211,153,.5); background: var(--accent-dim);
    color: var(--accent); border-radius: 8px; padding: 11px 18px;
    font-size: 13px; font-weight: 600; text-decoration: none;
  }
  #prbox a:hover { background: rgba(52,211,153,.22); }

  /* Tracked repos */
  .repo-item {
    display: flex; justify-content: space-between; align-items: center;
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 14px; margin-bottom: 8px;
  }
  .repo-item .name { font-family: var(--mono); font-size: 13px; }
  .badge-active {
    display: inline-flex; align-items: center; gap: 6px;
    border: 1px solid rgba(52,211,153,.4); background: var(--accent-dim);
    color: var(--accent); border-radius: 999px; padding: 2px 10px;
    font-size: 11px; font-family: var(--mono); margin-right: 10px;
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent);
         display: inline-block; animation: glow 1.8s infinite; }
  .empty { color: var(--faint); font-size: 13px; }
  .meta { font-family: var(--mono); font-size: 10px; color: var(--faint); margin-top: 2px; }

  /* Fixes feed */
  .fix-item { border: 1px solid var(--border); border-radius: 8px;
              padding: 12px 14px; margin-bottom: 8px; }
  .fix-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .fix-title { font-size: 13px; color: var(--text); font-weight: 600; }
  .fix-meta { font-family: var(--mono); font-size: 10px; color: var(--faint); margin-top: 3px; }
  .pr-link {
    flex-shrink: 0; font-family: var(--mono); font-size: 11px;
    border: 1px solid rgba(52,211,153,.5); background: var(--accent-dim);
    color: var(--accent); border-radius: 6px; padding: 5px 10px; text-decoration: none;
  }
  .pr-link:hover { background: rgba(52,211,153,.22); }
  details summary { cursor: pointer; font-size: 11px; color: var(--faint); margin-top: 8px; }
  details pre { margin-top: 8px; background: #0b1120; border: 1px solid var(--border);
                border-radius: 8px; padding: 10px; font-size: 11px; max-height: 220px; overflow: auto; }

  footer { margin-top: 30px; padding-top: 18px; border-top: 1px solid var(--border);
           text-align: center; font-size: 11px; color: var(--faint); }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>Patch</h1>
      <div class="tagline">Autonomous GitHub issue triage &amp; patch generation.</div>
    </div>
    <div class="pill">pipeline&nbsp;ready</div>
  </header>

  <!-- ============ Manual patch generation ============ -->
  <div class="card" id="manual-card">
    <h2>Manual patch generation</h2>
    <form id="patchForm">
      <div class="row">
        <label><span>GitHub repository</span>
          <input class="mono" id="repo" placeholder="owner/repo" required>
        </label>
        <label><span>Target file path</span>
          <input class="mono" id="filePath" placeholder="main.py" required>
        </label>
      </div>
      <label><span>Issue title</span>
        <input id="issueTitle" placeholder="Crash when name is None" required>
      </label>
      <label><span>Description / traceback</span>
        <textarea class="mono" id="errContext" rows="5" required
          placeholder="Paste the error, repro steps, or what should change…"></textarea>
      </label>
      <button id="submitBtn" type="submit">Generate Patch &amp; Open PR</button>
    </form>
  </div>

  <!-- ============ Execution log ============ -->
  <div class="card" id="logCard" style="display:none">
    <h2>Execution log</h2>
    <div id="logbox" class="show"></div>
  </div>

  <!-- ============ Diff + PR link ============ -->
  <div id="diffbox">
    <div class="card">
      <h2>Syntax-highlighted diff</h2>
      <pre id="diffPre"></pre>
    </div>
    <div id="prbox">
      <a id="prLink" href="#" target="_blank" rel="noopener">View PR on GitHub →</a>
    </div>
  </div>

  <!-- ============ Auto-track ============ -->
  <div class="card">
    <h2>Auto-track repository <span style="color:var(--blue)">&nbsp;·&nbsp;every 30s</span></h2>
    <div class="row">
      <input class="mono" id="trackRepo" placeholder="owner/repo"
             style="font-family: var(--mono); font-size: 13px;">
      <div style="flex:0 0 auto;"><button class="blue" id="trackBtn">Start Tracking</button></div>
    </div>
    <div id="trackMsg" class="error-text"></div>
    <div id="trackOkMsg" class="ok-text"></div>
  </div>

  <div class="card">
    <h2>Currently monitored repositories</h2>
    <div id="repoList"><div class="empty">No repositories tracked yet.</div></div>
  </div>

  <div class="card">
    <h2>Recent automated fixes</h2>
    <div id="fixList"><div class="empty">No fixes generated yet this session.</div></div>
  </div>

  <footer>Patch · powered by Groq &amp; PyGithub</footer>
</div>

<script>
"use strict";
// ---------------------------------------------------------------- state ---
var state = { tracked: [], fixes: [] };

function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function showLog(kind, text) {
  var box = $("logbox");
  $("logCard").style.display = "";
  box.classList.add("show");
  var line = document.createElement("div");
  line.className = "logline " + kind;
  var k = document.createElement("span");
  k.className = "k";
  k.textContent = kind === "ok" ? "✓" : kind === "err" ? "✕" : "⟳";
  if (kind === "run") k.classList.add("spin");
  line.appendChild(k);
  line.appendChild(document.createTextNode(" " + text));
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}
function clearLog() {
  $("logbox").innerHTML = "";
  $("logCard").style.display = "none";
}

// Simple diff syntax highlighting: colorize +, -, @@, ---, +++
function highlightDiff(diffText) {
  var out = [];
  var lines = String(diffText).split("\\n");
  for (var i = 0; i < lines.length; i++) {
    var l = esc(lines[i]);
    var cls = "";
    if (/^\\+/.test(lines[i]) && !/^\\+\\+\\+/.test(lines[i])) cls = "d-add";
    else if (/^-/.test(lines[i]) && !/^---/.test(lines[i])) cls = "d-del";
    else if (/^@@/.test(lines[i]) || /^---/.test(lines[i]) || /^\\+\\+\\+/.test(lines[i])) cls = "d-hdr";
    else cls = "d-ctx";
    out.push('<code class="' + cls + '">' + l + "</code>");
  }
  return out.join("\\n");
}

// ---------------------------------------------------- manual pipeline ----
$("patchForm").addEventListener("submit", function (e) {
  e.preventDefault();
  var repo = $("repo").value.trim();
  var path = $("filePath").value.trim();
  var title = $("issueTitle").value.trim();
  var ctxv = $("errContext").value.trim();
  if (!repo || !path || !title || !ctxv) {
    alert("All fields are required");
    return;
  }

  $("submitBtn").disabled = true;
  clearLog();
  $("diffbox").classList.remove("show");
  $("prbox").classList.remove("show");
  showLog("run", "Starting pipeline for " + repo + " › " + path + " …");
  showLog("run", "Fetching file, asking Groq, forking, opening PR …");

  fetch("/webhook/trigger-fix", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repo_name: repo, file_path: path,
      issue_title: title, error_context: ctxv
    })
  })
  .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
  .then(function (res) {
    $("submitBtn").disabled = false;
    if (res.ok) {
      showLog("ok", res.j.pull_request_url);
      showLog("ok", "Pull request opened successfully.");
      renderDiffBox(res.j.patch || "", res.j.pull_request_url);
    } else {
      showLog("err", String(res.j.detail || "unknown error"));
    }
  })
  .catch(function (e) {
    $("submitBtn").disabled = false;
    showLog("err", String(e));
  });
});

function renderDiffBox(patch, prUrl) {
  $("diffbox").classList.add("show");
  $("diffPre").innerHTML = highlightDiff(patch);
  $("prLink").href = prUrl;
  $("prLink").textContent = "View PR on GitHub → " + prUrl.split("/pull/")[1];
}

// ---------------------------------------------------------- tracking ------
$("trackBtn").addEventListener("click", function () {
  var repo = $("trackRepo").value.trim();
  var errMsg = $("trackMsg"), okMsg = $("trackOkMsg");
  errMsg.classList.remove("show"); okMsg.classList.remove("show");
  if (!repo) { errMsg.textContent = "Enter owner/repo first."; errMsg.classList.add("show"); return; }
  $("trackBtn").disabled = true;
  fetch("/api/track", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo: repo })
  })
  .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
  .then(function (res) {
    $("trackBtn").disabled = false;
    if (res.ok) {
      okMsg.textContent = "now tracking " + repo + " — auto-triage armed";
      okMsg.classList.add("show");
      $("trackRepo").value = "";
    } else {
      errMsg.textContent = res.j.detail || "Failed to track.";
      errMsg.classList.add("show");
    }
    loadTracked();
  })
  .catch(function (e) { $("trackBtn").disabled = false; errMsg.textContent = String(e); errMsg.classList.add("show"); });
});

function loadTracked() {
  fetch("/api/tracked").then(function (r) { return r.json(); }).then(function (j) {
    state.tracked = j.tracked || [];
    renderTracked();
  }).catch(function () { /* best-effort */ });
}

function renderTracked() {
  var list = $("repoList");
  list.innerHTML = "";
  if (!state.tracked.length) {
    list.innerHTML = '<div class="empty">No repositories tracked yet.</div>';
    return;
  }
  state.tracked.forEach(function (rec) {
    var row = document.createElement("div");
    row.className = "repo-item";
    var t = new Date(rec.added_at).toLocaleTimeString();
    row.innerHTML =
      '<div><span class="badge-active"><span class="dot"></span>Active</span>' +
      '<span class="name">' + esc(rec.repo) + '</span>' +
      '<div class="meta">since ' + esc(t) + '</div></div>' +
      '<div><button class="small" data-repo="' + esc(rec.repo) + '">untrack</button></div>';
    var btn = row.querySelector("button");
    btn.addEventListener("click", function () { untrack(rec.repo); });
    list.appendChild(row);
  });
}

function untrack(repo) {
  var parts = repo.split("/");
  fetch("/api/track/" + parts[0] + "/" + parts[1], { method: "DELETE" })
    .then(function () { loadTracked(); });
}

// ---------------------------------------------------------- fixes feed ----
function loadFixes() {
  fetch("/api/fixes").then(function (r) { return r.json(); }).then(function (j) {
    state.fixes = j.fixes || [];
    renderFixes();
  }).catch(function () { /* best-effort */ });
}

function renderFixes() {
  var list = $("fixList");
  list.innerHTML = "";
  if (!state.fixes.length) {
    list.innerHTML = '<div class="empty">No fixes generated yet this session.</div>';
    return;
  }
  state.fixes.forEach(function (f) {
    var item = document.createElement("div");
    item.className = "fix-item";
    item.innerHTML =
      '<div class="fix-head">' +
        '<div><div class="fix-title">' + esc(f.issue_title) + '</div>' +
        '<div class="fix-meta">' + esc(f.repo) + " · " + esc(f.file_path) +
        ' · ' + esc(String(f.timestamp).slice(0, 16).replace("T", " ")) + '</div></div>' +
        '<a class="pr-link" target="_blank" href="' + esc(f.pr_url) + '">PR →</a>' +
      '</div>' +
      '<details><summary>show diff</summary>' +
      '<pre>' + (f.patch ? esc(f.patch.slice(0, 2000)) : "(fetched live)") + '</pre></details>';
    list.appendChild(item);
  });
}

// ------------------------------------------------- refresh loop -----------
function refreshAll() { loadTracked(); loadFixes(); }
setInterval(refreshAll, 10000);

// First paint: populate tracked + fixes from the live backend.
refreshAll();
</script>
</body>
</html>
"""


@app.get("/")
def dashboard() -> HTMLResponse:
    return HTMLResponse(content=_dashboard_html(), status_code=200)


class TrackRequest(BaseModel):
    repo: str


# In-memory feed of automated fixes (newest first). Process-lifetime only.
_fixes_feed: list[dict] = []


def _record_fix(
    repo: str, file_path: str, issue_title: str, patch: str, pr_url: str,
) -> None:
    _fixes_feed.insert(
        0,
        {
            "repo": repo,
            "file_path": file_path,
            "issue_title": issue_title,
            "patch": patch,
            "pr_url": pr_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    del _fixes_feed[50:]


@app.get("/api/fixes")
def api_fixes():
    return {"fixes": _fixes_feed}


@app.post("/api/track")
def api_track(req: TrackRequest):
    try:
        entry = core.tracker.track(req.repo)
        return {"status": "tracked", "entry": entry}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/track/{owner}/{repo}")
def api_untrack(owner: str, repo: str):
    removed = core.tracker.untrack(f"{owner}/{repo}")
    if not removed:
        raise HTTPException(status_code=404, detail=f"{owner}/{repo} is not tracked")
    return {"status": "untracked", "repo": f"{owner}/{repo}"}


@app.get("/api/tracked")
def api_tracked():
    return {"tracked": core.tracker.list_tracked()}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/webhook/trigger-fix")
def trigger_fix(payload: IssuePayload):
    try:
        file_content = core.fetcher.get_file_content(
            payload.repo_name, payload.file_path, ref=payload.ref
        )
        patch = core.analyzer.generate_patch(
            payload.file_path, file_content, payload.error_context
        )
        pull_request_url = core.pr_creator.apply_patch_and_create_pr(
            payload.repo_name, payload.file_path, patch, ref=payload.ref,
            original_content=file_content,
        )
        _record_fix(
            payload.repo_name, payload.file_path, payload.issue_title,
            patch, pull_request_url,
        )
        return {"status": "success", "pull_request_url": pull_request_url, "patch": patch}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - single failure point for the webhook
        raise HTTPException(status_code=500, detail=str(exc)) from exc
