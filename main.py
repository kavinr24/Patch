import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
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

_ICON_PATH = Path(__file__).resolve().parent / "icon.png"


class IssuePayload(BaseModel):
    repo_name: str
    file_path: str
    issue_title: str
    error_context: str
    ref: str = "main"


def _dashboard_html() -> str:
    return DASHBOARD_TEMPLATE


# ---------------------------------------------------------------------------
# Dashboard: Tailwind CSS + Alpine.js + Lucide Icons + Prism.js, all via CDN.
# Loaded defensively: Alpine/lucide/prism init is guarded so a slow or blocked
# CDN degrades gracefully (plain text, no icons) instead of a blank page; all
# scripts are `defer` and logic runs on DOMContentLoaded / alpine:init.
# ---------------------------------------------------------------------------

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>Patch — Dashboard</title>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">

<!-- Tailwind CSS (Play CDN) -->
<script src="https://cdn.tailwindcss.com"></script>

<!-- Prism: theme + core + diff language -->
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/line-numbers/prism-line-numbers.min.css">

<style>
  :root {
    --bg: #090d16;
    --panel: #0d1220;
    --panel-2: #111a2c;
    --border: #1e293b;
    --border-2: #334155;
    --text: #e4e4e7;
    --text-muted: #a1a1aa;
    --text-faint: #71717a;
    --accent: #34d399;
    --accent-bg: rgba(52, 211, 153, 0.08);
    --mono: 'Space Grotesk', ui-monospace, monospace;
  }
  * { box-sizing: border-box; }
  html, body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Space Grotesk', system-ui, sans-serif;
  }
  [x-cloak] { display: none !important; }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
  }
  input, textarea {
    background: var(--panel-2);
    border: 1px solid var(--border);
    color: var(--text);
    transition: border-color .15s, box-shadow .15s;
  }
  input::placeholder, textarea::placeholder { color: var(--text-faint); }
  input:focus, textarea:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(52, 211, 153, 0.1);
  }
  .btn-primary {
    background: var(--accent);
    color: #052e21;
    transition: background .15s;
  }
  .btn-primary:hover { background: #6ee7b7; }
  .btn-primary:disabled { background: #1e293b; color: #64748b; cursor: not-allowed; }
  .btn-ghost {
    background: transparent; color: var(--text-muted);
    border: 1px solid var(--border-2);
    transition: background .15s, color .15s;
  }
  .btn-ghost:hover { background: var(--panel-2); color: var(--text); }
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 500;
    border: 1px solid var(--border);
  }
  .scrollbar-thin::-webkit-scrollbar { width: 6px; height: 6px; }
  .scrollbar-thin::-webkit-scrollbar-thumb { background: #2d3a4d; border-radius: 3px; }
  .scrollbar-thin::-webkit-scrollbar-track { background: transparent; }
</style>

<!-- Register the Alpine component BEFORE the Alpine CDN script executes -->
<script>
document.addEventListener('alpine:init', function () {
  Alpine.data('patchDashboard', function () {
    return {
      // ---------------- state ----------------
      backendOnline: null,
      running: false,
      form: { repo: '', filePath: '', issueTitle: '', errorContext: '', ref: '' },
      logLines: [],
      diffText: '',
      prUrl: '',
      showDiff: false,
      trackRepo: '',
      trackMsg: '', trackMsgOk: false,
      tracked: [],
      fixes: [],
      copied: false,
      toastMsg: '', toastOk: false, toastVisible: false,
      _toastTimer: null,
      _refreshTimer: null,
      _fixesSig: null,

      // ---------------- derived ----------------
      get diffStats() {
        var text = this.diffText || '';
        var lines = text.split('\\n');
        var add = 0, del = 0;
        for (var i = 0; i < lines.length; i++) {
          var l = lines[i];
          if (l.indexOf('+') === 0 && l.indexOf('+++') !== 0) add++;
          else if (l.indexOf('-') === 0 && l.indexOf('---') !== 0) del++;
        }
        return { add: add, del: del };
      },
      get highlightedDiff() {
        var text = this.diffText || '';
        if (!text) return '';
        try {
          if (window.Prism && Prism.languages && Prism.languages.diff) {
            return Prism.highlight(text, Prism.languages.diff, 'diff');
          }
        } catch (e) { /* fall through */ }
        return this.fallbackHighlight(text);
      },

      // ---------------- helpers ----------------
      fallbackHighlight(text) {
        var out = [];
        var lines = String(text).split('\\n');
        for (var i = 0; i < lines.length; i++) {
          var raw = lines[i];
          var e = this.esc(raw);
          var cls = 'text-zinc-400';
          if (raw.indexOf('+') === 0 && raw.indexOf('+++') !== 0) cls = 'text-emerald-400 bg-emerald-500/5';
          else if (raw.indexOf('-') === 0 && raw.indexOf('---') !== 0) cls = 'text-rose-400 bg-rose-500/5';
          else if (raw.indexOf('@@') === 0 || raw.indexOf('---') === 0 || raw.indexOf('+++') === 0) cls = 'text-sky-400 font-medium';
          out.push('<span class="' + cls + '">' + e + '</span>');
        }
        return out.join('\\n');
      },
      esc(s) {
        return String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      },
      log(kind, text) {
        this.logLines.push({ kind: kind, text: text, time: new Date().toLocaleTimeString() });
        var self = this;
        this.$nextTick(function () {
          var box = document.getElementById('logbox');
          if (box) box.scrollTop = box.scrollHeight;
          window.refreshIcons && window.refreshIcons();
        });
      },
      toast(msg, ok) {
        var self = this;
        this.toastMsg = msg;
        this.toastOk = !!ok;
        this.toastVisible = true;
        clearTimeout(this._toastTimer);
        this._toastTimer = setTimeout(function () { self.toastVisible = false; }, 3200);
      },

      // ---------------- pipeline ----------------
      submitPatch() {
        var f = this.form;
        if (this.running) return;
        if (!f.repo.trim() || !f.filePath.trim() || !f.issueTitle.trim() || !f.errorContext.trim()) {
          this.toast('All fields are required.', false);
          return;
        }
        var self = this;
        this.running = true;
        this.showDiff = false;
        this.diffText = '';
        this.prUrl = '';
        this.logLines = [];
        this.log('info', 'Pipeline started for ' + f.repo.trim() + ' › ' + f.filePath.trim());
        this.log('run', 'Fetching file from GitHub…');
        this.log('run', 'Generating patch with Groq…');
        this.log('run', 'Forking repo, applying patch, opening PR…');

        var body = {
          repo_name: f.repo.trim(),
          file_path: f.filePath.trim(),
          issue_title: f.issueTitle.trim(),
          error_context: f.errorContext.trim()
        };
        if (f.ref.trim()) body.ref = f.ref.trim();

        fetch('/webhook/trigger-fix', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        })
        .then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, j: j }; });
        })
        .then(function (res) {
          self.running = false;
          if (res.ok) {
            var pending = self.logLines.filter(function (l) { return l.kind === 'run'; });
            for (var i = 0; i < pending.length; i++) {
              pending[i].kind = 'ok';
              pending[i].text = pending[i].text.replace(/…$/, '') + ' — done';
            }
            self.log('ok', 'PR opened: ' + res.j.pull_request_url);
            self.diffText = res.j.patch || '';
            self.prUrl = res.j.pull_request_url || '';
            self.showDiff = true;
            self.toast('Patch generated & PR opened', true);
            self.loadFixes();
          } else {
            var pending2 = self.logLines.filter(function (l) { return l.kind === 'run'; });
            for (var k = 0; k < pending2.length; k++) pending2[k].kind = 'err';
            self.log('err', String(res.j.detail || 'unknown error'));
            self.toast('Pipeline failed — see log.', false);
          }
          window.refreshIcons && window.refreshIcons();
        })
        .catch(function (e) {
          self.running = false;
          var pending3 = self.logLines.filter(function (l) { return l.kind === 'run'; });
          for (var m = 0; m < pending3.length; m++) pending3[m].kind = 'err';
          self.log('err', String(e));
          self.toast('Network error — is the server running?', false);
          window.refreshIcons && window.refreshIcons();
        });
      },

      runInProgress(idx) { return this.logLines[idx] && this.logLines[idx].kind === 'run'; },

      // ---------------- diff / clipboard ----------------
      copyPatch() {
        if (!this.diffText) return;
        var self = this;
        navigator.clipboard.writeText(this.diffText).then(function () {
          self.copied = true;
          setTimeout(function () { self.copied = false; }, 1500);
        });
      },
      prNumber() {
        var m = String(this.prUrl).split('/pull/');
        return m.length > 1 ? '#' + m[1] : '';
      },

      // ---------------- auto-track ----------------
      addTrack() {
        var repo = this.trackRepo.trim();
        if (!repo) return;
        var self = this;
        this.trackMsg = ''; this.trackMsgOk = false;
        fetch('/api/track', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo: repo })
        })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (res.ok) {
            self.trackMsg = 'Now tracking ' + repo;
            self.trackMsgOk = true;
            self.trackRepo = '';
            self.loadTracked();
          } else {
            self.trackMsg = res.j.detail || 'Failed to track.';
            self.trackMsgOk = false;
          }
        })
        .catch(function (e) { self.trackMsg = String(e); self.trackMsgOk = false; });
      },
      untrack(repo) {
        var parts = repo.split('/');
        fetch('/api/track/' + parts[0] + '/' + parts[1], { method: 'DELETE' })
          .then(function () { this.loadTracked(); }.bind(this));
      },

      // ---------------- data loading ----------------
      loadTracked() {
        var self = this;
        return fetch('/api/tracked')
          .then(function (r) { return r.json(); })
          .then(function (j) {
            self.tracked = j.tracked || [];
            self.$nextTick(function () { window.refreshIcons && window.refreshIcons(); });
          })
          .catch(function () {});
      },
      loadFixes() {
        var self = this;
        return fetch('/api/fixes')
          .then(function (r) { return r.json(); })
          .then(function (j) {
            var sig = JSON.stringify(j.fixes || []);
            if (sig === self._fixesSig) return;
            self._fixesSig = sig;
            self.fixes = j.fixes || [];
            self.$nextTick(function () { window.refreshIcons && window.refreshIcons(); });
          })
          .catch(function () {});
      },
      pingHealth() {
        var self = this;
        fetch('/health')
          .then(function (r) { self.backendOnline = r.ok; })
          .catch(function () { self.backendOnline = false; });
      },
      fmtTime(iso) {
        return String(iso || '').slice(0, 16).replace('T', ' ');
      },

      // ---------------- lifecycle ----------------
      init() {
        var self = this;
        window.refreshIcons && window.refreshIcons();
        this.pingHealth();
        this.loadTracked();
        this.loadFixes();
        this._refreshTimer = setInterval(function () {
          self.pingHealth();
          if (!self.running) { self.loadTracked(); self.loadFixes(); }
        }, 10000);
        window.addEventListener('load', function () {
          window.refreshIcons && window.refreshIcons();
        });
      }
    };
  });
});

window.refreshIcons = function () {
  try { if (window.lucide && lucide.createIcons) lucide.createIcons(); } catch (e) {}
};
</script>

<!-- CDN scripts (deferred; boot after DOM is parsed) -->
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<script defer src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-diff.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/line-numbers/prism-line-numbers.min.js"></script>

</head>
<body class="min-h-screen antialiased" style="background:#090d16">

<div x-data="patchDashboard" class="max-w-6xl mx-auto px-6 pb-8">

  <!-- ============ Top nav ============ -->
  <nav class="sticky top-0 z-30 -mx-6 px-6 py-4 mb-10 flex items-center justify-between
              border-b border-slate-800 bg-[#090d16]/90 backdrop-blur">
    <div class="flex items-center gap-3">
      <img src="/icon.png" alt="Patch" class="h-7 w-7">
      <span class="text-[15px] font-semibold tracking-tight text-zinc-100">Patch</span>
    </div>
    <div class="flex items-center gap-6 text-[13px] text-zinc-500">
      <span class="hidden sm:inline">Autonomous issue triage</span>
      <span class="flex items-center gap-2">
        <span class="h-1.5 w-1.5 rounded-full"
              :class="backendOnline === false ? 'bg-rose-500' : 'bg-emerald-500'"></span>
        <span x-text="backendOnline === false ? 'Offline' : 'Operational'"></span>
      </span>
    </div>
  </nav>

  <div class="grid grid-cols-1 lg:grid-cols-5 gap-6 items-start">

    <!-- ======== LEFT (Manual Patch Generation, 3 cols) ======== -->
    <div class="lg:col-span-3 space-y-6">

      <section class="card p-6">
        <h2 class="text-[13px] font-medium tracking-tight text-zinc-200 mb-1">Manual patch generation</h2>
        <p class="text-[12px] text-zinc-500 mb-6">Fetch a file, generate a diff with Groq, and open a PR automatically.</p>

        <form @submit.prevent="submitPatch" class="space-y-5">
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label class="block">
              <span class="block text-[11px] font-medium text-zinc-500 mb-1.5">GitHub repository</span>
              <input x-model="form.repo" placeholder="owner/repo" autocomplete="off"
                     class="w-full rounded-lg px-3 py-2 text-[13px] font-mono placeholder-zinc-600">
            </label>
            <label class="block">
              <span class="block text-[11px] font-medium text-zinc-500 mb-1.5">Target file path</span>
              <input x-model="form.filePath" placeholder="src/main.py" autocomplete="off"
                     class="w-full rounded-lg px-3 py-2 text-[13px] font-mono placeholder-zinc-600">
            </label>
          </div>
          <label class="block">
            <span class="block text-[11px] font-medium text-zinc-500 mb-1.5">Issue title</span>
            <input x-model="form.issueTitle" placeholder="Crash when name is None" autocomplete="off"
                   class="w-full rounded-lg px-3 py-2 text-[13px] placeholder-zinc-600">
          </label>
          <label class="block">
            <span class="block text-[11px] font-medium text-zinc-500 mb-1.5">Description / traceback</span>
            <textarea x-model="form.errorContext" rows="5" placeholder="Paste the error, repro steps, or what should change…"
                      class="w-full rounded-lg px-3 py-2 text-[13px] font-mono placeholder-zinc-600 resize-y"></textarea>
          </label>
          <div class="grid grid-cols-1 sm:grid-cols-5 gap-4 items-end">
            <label class="block sm:col-span-2">
              <span class="block text-[11px] font-medium text-zinc-500 mb-1.5">
                Base branch <span class="text-zinc-700">(optional)</span>
              </span>
              <input x-model="form.ref" placeholder="main" autocomplete="off"
                     class="w-full rounded-lg px-3 py-2 text-[13px] font-mono placeholder-zinc-600">
            </label>
            <button type="submit" :disabled="running"
                    class="sm:col-span-3 inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2.5
                           text-[13px] font-semibold btn-primary">
              <span x-text="running ? 'Running…' : 'Generate Patch & Open PR'"></span>
            </button>
          </div>
        </form>
      </section>

      <!-- Execution log -->
      <section x-cloak x-show="logLines.length" class="card p-6">
        <h2 class="text-[13px] font-medium tracking-tight text-zinc-200 mb-4">Execution log</h2>
        <div id="logbox" class="h-48 overflow-y-auto scrollbar-thin rounded-lg border border-slate-800 p-3"
             style="background:#070b13">
          <template x-for="(line, idx) in logLines" :key="idx">
            <div class="flex items-start gap-3 font-mono text-[11px] py-0.5">
              <span class="w-14 shrink-0 text-zinc-700" x-text="line.time"></span>
              <span class="shrink-0 w-4 text-center"
                    :class="{
                      'text-emerald-500': line.kind === 'ok',
                      'text-rose-500': line.kind === 'err',
                      'text-zinc-500': line.kind === 'run',
                      'text-zinc-700': line.kind === 'info'
                    }"
                    x-text="line.kind === 'ok' ? '✓' : line.kind === 'err' ? '✕' : line.kind === 'run' ? '⟳' : '›'"></span>
              <span class="break-all"
                    :class="{
                      'text-zinc-300': line.kind === 'ok',
                      'text-rose-400': line.kind === 'err',
                      'text-zinc-500': line.kind === 'run',
                      'text-zinc-600': line.kind === 'info'
                    }"
                    x-text="line.text"></span>
            </div>
          </template>
        </div>
      </section>

      <!-- Generated diff -->
      <section x-cloak x-show="showDiff" class="space-y-4">
        <div class="card p-6">
          <div class="flex items-center justify-between mb-4">
            <h2 class="text-[13px] font-medium tracking-tight text-zinc-200">Generated diff</h2>
            <div class="flex items-center gap-3 font-mono text-[11px]">
              <span class="text-emerald-500" x-text="'+' + diffStats.add"></span>
              <span class="text-rose-500" x-text="'-' + diffStats.del"></span>
              <button @click="copyPatch" type="button"
                      class="inline-flex items-center gap-1 px-2 py-1 rounded-md border border-slate-800
                             text-zinc-400 hover:text-zinc-200 hover:border-slate-700 text-[11px] transition">
                <span x-text="copied ? 'Copied' : 'Copy'"></span>
              </button>
            </div>
          </div>
          <pre class="line-numbers max-h-80 overflow-auto scrollbar-thin rounded-lg p-4 text-xs"
               style="background:#070b13; border: 1px solid #1e293b"><code class="language-diff" x-html="highlightedDiff"></code></pre>
        </div>

        <a x-cloak x-show="prUrl" :href="prUrl" target="_blank" rel="noopener"
           class="card inline-flex items-center justify-center gap-2 w-full px-4 py-3 text-[13px]
                  font-medium text-zinc-200 hover:text-white hover:border-slate-700 transition">
          <span>View Pull Request</span>
          <span class="font-mono text-zinc-500" x-text="prNumber()"></span>
        </a>
      </section>
    </div>

    <!-- ======== RIGHT (Auto-Track + Monitored, 2 cols) ======== -->
    <div class="lg:col-span-2 space-y-6">

      <section class="card p-6">
        <h2 class="text-[13px] font-medium tracking-tight text-zinc-200 mb-1">Auto-track repository</h2>
        <p class="text-[12px] text-zinc-500 mb-5">Scan tracked repos for new issues every 30 seconds.</p>

        <div class="flex gap-2">
          <input x-model="trackRepo" @keydown.enter="addTrack" placeholder="owner/repo" autocomplete="off"
                 class="flex-1 rounded-lg px-3 py-2 text-[13px] font-mono placeholder-zinc-600">
          <button @click="addTrack" type="button"
                  class="rounded-lg px-4 py-2 text-[13px] font-medium btn-ghost">Track</button>
        </div>
        <p x-cloak x-show="trackMsg" class="mt-3 text-[11px] font-mono"
           :class="trackMsgOk ? 'text-emerald-500' : 'text-rose-400'" x-text="trackMsg"></p>
      </section>

      <section class="card p-6">
        <h2 class="text-[13px] font-medium tracking-tight text-zinc-200 mb-5">Monitored repositories</h2>

        <template x-if="!tracked.length">
          <p class="text-[12px] text-zinc-600 text-center py-8">No repositories tracked yet.</p>
        </template>

        <template x-for="rec in tracked" :key="rec.repo">
          <div class="flex items-center justify-between gap-3 py-2.5 border-b border-slate-800 last:border-b-0">
            <div class="min-w-0">
              <div class="font-mono text-[12px] text-zinc-300 truncate" x-text="rec.repo"></div>
              <div class="text-[10px] text-zinc-600 mt-0.5" x-text="'since ' + fmtTime(rec.added_at)"></div>
            </div>
            <button @click="untrack(rec.repo)" type="button"
                    class="shrink-0 text-[11px] text-zinc-600 hover:text-rose-400 transition">Remove</button>
          </div>
        </template>
      </section>

      <section class="card p-6">
        <h2 class="text-[13px] font-medium tracking-tight text-zinc-200 mb-5">Recent fixes</h2>

        <template x-if="!fixes.length">
          <p class="text-[12px] text-zinc-600 text-center py-8">No fixes generated yet.</p>
        </template>

        <template x-for="(f, idx) in fixes" :key="idx">
          <div class="py-3 border-b border-slate-800 last:border-b-0">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="text-[12px] font-medium text-zinc-300 truncate" x-text="f.issue_title"></div>
                <div class="text-[10px] text-zinc-600 mt-1"
                     x-text="f.repo + ' · ' + f.file_path + ' · ' + fmtTime(f.timestamp)"></div>
              </div>
              <a :href="f.pr_url" target="_blank" rel="noopener"
                 class="shrink-0 font-mono text-[11px] text-emerald-500 hover:text-emerald-400 transition">PR →</a>
            </div>
            <details class="mt-2">
              <summary class="text-[11px] text-zinc-500 hover:text-zinc-300 cursor-pointer transition select-none">diff</summary>
              <pre class="mt-2 max-h-52 overflow-auto scrollbar-thin rounded-md p-2.5 font-mono text-[10px] whitespace-pre text-zinc-500"
                   style="background:#070b13; border: 1px solid #1e293b"
                   x-text="(f.patch || '').slice(0, 2000) || '(fetched live)'"></pre>
            </details>
          </div>
        </template>
      </section>
    </div>
  </div>

  <!-- ============ Footer ============ -->
  <footer class="mt-14 pt-6 border-t border-slate-800 flex items-center justify-between">
    <p class="text-[11px] text-zinc-600">© 2026 Patch. All rights reserved.</p>
    <div class="flex items-center gap-2 text-[11px] text-zinc-600">
      <span class="h-1.5 w-1.5 rounded-full"
            :class="backendOnline === false ? 'bg-rose-500' : 'bg-emerald-500'"></span>
      <span x-text="backendOnline === false ? 'System offline' : 'All systems operational'"></span>
    </div>
  </footer>
</div>

<script>
window.addEventListener('load', function () {
  window.refreshIcons && window.refreshIcons();
});
</script>

</body>
</html>
"""


@app.get("/")
def dashboard() -> HTMLResponse:
    return HTMLResponse(content=_dashboard_html(), status_code=200)


@app.get("/icon.png", include_in_schema=False)
def icon() -> FileResponse:
    if not _ICON_PATH.is_file():
        raise HTTPException(status_code=404, detail="icon.png not found in project root")
    return FileResponse(_ICON_PATH, media_type="image/png")


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
