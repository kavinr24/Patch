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
<link rel="icon" type="image/png" href="/icon.png">

<!-- Tailwind CSS (Play CDN) -->
<script src="https://cdn.tailwindcss.com"></script>

<!-- Prism: theme + core + diff language (for the diff viewer) -->
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/line-numbers/prism-line-numbers.min.css">

<!-- Defeat FOUC: hide Alpine-controlled blocks until Alpine boots -->
<style>[x-cloak]{display:none !important;}</style>
</head>
<body class="min-h-screen bg-slate-900 text-slate-100 antialiased selection:bg-emerald-500/30">

<!--
  Register the Alpine component BEFORE the Alpine CDN script executes,
  via the alpine:init lifecycle event (works because Alpine loads with defer).
-->
<script>
document.addEventListener('alpine:init', function () {
  Alpine.data('patchDashboard', function () {
    return {
      // ---- state -----------------------------------------------------
      backendOnline: null,          // null = unknown, true/false after ping
      running: false,
      form: { repo: '', filePath: '', issueTitle: '', errorContext: '', ref: '' },
      logLines: [],                 // { kind: 'run'|'ok'|'err'|'info', text, time }
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

      // ---- derived ---------------------------------------------------
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
        } catch (e) { /* fall through to fallback */ }
        return this.fallbackHighlight(text);
      },

      // ---- helpers ---------------------------------------------------
      fallbackHighlight(text) {
        var out = [];
        var lines = String(text).split('\\n');
        for (var i = 0; i < lines.length; i++) {
          var raw = lines[i];
          var e = this.esc(raw);
          var cls = 'text-slate-300';
          if (raw.indexOf('+') === 0 && raw.indexOf('+++') !== 0) cls = 'text-emerald-400 bg-emerald-500/10';
          else if (raw.indexOf('-') === 0 && raw.indexOf('---') !== 0) cls = 'text-rose-400 bg-rose-500/10';
          else if (raw.indexOf('@@') === 0 || raw.indexOf('---') === 0 || raw.indexOf('+++') === 0) cls = 'text-sky-400 font-semibold';
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
        this.logLines.push({
          kind: kind,
          text: text,
          time: new Date().toLocaleTimeString()
        });
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
        this._toastTimer = setTimeout(function () {
          self.toastVisible = false;
        }, 3200);
      },

      // ---- pipeline --------------------------------------------------
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
            // Resolve the three pending steps as done.
            var pending = self.logLines.filter(function (l) { return l.kind === 'run'; });
            for (var i = 0; i < pending.length; i++) {
              pending[i].kind = 'ok';
              pending[i].text = pending[i].text.replace(/…$/, '') + ' — done';
            }
            self.log('ok', 'PR opened: ' + res.j.pull_request_url);
            self.diffText = res.j.patch || '';
            self.prUrl = res.j.pull_request_url || '';
            self.showDiff = true;
            self.toast('Patch generated & PR opened 🎉', true);
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

      // ---- diff / clipboard ------------------------------------------
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

      // ---- auto-track -------------------------------------------------
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
            self.trackMsg = 'Now tracking ' + repo + ' — every 30s the poller scans its open issues.';
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

      // ---- data loading / polls ---------------------------------------
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
            // Skip the repaint if nothing changed — keeps open <details> stable.
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
      isActive(record) { return true; },
      fmtTime(iso) {
        return String(iso || '').slice(0, 16).replace('T', ' ');
      },

      // ---- lifecycle --------------------------------------------------
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

// Lucide: expose a guarded icon refresh used everywhere above.
window.refreshIcons = function () {
  try { if (window.lucide && lucide.createIcons) lucide.createIcons(); } catch (e) {}
};
</script>

<!-- Alpine.js (deferred; boots after DOM is parsed) -->
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
<!-- Lucide icons -->
<script defer src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>
<!-- Prism core + diff language + line numbers -->
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-diff.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/line-numbers/prism-line-numbers.min.js"></script>

<div x-data="patchDashboard" class="max-w-6xl mx-auto px-4 sm:px-6 pb-16">

  <!-- ================= Header ================= -->
  <header class="sticky top-0 z-30 -mx-4 sm:-mx-6 px-4 sm:px-6 py-4 mb-8
                 bg-slate-900/80 backdrop-blur border-b border-slate-800
                 flex items-center justify-between gap-4">
    <div class="flex items-center gap-3">
        <img src="/icon.png" alt="Patch logo" class="h-9 w-9 rounded-lg
                    shadow-[0_0_18px_rgba(16,185,129,.35)] border border-slate-700">
      <div>
        <h1 class="text-lg font-semibold tracking-tight leading-none">Patch</h1>
        <p class="text-[11px] text-slate-500 mt-0.5">Autonomous GitHub issue triage &amp; patch generation</p>
      </div>
    </div>

    <div class="flex items-center gap-2">
      <span x-cloak x-show="backendOnline === true"
            class="inline-flex items-center gap-2 text-[11px] font-mono px-3 py-1.5 rounded-full
                   text-emerald-300 bg-emerald-500/10 border border-emerald-500/40
                   shadow-[0_0_10px_rgba(16,185,129,.35)] animate-pulse">
        <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span> backend online
      </span>
      <span x-cloak x-show="backendOnline === false"
            class="inline-flex items-center gap-2 text-[11px] font-mono px-3 py-1.5 rounded-full
                   text-rose-300 bg-rose-500/10 border border-rose-500/40
                   shadow-[0_0_10px_rgba(244,63,94,.35)]">
        <span class="h-1.5 w-1.5 rounded-full bg-rose-400"></span> backend offline
      </span>
      <span x-cloak x-show="backendOnline === null"
            class="inline-flex items-center gap-2 text-[11px] font-mono px-3 py-1.5 rounded-full
                   text-slate-400 bg-slate-800 border border-slate-700">
        <span class="h-1.5 w-1.5 rounded-full bg-slate-500 animate-pulse"></span> checking…
      </span>
      <a href="https://github.com" target="_blank" rel="noopener"
         class="h-9 w-9 rounded-lg border border-slate-700 bg-slate-800/60 hover:bg-slate-800
                flex items-center justify-center transition" title="GitHub">
        <i data-lucide="github" class="w-4 h-4 text-slate-300"></i>
      </a>
    </div>
  </header>

  <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">

    <!-- ============ LEFT COLUMN (2/3) ============ -->
    <div class="lg:col-span-2 space-y-6">

      <!-- Manual Patch Generation -->
      <section class="rounded-xl border border-slate-800 bg-slate-900/60 backdrop-blur p-5">
        <div class="flex items-center gap-2 mb-4">
          <i data-lucide="file-code-2" class="w-4 h-4 text-emerald-400"></i>
          <h2 class="text-[11px] font-semibold uppercase tracking-[.14em] text-slate-400">Manual patch generation</h2>
          <span x-cloak x-show="running"
                class="ml-auto inline-flex items-center gap-1.5 text-[10px] font-mono px-2 py-1 rounded-full
                       text-sky-300 bg-sky-500/10 border border-sky-500/40 animate-pulse">
                <span class="h-1.5 w-1.5 rounded-full bg-sky-400"></span> running
              </span>
        </div>

        <form @submit.prevent="submitPatch" class="space-y-4">
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label class="block">
              <span class="block text-[11px] font-medium text-slate-400 mb-1.5">GitHub repository</span>
              <input x-model="form.repo" class="w-full bg-slate-950/70 border border-slate-700 rounded-lg
                     px-3 py-2 text-sm font-mono text-slate-100 placeholder-slate-600
                     focus:outline-none focus:border-emerald-500/60 focus:ring-1 focus:ring-emerald-500/40 transition"
                     placeholder="owner/repo" autocomplete="off">
            </label>
            <label class="block">
              <span class="block text-[11px] font-medium text-slate-400 mb-1.5">Target file path</span>
              <input x-model="form.filePath" class="w-full bg-slate-950/70 border border-slate-700 rounded-lg
                     px-3 py-2 text-sm font-mono text-slate-100 placeholder-slate-600
                     focus:outline-none focus:border-emerald-500/60 focus:ring-1 focus:ring-emerald-500/40 transition"
                     placeholder="src/main.py" autocomplete="off">
            </label>
          </div>
          <label class="block">
            <span class="block text-[11px] font-medium text-slate-400 mb-1.5">Issue title</span>
            <input x-model="form.issueTitle" class="w-full bg-slate-950/70 border border-slate-700 rounded-lg
                   px-3 py-2 text-sm text-slate-100 placeholder-slate-600
                   focus:outline-none focus:border-emerald-500/60 focus:ring-1 focus:ring-emerald-500/40 transition"
                   placeholder="Crash when name is None" autocomplete="off">
          </label>
          <label class="block">
            <span class="block text-[11px] font-medium text-slate-400 mb-1.5">Description / traceback</span>
            <textarea x-model="form.errorContext" rows="5"
                      class="w-full bg-slate-950/70 border border-slate-700 rounded-lg px-3 py-2 text-sm
                      font-mono text-slate-100 placeholder-slate-600 resize-y
                      focus:outline-none focus:border-emerald-500/60 focus:ring-1 focus:ring-emerald-500/40 transition"
                      placeholder="Paste the error, repro steps, or what should change…"></textarea>
          </label>
          <div class="grid grid-cols-1 sm:grid-cols-5 gap-4 items-end">
            <label class="block sm:col-span-2">
              <span class="block text-[11px] font-medium text-slate-400 mb-1.5">
                Base branch <span class="text-slate-600">(optional — defaults to repo default)</span>
              </span>
              <input x-model="form.ref" class="w-full bg-slate-950/70 border border-slate-700 rounded-lg
                     px-3 py-2 text-sm font-mono text-slate-100 placeholder-slate-600
                     focus:outline-none focus:border-emerald-500/60 focus:ring-1 focus:ring-emerald-500/40 transition"
                     placeholder="main" autocomplete="off">
            </label>
            <button type="submit" :disabled="running"
                    class="sm:col-span-3 inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2.5
                           text-sm font-semibold text-emerald-300 bg-emerald-500/15 border border-emerald-500/50
                           hover:bg-emerald-500/25 disabled:opacity-40 disabled:cursor-not-allowed
                           shadow-[0_0_16px_rgba(16,185,129,.25)] hover:shadow-[0_0_22px_rgba(16,185,129,.45)] transition">
              <i data-lucide="wand-2" class="w-4 h-4"></i>
              <span x-text="running ? 'Running pipeline…' : 'Generate Patch & Open PR'"></span>
            </button>
          </div>
        </form>
      </section>

      <!-- Execution log -->
      <section x-cloak x-show="logLines.length"
               class="rounded-xl border border-slate-800 bg-slate-900/60 backdrop-blur p-5">
        <div class="flex items-center gap-2 mb-3">
          <i data-lucide="activity" class="w-4 h-4 text-sky-400"></i>
          <h2 class="text-[11px] font-semibold uppercase tracking-[.14em] text-slate-400">Execution log</h2>
        </div>
        <div id="logbox" class="h-52 overflow-y-auto rounded-lg bg-slate-950/80 border border-slate-800 p-3">
          <template x-for="(line, idx) in logLines" :key="idx">
            <div class="flex items-start gap-2 font-mono text-xs py-0.5">
              <span class="w-14 shrink-0 text-slate-600" x-text="line.time"></span>
              <span class="shrink-0 w-4 text-center"
                    :class="{
                      'text-emerald-400': line.kind === 'ok',
                      'text-rose-400': line.kind === 'err',
                      'text-sky-400 animate-spin inline-block': line.kind === 'run',
                      'text-slate-500': line.kind === 'info'
                    }"
                    x-text="line.kind === 'ok' ? '✓' : line.kind === 'err' ? '✕' : line.kind === 'run' ? '⟳' : '›'"></span>
              <span class="break-all"
                    :class="{
                      'text-emerald-300': line.kind === 'ok',
                      'text-rose-300': line.kind === 'err',
                      'text-slate-400': line.kind === 'run',
                      'text-slate-500': line.kind === 'info'
                    }"
                    x-text="line.text"></span>
            </div>
          </template>
        </div>
      </section>

      <!-- Diff viewer + PR link -->
      <section x-cloak x-show="showDiff" class="space-y-4">
        <div class="rounded-xl border border-slate-800 bg-slate-900/60 backdrop-blur p-5">
          <div class="flex items-center gap-2 mb-3">
            <i data-lucide="git-compare" class="w-4 h-4 text-sky-400"></i>
            <h2 class="text-[11px] font-semibold uppercase tracking-[.14em] text-slate-400">Generated diff</h2>
            <span class="ml-auto flex items-center gap-2 font-mono text-[10px]">
              <span class="text-emerald-400" x-text="'+' + diffStats.add"></span>
              <span class="text-rose-400" x-text="'-' + diffStats.del"></span>
              <button @click="copyPatch" type="button"
                      class="ml-2 inline-flex items-center gap-1 px-2 py-1 rounded-md border border-slate-700
                             bg-slate-800/60 text-slate-300 hover:bg-slate-800 text-[10px] transition">
                <i data-lucide="copy" class="w-3 h-3"></i>
                <span x-text="copied ? 'copied!' : 'copy'"></span>
              </button>
            </span>
          </div>
          <pre class="line-numbers max-h-80 overflow-auto rounded-lg bg-slate-950 border border-slate-800
                      text-xs p-4 whitespace-pre"><code class="language-diff" x-html="highlightedDiff"></code></pre>
        </div>

        <a x-cloak x-show="prUrl" :href="prUrl" target="_blank" rel="noopener"
           class="flex items-center justify-center gap-2 rounded-lg px-4 py-3 text-sm font-semibold
                  text-emerald-300 bg-emerald-500/15 border border-emerald-500/50
                  hover:bg-emerald-500/25 shadow-[0_0_16px_rgba(16,185,129,.25)] transition">
          <i data-lucide="git-pull-request" class="w-4 h-4"></i>
          <span>View PR on GitHub</span>
          <span class="font-mono" x-text="prNumber()"></span>
          <i data-lucide="external-link" class="w-3.5 h-3.5"></i>
        </a>
      </section>
    </div>

    <!-- ============ RIGHT COLUMN (1/3) ============ -->
    <div class="space-y-6">

      <!-- Auto-track management -->
      <section class="rounded-xl border border-slate-800 bg-slate-900/60 backdrop-blur p-5">
        <div class="flex items-center gap-2 mb-4">
          <i data-lucide="list-plus" class="w-4 h-4 text-sky-400"></i>
          <h2 class="text-[11px] font-semibold uppercase tracking-[.14em] text-slate-400">Auto-track repository</h2>
          <span class="ml-auto text-[10px] font-mono text-slate-600">every 30s</span>
        </div>
        <div class="flex gap-2">
          <input x-model="trackRepo" @keydown.enter="addTrack"
                 class="flex-1 bg-slate-950/70 border border-slate-700 rounded-lg px-3 py-2 text-sm
                        font-mono text-slate-100 placeholder-slate-600
                        focus:outline-none focus:border-sky-500/60 focus:ring-1 focus:ring-sky-500/40 transition"
                 placeholder="owner/repo" autocomplete="off">
          <button @click="addTrack" type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-sm font-semibold
                         text-sky-300 bg-sky-500/15 border border-sky-500/50 hover:bg-sky-500/25
                         shadow-[0_0_12px_rgba(56,189,248,.25)] transition">
            <i data-lucide="plus" class="w-4 h-4"></i> Track
          </button>
        </div>
        <p x-cloak x-show="trackMsg" class="mt-2.5 text-[11px] font-mono"
           :class="trackMsgOk ? 'text-emerald-300' : 'text-rose-300'" x-text="trackMsg"></p>
      </section>

      <!-- Monitored repos -->
      <section class="rounded-xl border border-slate-800 bg-slate-900/60 backdrop-blur p-5">
        <div class="flex items-center gap-2 mb-4">
          <i data-lucide="folder-git-2" class="w-4 h-4 text-emerald-400"></i>
          <h2 class="text-[11px] font-semibold uppercase tracking-[.14em] text-slate-400">Monitored repositories</h2>
          <span x-cloak x-show="tracked.length" class="ml-auto font-mono text-[10px] px-2 py-0.5 rounded-full
                text-slate-400 border border-slate-700 bg-slate-800/60" x-text="tracked.length"></span>
        </div>

        <!-- glowing active badge once tracking exists -->
        <template x-if="tracked.length">
          <div class="inline-flex items-center gap-2 mb-3 px-3 py-1 rounded-full text-[10px] font-mono
                      text-emerald-300 bg-emerald-500/10 border border-emerald-500/40
                      shadow-[0_0_12px_rgba(16,185,129,.35)] animate-pulse">
            <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span> poller armed
          </div>
        </template>

        <template x-if="!tracked.length">
          <div class="flex flex-col items-center gap-2 py-6 text-center">
            <i data-lucide="inbox" class="w-6 h-6 text-slate-600"></i>
            <p class="text-xs text-slate-500">No repositories tracked yet.</p>
          </div>
        </template>

        <template x-for="rec in tracked" :key="rec.repo">
          <div class="flex items-center justify-between gap-3 rounded-lg border border-slate-800
                      bg-slate-950/50 px-3.5 py-2.5 mb-2">
            <div class="min-w-0">
              <div class="font-mono text-[13px] text-slate-200 truncate" x-text="rec.repo"></div>
              <div class="font-mono text-[10px] text-slate-600 mt-0.5"
                   x-text="'since ' + fmtTime(rec.added_at)"></div>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[10px] font-mono
                           text-emerald-300 bg-emerald-500/10 border border-emerald-500/40">
                <span class="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse"></span> active
              </span>
              <button @click="untrack(rec.repo)" type="button" title="Untrack"
                      class="h-7 w-7 rounded-md border border-slate-700 bg-slate-800/60 hover:bg-rose-500/20
                             hover:border-rose-500/50 flex items-center justify-center transition">
                <i data-lucide="trash-2" class="w-3.5 h-3.5 text-slate-400"></i>
              </button>
            </div>
          </div>
        </template>
      </section>

      <!-- Recent fixes -->
      <section class="rounded-xl border border-slate-800 bg-slate-900/60 backdrop-blur p-5">
        <div class="flex items-center gap-2 mb-4">
          <i data-lucide="history" class="w-4 h-4 text-emerald-400"></i>
          <h2 class="text-[11px] font-semibold uppercase tracking-[.14em] text-slate-400">Recent automated fixes</h2>
        </div>

        <template x-if="!fixes.length">
          <div class="flex flex-col items-center gap-2 py-6 text-center">
            <i data-lucide="bug" class="w-6 h-6 text-slate-600"></i>
            <p class="text-xs text-slate-500">No fixes generated yet this session.</p>
          </div>
        </template>

        <template x-for="(f, idx) in fixes" :key="idx">
          <div class="rounded-lg border border-slate-800 bg-slate-950/50 px-3.5 py-3 mb-2">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="text-[13px] font-semibold text-slate-200 truncate" x-text="f.issue_title"></div>
                <div class="font-mono text-[10px] text-slate-600 mt-1"
                     x-text="f.repo + ' · ' + f.file_path + ' · ' + fmtTime(f.timestamp)"></div>
              </div>
              <a :href="f.pr_url" target="_blank" rel="noopener"
                 class="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-mono
                        text-emerald-300 bg-emerald-500/10 border border-emerald-500/50 hover:bg-emerald-500/20 transition">
                <i data-lucide="git-pull-request" class="w-3 h-3"></i> PR
              </a>
            </div>
            <details class="mt-2 group">
              <summary class="text-[11px] text-slate-500 cursor-pointer hover:text-slate-400 transition
                             select-none list-none flex items-center gap-1.5">
                <i data-lucide="chevron-right" class="w-3 h-3 transition group-open:rotate-90"></i> show diff
              </summary>
              <pre class="mt-2 max-h-52 overflow-auto rounded-md bg-slate-950 border border-slate-800
                          p-2.5 font-mono text-[10px] whitespace-pre text-slate-400"
                   x-text="(f.patch || '').slice(0, 2000) || '(fetched live)'"></pre>
            </details>
          </div>
        </template>
      </section>
    </div>
  </div>

  <footer class="mt-10 pt-5 border-t border-slate-800 flex items-center justify-center gap-2">
    <i data-lucide="zap" class="w-3.5 h-3.5 text-slate-600"></i>
    <p class="text-[11px] text-slate-600">Patch · powered by Groq &amp; PyGithub · FastAPI</p>
  </footer>

  <!-- Toast -->
  <div id="toast" x-cloak
       class="fixed bottom-5 right-5 z-50 transition-all duration-300
              flex items-center gap-2.5 px-4 py-3 rounded-xl border shadow-2xl backdrop-blur
              bg-slate-900/90"
       :class="toastVisible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-4'"
       style="border-color: rgba(51,65,85,1)">
    <span class="h-2 w-2 rounded-full"
          :class="toastOk ? 'bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,.7)]'
                          : 'bg-rose-400 shadow-[0_0_8px_rgba(244,63,94,.7)]'"></span>
    <span class="text-xs font-medium text-slate-200" x-text="toastMsg"></span>
  </div>
</div>

<script>
// Final icon pass once every CDN script has finished loading.
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
