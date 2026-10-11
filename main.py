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
    # Auto-triage feed: hook the tracker so polled issues land in /api/fixes.
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
    return DASHBOARD_TEMPLATE.replace("__ESCAPED_TITLE__", html.escape(app.title))


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="color-scheme" content="dark" />
  __ESCAPED_TITLE__
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: { extend: { fontFamily: { mono: ['"JetBrains Mono"', 'monospace'] } } }
    };
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <script src="https://unpkg.com/lucide@latest"></script>
  <link href="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/themes/prism-tomorrow.min.css" rel="stylesheet" />
  <script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/prism.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/prismjs@1.29.0/components/prism-diff.min.js"></script>
  <style>
    [x-cloak] { display: none !important; }
    @keyframes pulseGlow {
      0%, 100% { box-shadow: 0 0 8px 0 rgba(52, 211, 153, 0.35); }
      50%      { box-shadow: 0 0 16px 2px rgba(52, 211, 153, 0.55); }
    }
    .badge-glow  { animation: pulseGlow 1.6s ease-in-out infinite; }
    .grain::before {
      content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
      background: radial-gradient(ellipse at 20% 10%, rgba(56, 189, 248, 0.07), transparent 55%),
                  radial-gradient(ellipse at 85% 90%, rgba(52, 211, 153, 0.06), transparent 50%);
    }
    pre[class*="language-"] {
      background: #1b1f2a !important; border: 1px solid #334155; border-radius: 0.5rem;
      font-size: 0.8rem; max-height: 22rem; overflow: auto;
    }
    .token.regex .double-slash { color: #f59e0b !important; }
  </style>
</head>

<body x-data="patch()" class="grain min-h-screen bg-slate-950 text-slate-100
     font-sans antialiased selection:bg-emerald-500/30">

<div class="relative z-10 mx-auto max-w-4xl px-6 py-12">

  <!-- Header -->
  <header class="mb-10 flex items-center justify-between border-b border-slate-800 pb-6">
    <div>
      <h1 class="font-mono text-2xl font-semibold tracking-tight text-slate-50">Patch</h1>
      <p class="mt-1 text-sm text-slate-500">Autonomous GitHub issue triage &amp; patch generation.</p>
    </div>
    <span class="rounded-full border border-emerald-700/60 bg-emerald-900/20 px-3 py-1
                 text-[11px] font-medium tracking-wide text-emerald-400 badge-glow">
      pipeline&nbsp;ready
    </span>
  </header>

  <!-- Run state badge -->
  <div class="mb-6">
    <template x-if="state !== 'idle'">
      <span class="inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[11px] font-mono"
            :class="{
              'border-sky-700/60 bg-sky-900/20 text-sky-400 badge-glow':      state === 'running',
              'border-emerald-700/60 bg-emerald-900/20 text-emerald-400':     state === 'success',
              'border-rose-700/60 bg-rose-900/20 text-rose-400':              state === 'error',
            }">
        <span x-text="state"></span>
      </span>
    </template>
  </div>

  <!-- Form -->
  <form class="space-y-5 rounded-xl border border-slate-800 bg-slate-900/60 p-6"
        @submit.prevent="run()">

    <div class="grid gap-5 sm:grid-cols-2">
      <label class="block">
        <span class="mb-1.5 block text-xs font-medium text-slate-400">GitHub repository</span>
        <div class="relative">
          <i data-lucide="github" class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"></i>
          <input x-model.trim="form.repo" type="text" placeholder="owner/repo" required
                 class="w-full rounded-lg border border-slate-700 bg-slate-800/70 py-2.5 pl-10 pr-3
                        text-sm font-mono placeholder-slate-600 outline-none
                        focus:border-emerald-600 focus:ring-1 focus:ring-emerald-600/40" />
        </div>
      </label>

      <label class="block">
        <span class="mb-1.5 block text-xs font-medium text-slate-400">Target file path</span>
        <div class="relative">
          <i data-lucide="file-code-2" class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"></i>
          <input x-model.trim="form.path" type="text" placeholder="main.py" required
                 class="w-full rounded-lg border border-slate-700 bg-slate-800/70 py-2.5 pl-10 pr-3
                        text-sm font-mono placeholder-slate-600 outline-none
                        focus:border-emerald-600 focus:ring-1 focus:ring-emerald-600/40" />
        </div>
      </label>
    </div>

    <label class="block">
      <span class="mb-1.5 block text-xs font-medium text-slate-400">Issue title</span>
      <div class="relative">
        <i data-lucide="bug" class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"></i>
        <input x-model.trim="form.title" type="text" placeholder="Crash when name is None" required
               class="w-full rounded-lg border border-slate-700 bg-slate-800/70 py-2.5 pl-10 pr-3
                      text-sm placeholder-slate-600 outline-none
                      focus:border-emerald-600 focus:ring-1 focus:ring-emerald-600/40" />
      </div>
    </label>

    <label class="block">
      <span class="mb-1.5 block text-xs font-medium text-slate-400">Description / traceback</span>
      <textarea x-model.trim="form.context" rows="6" required
                placeholder="Paste the error, repro steps, or what should change…"
                class="w-full rounded-lg border border-slate-700 bg-slate-800/70 px-3 py-2.5
                       text-sm font-mono placeholder-slate-600 outline-none
                       focus:border-emerald-600 focus:ring-1 focus:ring-emerald-600/40"></textarea>
    </label>

    <button type="submit" :disabled="state === 'running'"
            class="flex w-full items-center justify-center gap-2 rounded-lg border
                   border-emerald-600/50 bg-emerald-600/10 py-3 text-sm font-medium text-emerald-400
                   transition hover:bg-emerald-600/20 disabled:cursor-not-allowed disabled:opacity-40">
      <i data-lucide="git-pull-request-arrow" class="h-4 w-4"></i>
      <span x-text="state === 'running' ? 'Running pipeline…' : 'Generate Patch &amp; Open PR'"></span>
    </button>
  </form>

  <!-- Execution log -->
  <template x-if="logs.length">
    <section class="mt-8">
      <h2 class="mb-3 font-mono text-xs uppercase tracking-widest text-slate-500">Execution log</h2>
      <div class="h-64 overflow-y-auto rounded-xl border border-slate-800 bg-black/40 p-4"
           x-ref="logbox">
        <template x-for="(line, i) in logs" :key="i">
          <div class="flex items-start gap-3 py-1 font-mono text-[13px] leading-5"
               :class="line.kind === 'err' ? 'text-rose-400' : 'text-slate-400'">
            <template x-if="line.kind === 'ok'">
              <i data-lucide="check-circle-2" class="mt-0.5 h-4 w-4 shrink-0 text-emerald-500"></i>
            </template>
            <template x-if="line.kind === 'err'">
              <i data-lucide="alert-triangle" class="mt-0.5 h-4 w-4 shrink-0 text-rose-500"></i>
            </template>
            <template x-if="line.kind === 'run'">
              <i data-lucide="loader-2" class="mt-0.5 h-4 w-4 shrink-0 animate-spin text-sky-400"></i>
            </template>
            <span x-text="line.text"></span>
          </div>
        </template>
      </div>
    </section>
  </template>

  <!-- Diff viewer + PR link -->
  <template x-if="diff">
    <section class="mt-8">
      <h2 class="mb-3 font-mono text-xs uppercase tracking-widest text-slate-500">Generated diff</h2>
      <pre class="language-diff"><code class="language-diff" x-html="highlightedDiff"></code></pre>

      <a :href="prUrl" target="_blank" rel="noopener"
         class="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-lg border
                border-emerald-600/50 bg-emerald-600/10 py-3 text-sm font-medium text-emerald-400
                transition hover:bg-emerald-600/20">
        <i data-lucide="external-link" class="h-4 w-4"></i>
        <span x-text="'Open Pull Request on GitHub — ' + (prUrl.split('/pull/')[1] ?? '')"></span>
      </a>
    </section>
  </template>

  <!-- Auto-Track Repository -->
  <section class="mt-10 rounded-xl border border-slate-800 bg-slate-900/60 p-6">
    <div class="mb-4 flex items-center justify-between">
      <h2 class="flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-slate-500">
        <i data-lucide="radio-tower" class="h-4 w-4 text-sky-400"></i> Auto-Track Repository
      </h2>
      <span class="text-[11px] text-slate-600">polls every 30s</span>
    </div>
    <div class="flex gap-3">
      <div class="relative flex-1">
        <i data-lucide="github" class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"></i>
        <input x-model.trim="trackRepo" type="text" placeholder="owner/repo"
               class="w-full rounded-lg border border-slate-700 bg-slate-800/70 py-2.5 pl-10 pr-3
                      text-sm font-mono placeholder-slate-600 outline-none
                      focus:border-sky-600 focus:ring-1 focus:ring-sky-600/40"
               @keydown.enter.prevent="startTracking()" />
      </div>
      <button type="button" @click="startTracking()" :disabled="trackingBusy"
              class="flex items-center gap-2 rounded-lg border border-sky-600/50 bg-sky-600/10 px-4
                     py-2.5 text-sm font-medium text-sky-400 transition hover:bg-sky-600/20
                     disabled:cursor-not-allowed disabled:opacity-40">
        <i data-lucide="play" class="h-4 w-4"></i> Start Tracking
      </button>
    </div>
    <p x-show="trackMsg" x-text="trackMsg" class="mt-3 text-xs"
       :class="trackOk ? 'text-emerald-400' : 'text-rose-400'"></p>
  </section>

  <!-- Currently Monitored Repositories -->
  <section class="mt-8">
    <h2 class="mb-3 flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-slate-500">
      <i data-lucide="monitor-dot" class="h-4 w-4 text-emerald-400"></i> Currently Monitored Repositories
    </h2>
    <div class="space-y-2">
      <template x-for="r in tracked" :key="r.repo">
        <div class="flex items-center justify-between rounded-lg border border-slate-800
                    bg-slate-900/60 px-4 py-3">
          <div class="flex items-center gap-3">
            <span class="inline-flex items-center gap-1.5 rounded-full border border-emerald-700/60
                        bg-emerald-900/20 px-2.5 py-0.5 text-[11px] font-mono text-emerald-400">
              <span class="h-1.5 w-1.5 rounded-full bg-emerald-400 badge-glow"></span> Active
            </span>
            <span class="font-mono text-sm text-slate-200" x-text="r.repo"></span>
          </div>
          <div class="flex items-center gap-4">
            <span class="text-[11px] text-slate-500 font-mono"
                  x-text="'since ' + (new Date(r.added_at)).toLocaleTimeString()"></span>
            <button type="button" @click="untrackRepo(r.repo)"
                    class="rounded-md border border-slate-700 px-2 py-1 text-[11px]
                           text-slate-400 transition hover:border-rose-700/60 hover:text-rose-400">
              <i data-lucide="trash-2" class="h-3 w-3 inline"></i> untrack
            </button>
          </div>
        </div>
      </template>
      <p x-show="!tracked.length" class="text-sm text-slate-600">
        No repositories tracked yet — add one above to arm the auto-triage loop.
      </p>
    </div>
  </section>

  <!-- Recent Automated Fixes -->
  <section class="mt-8">
    <h2 class="mb-3 flex items-center gap-2 font-mono text-xs uppercase tracking-widest text-slate-500">
      <i data-lucide="zap" class="h-4 w-4 text-amber-400"></i> Recent Automated Fixes
    </h2>
    <div class="space-y-2">
      <template x-for="(f, i) in fixes" :key="f.timestamp + i">
        <div class="rounded-lg border border-slate-800 bg-slate-900/60 px-4 py-3">
          <div class="flex items-center justify-between gap-4">
            <div class="min-w-0">
              <div class="truncate text-sm text-slate-200" x-text="f.issue_title"></div>
              <div class="mt-0.5 font-mono text-[11px] text-slate-500"
                   x-text="f.repo + ' · ' + f.file_path"></div>
            </div>
            <a :href="f.pr_url" target="_blank" rel="noopener"
               class="shrink-0 inline-flex items-center gap-1.5 rounded-md border
                      border-emerald-700/50 bg-emerald-900/20 px-2.5 py-1 text-[11px]
                      font-mono text-emerald-400 hover:bg-emerald-900/40">
              <i data-lucide="git-pull-request-arrow" class="h-3 w-3"></i> PR
            </a>
          </div>
          <details class="mt-2">
            <summary class="cursor-pointer text-[11px] text-slate-500 hover:text-slate-400">show diff</summary>
            <pre class="language-diff mt-2"><code class="language-diff" x-text="f.patch"></code></pre>
          </details>
        </div>
      </template>
      <p x-show="!fixes.length" class="text-sm text-slate-600">
        No fixes generated yet in this session — they will appear here automatically.
      </p>
    </div>
  </section>

  <footer class="mt-12 border-t border-slate-800 pt-6 text-center text-xs text-slate-600">
    Patch · powered by Groq &amp; PyGithub
  </footer>
</div>

<script>
  // NOTE: must stay a zero-arg function Alpine can call as x-data.
  window.patch = function patch() {
    return {
      state: 'idle', // idle | running | success | error
      form: { repo: '', path: '', title: '', context: '' },
      logs: [], diff: '', prUrl: '', highlightedDiff: '',
      trackRepo: '', trackMsg: '', trackOk: false, trackingBusy: false,
      tracked: [], fixes: [],

      log(kind, text) {
        this.logs.push({ kind, text });
        this.$nextTick(() => {
          const box = this.$refs.logbox;
          if (box) box.scrollTop = box.scrollHeight;
        });
      },

      reset() { this.logs = []; this.diff = ''; this.prUrl = ''; this.highlightedDiff = ''; },

      run() {
        this.reset();
        this.state = 'running';
        this.log('run', `Starting pipeline for ${this.form.repo}/${this.form.path} …`);

        fetch('/webhook/trigger-fix', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            repo_name: this.form.repo,
            file_path: this.form.path,
            issue_title: this.form.title,
            error_context: this.form.context,
          }),
        })
        .then(r => r.json().then(j => ({ ok: r.ok, j })))
        .then(({ ok, j }) => {
          if (ok) {
            this.log('ok', j.pull_request_url);
            this.prUrl = j.pull_request_url;
            this.diff = j.patch ?? '';
            this.highlightedDiff = Prism.highlight(
              this.diff,
              Prism.languages.diff,
              'diff'
            );
            this.state = 'success';
            this.log('ok', 'Pull request opened successfully.');
          } else {
            this.state = 'error';
            this.log('err', (j.detail ?? 'unknown error').toString());
          }
        })
        .catch(e => { this.state = 'error'; this.log('err', String(e)); });
      },

      async startTracking() {
        this.trackMsg = '';
        if (!this.trackRepo) { this.trackMsg = 'enter owner/repo first'; this.trackOk = false; return; }
        this.trackingBusy = true;
        try {
          const r = await fetch('/api/track', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ repo: this.trackRepo }),
          });
          const j = await r.json();
          if (r.ok) {
            this.trackOk = true;
            this.trackMsg = `now tracking ${this.trackRepo} — auto-triage armed`;
            this.trackRepo = ''
          } else {
            this.trackOk = false;
            this.trackMsg = j.detail ?? 'failed to track';
          }
        } catch (e) { this.trackOk = false; this.trackMsg = String(e); }
        this.trackingBusy = false;
        const ctx = Alpine.$data(document.body);
        const t = await fetch('/api/tracked').then(r => r.json());
        ctx.tracked = t.tracked ?? [];
      },

      async untrackRepo(repo) {
        await fetch(`/api/track/${repo.split('/')[0]}/${repo.split('/')[1]}`, { method: 'DELETE' });
        const t = await fetch('/api/tracked').then(r => r.json());
        Alpine.$data(document.body).tracked = t.tracked ?? [];
      },
    };
  }

  // Alpine is defer-loaded: wait for 'alpine:init' (NOT DOMContentLoaded)
  // before touching the Alpine global, otherwise $data() throws and the
  // whole script dies, leaving the page half-initialized.
  document.addEventListener('alpine:init', () => {
    if (window.lucide) lucide.createIcons();
    Alpine.effect(() => {
      const ctx = Alpine.$data(document.body);
      if (!ctx) return;
      const refresh = async () => {
        try {
          const [t, f] = await Promise.all([
            fetch('/api/tracked').then(r => r.json()),
            fetch('/api/fixes').then(r => r.json()),
          ]);
          ctx.tracked = t.tracked ?? [];
          ctx.fixes = f.fixes ?? [];
        } catch (e) { /* dashboard polling is best-effort */ }
      };
      refresh();
      setInterval(refresh, 10000);
    });
  });
  // Re-render icons whenever Alpine injects new <i data-lucide> nodes.
  document.addEventListener('DOMContentLoaded', () => {
    try { lucide.createIcons(); } catch (e) { /* CDN not ready */ }
  });
  const observer = new MutationObserver(() => {
    if (window.lucide && document.body) lucide.createIcons();
  });
  function attachObserver() {
    if (document.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    } else {
      setTimeout(attachObserver, 50);
    }
  }
  if (document.body) {
    attachObserver();
  } else {
    document.addEventListener('DOMContentLoaded', attachObserver);
  }
</script>
</body>
</html>
"""


@app.get("/")
def dashboard() -> HTMLResponse:
    return HTMLResponse(content=_dashboard_html(), status_code=200)


class TrackRequest(BaseModel):
    repo: str


# In-memory feed of automated fixes (newest first). Not persisted: survives
# only for this process lifetime, which is fine for a local dashboard.
_fixes_feed: list[dict] = []


def _record_fix(
    repo: str, file_path: str, issue_title: str, patch: str, pr_url: str,
) -> None:
    """Insert one dashboard-visible fix entry, capped to the last 50."""
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
            payload.repo_name, payload.file_path, payload.issue_title, patch,
            pull_request_url,
        )
        return {"status": "success", "pull_request_url": pull_request_url, "patch": patch}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - single failure point for the webhook
        raise HTTPException(status_code=500, detail=str(exc)) from exc
