#!/usr/bin/env python3
"""
Live Context Agent dashboard server.

Run from any project using Context Agent:
  python .context/scripts/dashboard_server.py --port 8765
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HTML = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Context Agent Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Albert+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* Design Hub · workbench macrostructure · restrained premium light theme */
:root {
  --bg:#f4f3ef; --panel:#fbfbf9; --panel-2:#efeee9; --ink:#171815;
  --muted:#71736c; --line:#deddd6; --line-strong:#c5c4bc;
  --accent:#c23d36; --accent-strong:#a9312c; --accent-hover:#a9312c;
  --blue:#315f78; --good:#267052; --warn:#a35d16; --bad:#b53232;
  --code:#1e211d;
  --soft-good:#eaf3ee; --soft-blue:#e9f0f3; --soft-warn:#f6eee4; --soft-bad:#f7eaea;
  --shadow:0 1px 2px rgba(23,24,21,.035);
  --shadow-lg:0 8px 26px rgba(23,24,21,.07);
  --radius:8px; --radius-sm:5px; --rail:244px; --topbar:72px;
}
* { box-sizing:border-box; }
body {
  margin:0; min-height:100vh; color:var(--ink);
  background:var(--bg);
  font:14px/1.55 "Albert Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
}
header {
  position:fixed; left:var(--rail); right:0; top:0; z-index:20; height:var(--topbar);
  display:flex; justify-content:space-between; align-items:center; gap:18px;
  padding:14px 28px;
  background:rgba(244,243,239,.94); color:var(--ink);
  border-bottom:1px solid var(--line);
  backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px);
}
h1 { margin:0; font-size:19px; font-weight:650; letter-spacing:-.035em; color:var(--ink); }
h2 { margin:0; font-size:15px; font-weight:600; letter-spacing:-.02em; }
.subtitle, .muted, .hint { color:var(--muted); }
header .subtitle { color:var(--muted); font-weight:400; }
.subtitle { font-size:12px; margin-top:2px; }
main { max-width:1540px; margin:var(--topbar) auto 0; padding:26px 28px 42px; padding-left:calc(var(--rail) + 28px); display:grid; gap:18px; }
button, input, select { font:inherit; font-family:"Albert Sans", -apple-system, system-ui, sans-serif; }
button {
  min-height:36px; border:1px solid var(--ink); border-radius:var(--radius-sm); padding:8px 15px;
  background:var(--ink); color:var(--panel); cursor:pointer; font-weight:600;
  transition:background .15s, border-color .15s, transform .12s; font-size:13px;
}
button:hover { background:#30322d; border-color:#30322d; }
button:active { transform:translateY(1px); }
button.secondary {
  background:var(--panel); color:var(--ink); border-color:var(--line);
  box-shadow:var(--shadow);
}
button.secondary:hover { background:var(--panel-2); border-color:var(--line-strong); }
button.ghost { background:transparent; color:var(--muted); border-color:transparent; box-shadow:none; }
button.ghost:hover { color:var(--ink); background:var(--panel-2); }
button.primary { background:var(--ink); color:var(--panel); }
button:disabled { opacity:.4; cursor:not-allowed; }
input, select {
  min-width:0; color:var(--ink); background:var(--panel);
  border:1px solid var(--line); border-radius:var(--radius-sm); padding:9px 11px; outline:none;
  transition:border-color .15s, box-shadow .15s;
}
select { min-height:36px; padding:8px 10px; }
header select { background:var(--panel); color:var(--ink); border-color:var(--line); }
input:focus, select:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(194,61,54,.1); }
input::placeholder { color:var(--muted); }
button.compact { min-height:32px; padding:5px 12px; font-size:12px; }
.path-input { width:260px; min-height:34px; padding:7px 10px; }
.row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.pill {
  display:inline-flex; align-items:center; gap:7px; padding:4px 9px;
  border:1px solid var(--line); border-radius:999px; color:var(--muted);
  background:var(--panel); font-size:11px; font-weight:500; white-space:nowrap;
}
header .pill { background:transparent; border-color:var(--line); color:var(--muted); }
.hero { display:grid; grid-template-columns:1.15fr .85fr; gap:16px; }
.card {
  background:var(--panel); border:1px solid var(--line);
  border-radius:var(--radius); padding:20px; box-shadow:var(--shadow);
  transition:border-color .2s, box-shadow .2s;
}
.card:hover { border-color:var(--line-strong); box-shadow:var(--shadow-lg); }
.primary-card {
  padding:22px; background:var(--panel);
  border-top:2px solid var(--ink);
}
.metric-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-top:14px; }
.stat-grid { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; }
.metric {
  min-height:108px; background:transparent; border:1px solid var(--line);
  border-radius:var(--radius); padding:16px;
  transition:border-color .2s, background .2s;
}
.metric:hover { border-color:var(--line-strong); background:var(--panel); }
.metric.emphasis { background:var(--soft-good); border-color:#c8e6d5; }
.metric.blue { background:var(--soft-blue); border-color:#c0d8f0; }
.metric.warn { background:var(--soft-warn); border-color:#ffe0b2; }
.label {
  color:var(--muted); font:500 10px/1.4 "JetBrains Mono", monospace;
  text-transform:uppercase; letter-spacing:.075em;
}
.value { margin-top:9px; font-size:29px; line-height:1.05; font-weight:650; letter-spacing:-.045em; }
.value.small { font-size:22px; }
.value.good { color:var(--good); } .value.warn { color:var(--warn); } .value.bad { color:var(--bad); }
.hint { margin-top:8px; font-size:12px; min-height:34px; color:var(--muted); }
.bar { height:3px; background:var(--line); border-radius:999px; overflow:hidden; margin-top:12px; }
.fill { height:100%; width:0%; background:var(--ink); transition:width .3s ease; border-radius:999px; }
.fill.warn { background:var(--warn); }
.fill.bad { background:var(--bad); }
.task { display:grid; grid-template-columns:1fr 120px; gap:10px; margin-top:14px; }
.split { display:grid; grid-template-columns:1.08fr .92fr; gap:16px; }
.three { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
.toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
table { width:100%; border-collapse:collapse; }
th, td { border-bottom:1px solid var(--line); padding:10px 10px; text-align:left; vertical-align:top; }
th { color:var(--muted); font:500 10px/1.4 "JetBrains Mono", monospace; text-transform:uppercase; letter-spacing:.065em; background:var(--panel-2); position:sticky; top:0; }
tr[onclick]:hover { background:var(--panel-2); }
td { font-size:13px; }
.mono { font-family:"JetBrains Mono", "Cascadia Mono", monospace; font-size:12px; }
pre {
  margin:0; padding:16px; max-height:370px; overflow:auto; white-space:pre-wrap;
  color:#1d1d1f; background:var(--panel-2); border:1px solid var(--line); border-radius:var(--radius);
  font:12px/1.6 "JetBrains Mono", "Cascadia Mono", monospace;
}
.error { color:var(--bad); font-weight:600; }
.section-title { display:flex; justify-content:space-between; align-items:flex-start; gap:14px; margin-bottom:12px; }
.explain { display:grid; gap:8px; color:var(--muted); font-size:12px; line-height:1.6; }
.explain strong { color:var(--ink); }
.status-dot { width:8px; height:8px; display:inline-block; border-radius:999px; background:var(--good); }
.table-wrap {
  max-height:390px; overflow:auto; border:1px solid var(--line); border-radius:var(--radius);
}
.inline-panel { margin-top:16px; padding-top:16px; border-top:1px solid var(--line); }
.mini-usage { display:grid; gap:8px; margin-top:10px; }
.mini-row {
  display:grid; grid-template-columns:1fr auto auto; gap:10px; align-items:center;
  padding:10px 14px; background:var(--panel); border:1px solid var(--line); border-radius:var(--radius-sm);
  transition:background .15s;
}
.mini-row:hover { background:var(--panel-2); }
.mini-row strong { font-size:13px; }
.mini-row span { color:var(--muted); font-size:12px; }
nav.pages {
  position:fixed; inset:0 auto 0 0; z-index:30; width:var(--rail);
  display:flex; flex-direction:column; gap:2px; padding:110px 14px 18px;
  background:#1d1f1b; border-right:1px solid #2d302a; overflow-y:auto;
}
nav.pages button {
  min-height:34px; padding:7px 11px; font-size:12px; font-weight:500; text-align:left;
  background:transparent; color:#9fa29a; border:1px solid transparent; border-radius:5px;
  box-shadow:none; transition:background .15s, color .15s, border-color .15s;
}
nav.pages button:hover { background:#292c26; border-color:#34372f; transform:none; box-shadow:none; color:#f2f1ed; }
nav.pages button.active {
  background:#f2f1ed; color:#171815; border-color:#f2f1ed;
}
.brand {
  position:fixed; left:0; top:0; z-index:40; width:var(--rail); height:96px;
  display:flex; align-items:center; gap:11px; padding:22px 24px;
  color:#f4f3ef; background:#1d1f1b; border-bottom:1px solid #34372f;
}
.brand-mark { width:27px; height:27px; display:grid; place-items:center; border:1px solid #62665c; border-radius:6px; font:600 11px/1 "JetBrains Mono",monospace; }
.brand h1 { color:#f4f3ef; font-size:16px; }
.brand .subtitle { color:#858980; }
.nav-label { color:#666a62; font:500 9px/1.4 "JetBrains Mono",monospace; letter-spacing:.1em; text-transform:uppercase; padding:15px 11px 5px; }
.topbar-title { min-width:170px; }
.topbar-title .label { margin-bottom:2px; }
.topbar-title strong { font-size:14px; font-weight:600; }
.page { display:none; }
.page.visible { display:grid; gap:16px; }
.onboard { border-radius:var(--radius); border-color:var(--line-strong); background:var(--panel-2); }
.badge { display:inline-block; padding:3px 10px; border-radius:999px; font-size:11px; font-weight:500; }
.badge.good { background:var(--soft-good); color:var(--good); }
.badge.warn { background:var(--soft-warn); color:var(--warn); }
.badge.bad { background:var(--soft-bad); color:var(--bad); }
.badge.muted { background:var(--panel-2); color:var(--muted); }
.feature-switch { display:flex; align-items:center; gap:12px; padding:13px 14px; border:1px solid var(--line); border-radius:var(--radius); background:var(--panel-2); }
.feature-switch input { width:44px; height:24px; accent-color:var(--accent); cursor:pointer; }
.feature-switch .copy { flex:1; }
.feature-switch .copy strong { display:block; margin-bottom:3px; }
/* Custom scrollbar */
::-webkit-scrollbar { width:8px; height:8px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:rgba(0,0,0,.1); border-radius:4px; }
::-webkit-scrollbar-thumb:hover { background:rgba(0,0,0,.2); }
/* Selection */
::selection { background:rgba(194,61,54,.16); color:var(--ink); }
@media (max-width:1050px) {
  .hero,.split,.metric-grid,.stat-grid,.three,.task { grid-template-columns:1fr; }
  :root { --rail:0px; --topbar:118px; }
  .brand { position:relative; width:100%; height:auto; padding:16px 18px; }
  header { position:relative; left:0; height:auto; align-items:flex-start; flex-direction:column; padding:14px 18px; }
  nav.pages { position:relative; width:100%; inset:auto; flex-direction:row; padding:9px 14px; overflow-x:auto; border-right:0; }
  nav.pages .nav-label { display:none; }
  nav.pages button { white-space:nowrap; text-align:center; }
  main { margin:0; padding:18px; }
  .path-input { width:min(100%,340px); }
}
</style>
</head>
<body>
<div class="brand">
  <div class="brand-mark">CA</div>
  <div>
    <h1>Context Agent</h1>
    <div class="subtitle">Intelligence console</div>
  </div>
</div>
<header>
  <div class="topbar-title">
    <div class="label">Workspace</div>
    <strong>Project control plane</strong>
  </div>
  <div class="row">
    <span class="pill" id="root">root: ...</span>
    <select id="projectSelect" onchange="selectProject(this.value)" title="Dashboard projesi">
      <option value="">Bu proje</option>
    </select>
    <input id="pathInput" class="path-input" placeholder="Proje path'i gir..." title="İçinde .context olan proje klasörü">
    <button class="secondary compact" onclick="openPath()">Aç</button>
    <span class="pill"><span class="status-dot"></span><span id="lastUpdated">bekliyor</span></span>
    <select id="refreshEvery" onchange="setAutoRefresh(this.value)" title="Otomatik dashboard yenileme">
      <option value="0">Auto: kapalı</option>
      <option value="10000">Auto: 10 sn</option>
      <option value="30000" selected>Auto: 30 sn</option>
      <option value="60000">Auto: 60 sn</option>
    </select>
  </div>
</header>
<nav class="pages" id="pageNav">
  <span class="nav-label">Intelligence</span>
  <button data-page="overview" class="active" onclick="showPage('overview')">Overview</button>
  <button data-page="projects" onclick="showPage('projects')">Projects</button>
  <button data-page="context" onclick="showPage('context')">Context Intelligence</button>
  <button data-page="memory" onclick="showPage('memory')">Project Memory</button>
  <button data-page="sessions" onclick="showPage('sessions')">IDE Sessions</button>
  <span class="nav-label">Model layer</span>
  <button data-page="execution" onclick="showPage('execution')">Model Execution</button>
  <button data-page="providers" onclick="showPage('providers')">AI Providers</button>
  <button data-page="models" onclick="showPage('models')">Models</button>
  <button data-page="router" onclick="showPage('router')">Routing Policy</button>
  <button data-page="usagecost" onclick="showPage('usagecost')">Usage &amp; Cost</button>
  <button data-page="history" onclick="showPage('history')">Routing History</button>
  <span class="nav-label">System</span>
  <button data-page="diagnostics" onclick="showPage('diagnostics')">Diagnostics</button>
  <button data-page="settings" onclick="showPage('settings')">Settings</button>
</nav>
<main>
  <div class="page visible" id="page-overview">
  <section class="card onboard" id="onboarding" style="display:none">
    <div class="section-title">
      <div><div class="label">İlk kurulum</div><h2 id="onboardTitle">Anahtar gerekmez: IDE MODEL modu aktif</h2></div>
      <span class="pill">zero-config</span>
    </div>
    <div class="explain" id="onboardBody">
      <div>Context Intelligence, IDE'nizin kendi modeliyle çalışır; hiçbir provider anahtarı zorunlu değildir.</div>
      <div>Provider LLM Routing varsayılan olarak kapalıdır; proje için Model Execution sayfasından açılır.</div>
      <div>Models sayfasındaki seçimler tüm projeler için globaldir. Router kapalıyken Context Optimization çalışmaya devam eder.</div>
    </div>
  </section>
  <section class="hero">
    <div class="card primary-card">
      <div class="section-title">
        <div><div class="label">Son context paketi</div><h2>LLM'e gerçekte ne kadar kod gitti?</h2></div>
        <span class="pill" id="lastClient">kaynak: -</span>
      </div>
      <div class="metric-grid">
        <div class="metric emphasis">
          <div class="label">Tasarruf (Index Bazlı)</div>
          <div class="value good" id="savingsValue">-</div>
          <div class="hint" id="savingsHint">Tam repo baseline'a göre azalma.</div>
          <div class="bar"><div class="fill" id="savingsBar"></div></div>
        </div>
        <div class="metric blue">
          <div class="label">Kaç Kat Daha Az (Index Bazlı)</div>
          <div class="value" id="factorValue">-</div>
          <div class="hint">Tam repo token / son paket token.</div>
        </div>
        <div class="metric warn">
          <div class="label">Context Quality</div>
          <div class="value" id="quality">-</div>
          <div class="hint" id="qualityHint">Seçilen context'in yeterlilik sinyali.</div>
          <div class="bar"><div class="fill" id="qualityBar"></div></div>
        </div>
      </div>
      <p class="muted" id="statusLine">Hazır.</p>
    </div>
    <div class="card">
      <div class="section-title"><div><div class="label">Token karşılaştırması</div><h2>Full repo vs seçili context</h2></div></div>
      <div class="three">
        <div class="metric"><div class="label">Repo Baseline (Index Tahmini)</div><div class="value small" id="repoTokens">-</div><div class="hint">Index'ten hesaplanan, tüm repo gönderilse oluşacak yaklaşık token.</div></div>
        <div class="metric"><div class="label">Son Paket (LLM'e Giden)</div><div class="value small good" id="ctxTokens">-</div><div class="hint">Son görev için seçilip gönderilen context token miktarı.</div></div>
        <div class="metric"><div class="label">Paket / Baseline</div><div class="value small" id="packageRatio">-</div><div class="hint">Düşük olması token açısından iyidir.</div></div>
      </div>
      <div class="explain" style="margin-top:14px">
        <div><strong>Repo Baseline</strong> index'in (map.json) ürettiği yaklaşık "full repo" token tahminidir.</div>
        <div><strong>Son Paket</strong> Context Agent'in son görev için seçtiği ve LLM'e gönderdiği gerçek token miktarıdır.</div>
        <div><strong>Quality</strong> düşükse tasarruf yüksek olsa bile LLM eksik bilgiyle kalabilir.</div>
      </div>
      <div class="inline-panel">
        <div class="section-title">
          <div><div class="label">Kaynak Bazlı Kullanım</div><h2>MCP client kırılımı</h2></div>
          <span class="pill" id="usagePreviewTotal">toplam: -</span>
        </div>
        <div class="mini-usage" id="usagePreview">
          <div class="mini-row"><strong>Henüz kayıt yok</strong><span>-</span><span>-</span></div>
        </div>
      </div>
    </div>
  </section>

  <section class="stat-grid">
    <div class="metric"><div class="label">Index Dosya</div><div class="value small" id="files">-</div><div class="hint">Indexlenen kaynak dosyalar.</div></div>
    <div class="metric"><div class="label">Sembol</div><div class="value small" id="symbols">-</div><div class="hint">Bulunabilir class/fonksiyon/semboller.</div></div>
    <div class="metric blue"><div class="label">MCP Client Kullanımı</div><div class="value small" id="clientCount">-</div><div class="hint" id="clientHint">Platform ilk gerçek kullanımda otomatik tespit edilir.</div></div>
    <div class="metric"><div class="label">Eval Hit Rate</div><div class="value small good" id="evalRate">-</div><div class="hint">Routing test setinde başarı oranı.</div></div>
    <div class="metric"><div class="label">Capsule Context</div><div class="value small" id="capsuleCount">-</div><div class="hint">Aktif görev hafızasındaki context sayısı.</div></div>
  </section>
  </div>

  <div class="page" id="page-context">
  <section class="card">
    <div class="section-title">
      <div><div class="label">Context Intelligence</div><h2>Görev için context paketi üret</h2></div>
      <span class="pill">routing + machine_readable</span>
    </div>
    <div class="task">
      <input id="task" value="Add rate limiting to login" placeholder="Görev yaz...">
      <input id="budget" value="8000" title="Token budget">
    </div>
    <div class="row" style="margin-top:10px">
      <button onclick="runIndexAndBuild()">Index + Paket Kur</button>
      <button class="secondary" onclick="buildContext()">Sadece Paket</button>
      <button class="secondary" onclick="runEval()">Eval Çalıştır</button>
    </div>
  </section>
  <section class="split">
    <div class="card">
      <div class="section-title">
        <div><div class="label">Context Items</div><h2>LLM'e gönderilen gerçek kod parçaları</h2></div>
        <span class="pill" id="itemSummary">paket yok</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Dosya</th><th>Mod</th><th>Token</th><th>Neden seçildi</th><th>Durum</th></tr></thead>
          <tbody id="items"><tr><td colspan="5" class="muted">Henüz paket oluşturulmadı.</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="section-title"><div><div class="label">Plan ve doğrulama</div><h2>Kalite sinyalleri</h2></div></div>
      <pre id="plan">{}</pre>
    </div>
  </section>

  <section class="card">
    <div class="section-title">
      <div><div class="label">Kaynak Bazlı Kullanım</div><h2>Hangi MCP client ne kadar tasarruf sağladı?</h2></div>
      <span class="pill" id="usageTotals">toplam: -</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Kaynak</th><th>Paket/Tool</th><th>Context Token</th><th>Tasarruf</th><th>Son Görev</th><th>Scope</th></tr></thead>
        <tbody id="usageRows"><tr><td colspan="6" class="muted">Henüz kullanım kaydı yok.</td></tr></tbody>
      </table>
    </div>
    <div class="hint" id="scopeHint" style="margin-top:8px">Scope verisi henüz oluşmadı.</div>
  </section>

  <section class="card">
    <div class="section-title"><div><div class="label">Seçili içerik önizleme</div><h2>Kod parçası</h2></div></div>
    <pre id="preview">Bir context item seç.</pre>
  </section>
  </div>

  <div class="page" id="page-projects">
  <section class="card">
    <div class="section-title"><div><div class="label">Projects</div><h2>Kayıtlı projeler ve multi-project izolasyonu</h2></div></div>
    <div class="row" style="margin-bottom:12px">
      <input id="projPathInput" class="path-input" style="width:420px" placeholder="İçinde .context olan proje klasörü...">
      <button onclick="addProjectFromPage()">Projeyi Ekle</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Proje</th><th>Root</th><th>Panel URL</th><th>Geç</th></tr></thead>
      <tbody id="projectsRows"><tr><td colspan="4" class="muted">Yükleniyor...</td></tr></tbody>
    </table></div>
    <div class="hint" style="margin-top:8px">Her proje kendi .context dizininde izole çalışır: index, memory, config ve router verileri projeler arasında karışmaz.</div>
  </section>
  </div>

  <div class="page" id="page-memory">
  <section class="card">
    <div class="section-title">
      <div><div class="label">Project Memory</div><h2>Kalıcı proje hafızası (Project Memory ≠ Model Knowledge)</h2></div>
      <button class="secondary compact" onclick="loadMemory()">Yenile</button>
    </div>
    <div class="explain" style="margin-bottom:10px">
      <div>Project Memory yalnız bu sistemde saklanır; hiçbir modelin ön-eğitim bilgisine dayanmaz ve model bilgisi gibi sunulmaz.</div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>ID</th><th>Tür</th><th>İçerik</th><th>Kaynak</th><th>Güven</th><th>Durum</th></tr></thead>
      <tbody id="memoryRows"><tr><td colspan="6" class="muted">Henüz memory kaydı yok.</td></tr></tbody>
    </table></div>
  </section>
  </div>

  <div class="page" id="page-sessions">
  <section class="split">
    <div class="card">
      <div class="section-title">
        <div><div class="label">IDE Sessions</div><h2>Aktif oturum ve kimlik</h2></div>
        <button class="secondary compact" onclick="loadSessions()">Yenile</button>
      </div>
      <div class="row" style="margin-bottom:10px">
        <span class="pill" id="contPill">CROSS_IDE_CONTINUITY: -</span>
        <span class="pill" id="idePill">IDE: -</span>
      </div>
      <pre id="identityPre">Yükleniyor...</pre>
    </div>
    <div class="card">
      <div class="section-title"><div><div class="label">SessionHandoff</div><h2>IDE değişiminde devredilen durum</h2></div></div>
      <pre id="handoffPre">Yükleniyor...</pre>
    </div>
  </section>
  <section class="card">
    <div class="section-title"><div><div class="label">Session History</div><h2>Oturum geçmişi</h2></div></div>
    <pre id="sessionHistoryPre">Yükleniyor...</pre>
  </section>
  </div>

  <div class="page" id="page-execution">
  <section class="card">
    <div class="section-title">
      <div><div class="label">Model Execution</div><h2>Yürütme modları</h2></div>
      <button class="secondary compact" onclick="loadExecution()">Yenile</button>
    </div>
    <div class="stat-grid" style="grid-template-columns:repeat(4,minmax(0,1fr))">
      <div class="metric"><div class="label">Configured Mode</div><div class="value small" id="execMode">-</div><div class="hint">Ayarlanan yürütme modu.</div></div>
      <div class="metric emphasis"><div class="label">Effective Mode</div><div class="value small good" id="execEffective">-</div><div class="hint" id="execNote">Gerçekte çalışan mod.</div></div>
      <div class="metric"><div class="label">Usable Models</div><div class="value small" id="execUsable">-</div><div class="hint">Etkin provider + anahtar koşullarını sağlayan modeller.</div></div>
      <div class="metric"><div class="label">Gateway</div><div class="value small" id="execGateway">-</div><div class="hint" id="execGatewayHint">Model Gateway durumu.</div></div>
    </div>
    <div class="split" style="margin-top:14px" id="featureToggles">
      <label class="feature-switch">
        <input type="checkbox" id="contextToggle" onchange="setFeatureToggle('context', this.checked)">
        <span class="copy"><strong>1. Context Optimization</strong><span class="muted">IDE modeline tüm repo yerine seçilmiş, güncel ve kısa context gönderir.</span></span>
      </label>
      <label class="feature-switch">
        <input type="checkbox" id="routingToggle" onchange="setFeatureToggle('routing', this.checked)">
        <span class="copy"><strong>2. Provider LLM Routing</strong><span class="muted">Görevi kalite, risk ve maliyete göre proje için izin verilen modele yönlendirir.</span></span>
      </label>
    </div>
    <div class="explain" style="margin-top:10px">
      <div><strong>Yalnız Context ON</strong>: IDE'nin kendi modeli çalışır; Context Agent yalnız gerekli proje bilgisini taşır.</div>
      <div><strong>Context + Routing ON</strong>: Context Agent görevi proje için izinli provider modellerinden uygun olana yürütür; IDE sonucu uygular ve doğrular.</div>
      <div><strong>İkisi de OFF</strong>: MCP bağlı kalsa da yürütmeye karışmaz; IDE normal dosya okuma ve kendi modeliyle devam eder.</div>
    </div>
  </section>
  <section class="card">
    <div class="section-title"><div><div class="label">Route Preview</div><h2>Görev için routing kararı önizle</h2></div></div>
    <div class="task">
      <input id="routeTask" placeholder="Görev tanımı (ör. fix payment retry bug)">
      <input id="routeTokens" value="4000" title="Context token tahmini">
    </div>
    <div class="row" style="margin-top:10px">
      <button onclick="previewRoute()">Route Önizle</button>
      <button class="secondary" onclick="previewDelegate()">Hybrid Delegate</button>
    </div>
    <pre id="routePreviewPre" style="margin-top:12px">Henüz önizleme yok. Not: router önce Context Intelligence'ın machine_readable analizini tüketir; burada basit analiz gönderilir.</pre>
  </section>
  </div>

  <div class="page" id="page-providers">
  <section class="card">
    <div class="section-title">
      <div><div class="label">AI Providers</div><h2>Provider konfigürasyonu ve sağlık</h2></div>
      <div class="row"><button class="secondary compact" onclick="loadProviders()">Yenile</button><button class="ghost compact" onclick="checkAllHealth()">Health Kontrol</button></div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Provider</th><th>Tür</th><th>Base URL</th><th>Enabled</th><th>Anahtar</th><th>Health</th><th>İşlem</th></tr></thead>
      <tbody id="providerRows"><tr><td colspan="7" class="muted">Yükleniyor...</td></tr></tbody>
    </table></div>
    <div class="hint" style="margin-top:8px">Provider bağlantıları, anahtarlar ve routing model havuzu makine genelidir. Context ve Provider Routing aç/kapat tercihleri proje bazındadır. Anahtarlar yalnız maskelenmiş gösterilir.</div>
  </section>
  <section class="card">
    <div class="section-title"><div><div class="label">Secrets</div><h2>Stored secrets (mask only)</h2></div><button class="secondary compact" onclick="loadSecrets()">Yenile</button></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Provider</th><th>Mask</th><th>Yöntem</th><th>Güncellendi</th><th>İşlem</th></tr></thead>
      <tbody id="secretRows"><tr><td colspan="5" class="muted">Kayıtlı anahtar yok. Bu bir hata değildir; IDE MODEL anahtarsız çalışır.</td></tr></tbody>
    </table></div>
  </section>
  </div>

  <div class="page" id="page-models">
  <section class="card">
    <div class="section-title">
      <div><div class="label">Models</div><h2>Global routing model havuzu</h2></div>
      <button class="secondary compact" onclick="loadModels()">Yenile</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Model</th><th>Provider</th><th>Remote</th><th>Tier</th><th>In $/1M</th><th>Out $/1M</th><th>Latency</th><th>Global</th><th>İşlem</th></tr></thead>
      <tbody id="modelRows"><tr><td colspan="9" class="muted">Yükleniyor...</td></tr></tbody>
    </table></div>
  </section>
  </div>

  <div class="page" id="page-router">
  <section class="split">
    <div class="card">
      <div class="section-title">
        <div><div class="label">Auto Router</div><h2>Router durumu</h2></div>
        <button class="secondary compact" onclick="loadRouter()">Yenile</button>
      </div>
      <pre id="routerStatusPre">Yükleniyor...</pre>
      <div class="hint">Router kapalıyken (NOT ENABLED) bu bir hata değildir: Context Intelligence tam çalışmaya devam eder. Hedef: en düşük beklenen maliyetle doğru, doğrulanmış sonuç.</div>
    </div>
    <div class="card">
      <div class="section-title"><div><div class="label">Routing Policy</div><h2>SMART matrix + guardrails</h2></div></div>
      <pre id="policyPre">Yükleniyor...</pre>
    </div>
  </section>
  </div>

  <div class="page" id="page-usagecost">
  <section class="split">
    <div class="card">
      <div class="section-title">
        <div><div class="label">Usage &amp; Cost</div><h2>Router kullanım ve tahmini maliyet</h2></div>
        <button class="secondary compact" onclick="loadUsageCost()">Yenile</button>
      </div>
      <div class="three">
        <div class="metric"><div class="label">Bugün</div><div class="value small" id="costToday">-</div><div class="hint" id="costTodayRoutes">route: -</div></div>
        <div class="metric"><div class="label">Bu Ay</div><div class="value small" id="costMonth">-</div><div class="hint" id="costMonthRoutes">route: -</div></div>
        <div class="metric"><div class="label">Son Görev</div><div class="value small" id="costLast">-</div><div class="hint">Son routing kaydının tahmini maliyeti.</div></div>
      </div>
      <div class="table-wrap" style="margin-top:12px"><table>
        <thead><tr><th>Model</th><th>Route</th><th>Tahmini Maliyet</th></tr></thead>
        <tbody id="costByModel"><tr><td colspan="3" class="muted">Henüz router kullanım kaydı yok.</td></tr></tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="section-title"><div><div class="label">Context Usage</div><h2>MCP client tasarruf özeti</h2></div></div>
      <pre id="ctxUsagePre">Overview sayfasındaki kullanım kartına bakın; burada ham özet gösterilir.</pre>
    </div>
  </section>
  </div>

  <div class="page" id="page-history">
  <section class="card">
    <div class="section-title">
      <div><div class="label">Routing History</div><h2>Routing karar geçmişi (router.db)</h2></div>
      <button class="secondary compact" onclick="loadHistory()">Yenile</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>ID</th><th>Zaman</th><th>Görev</th><th>Karar</th><th>Model</th><th>Tier</th><th>Executed By</th><th>Maliyet</th></tr></thead>
      <tbody id="historyRows"><tr><td colspan="8" class="muted">Henüz routing kaydı yok.</td></tr></tbody>
    </table></div>
    <pre id="historyDetail" style="margin-top:12px">Bir kayıt seç.</pre>
  </section>
  </div>

  <div class="page" id="page-diagnostics">
  <section class="card">
    <div class="section-title">
      <div><div class="label">Diagnostics</div><h2>Sanitize edilmiş sistem raporu</h2></div>
      <button class="secondary compact" onclick="loadDiagnostics()">Rapor Üret</button>
    </div>
    <div class="hint" style="margin-bottom:10px">Bu rapor anahtar, token veya hassas içerik barındırmaz; yalnız durum ve konfigürasyon kaynaklarını gösterir.</div>
    <pre id="diagnosticsPre">Rapor Üret'e basın.</pre>
  </section>
  </div>

  <div class="page" id="page-settings">
  <section class="card">
    <div class="section-title">
      <div><div class="label">Settings</div><h2>Hiyerarşik konfigürasyon (SYSTEM → GLOBAL → PROJECT → SESSION → TASK)</h2></div>
      <button class="secondary compact" onclick="loadSettings()">Yenile</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Anahtar</th><th>Efektif Değer</th><th>Kaynak</th><th>Yeni Değer</th><th>İşlem</th></tr></thead>
      <tbody id="configRows"><tr><td colspan="5" class="muted">Yükleniyor...</td></tr></tbody>
    </table></div>
    <div class="hint" style="margin-top:8px">SYSTEM katmanı değiştirilemez; TASK override kalıcı olmaz. RESET TO INHERITED üst katman değerine döndürür. Routing tercihleri PROJECT/GLOBAL katmanında tutulur (IDE'ye bağlı değildir).</div>
  </section>
  </div>
</main>
<script>
let lastPackage = null;
let autoRefreshTimer = null;
let selectedRoot = '';
let autoBuildTimer = null;
function fmt(n){ return Number(n || 0).toLocaleString('tr-TR'); }
function pct(n){ return `%${Number(n || 0).toFixed(1)}`; }
function ratio(repo, total){ return total > 0 ? `${(repo / total).toFixed(1)}x` : '-'; }
function qualityClass(score){ return score >= 75 ? 'good' : score >= 45 ? 'warn' : 'bad'; }
function clientLabel(value){
  const raw = (value || '').trim();
  if(!raw) return '-';
  if(raw === 'direct') return 'Dashboard / Manuel';
  return raw;
}
function setBusy(isBusy){
  const inputs = [document.getElementById('projectSelect'), document.getElementById('pathInput'), document.getElementById('task'), document.getElementById('budget')];
  inputs.forEach(el => { if(el) el.disabled = isBusy; });
}
function stamp(){
  document.getElementById('lastUpdated').textContent = 'son: ' + new Date().toLocaleTimeString('tr-TR', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
async function api(path, opts){
  const joiner = path.includes('?') ? '&' : '?';
  const scoped = selectedRoot ? `${path}${joiner}root=${encodeURIComponent(selectedRoot)}` : path;
  const res = await fetch(scoped, opts);
  const data = await res.json();
  if(!res.ok || data.error) throw new Error(data.error || data.message || res.statusText);
  return data;
}
function projectLabel(root){
  if(!root) return 'Bu proje';
  const parts = root.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || root;
}
async function loadProjects(){
  try {
    const data = await api('/api/projects');
    const projects = data.projects || [];
    const select = document.getElementById('projectSelect');
    select.innerHTML = projects.map(p => `<option value="${p.root}">${projectLabel(p.root)}</option>`).join('');
    if(data.current_root && !selectedRoot) selectedRoot = data.current_root;
    if(selectedRoot) select.value = selectedRoot;
    document.getElementById('pathInput').value = selectedRoot || '';
  } catch(e) {}
}
function selectProject(root){
  selectedRoot = root;
  document.getElementById('pathInput').value = root || '';
  lastPackage = null;
  renderQuality({});
  document.getElementById('items').innerHTML = '<tr><td colspan="5" class="muted">Henüz paket oluşturulmadı.</td></tr>';
  document.getElementById('itemSummary').textContent = 'paket yok';
  document.getElementById('preview').textContent = 'Bir context item seç.';
  scheduleAutoBuild();
}
async function openPath(){
  const raw = document.getElementById('pathInput').value.trim();
  if(!raw) return;
  try {
    const payload = await api('/api/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({add: raw}),
    });
    selectedRoot = payload.current_root || raw;
    await loadProjects();
    selectProject(selectedRoot);
  } catch(e) {
    document.getElementById('statusLine').innerHTML = '<span class="error">'+e.message+'</span>';
  }
}
function scheduleAutoBuild(){
  if(autoBuildTimer) clearTimeout(autoBuildTimer);
  autoBuildTimer = setTimeout(() => runIndexAndBuild(), 150);
}
function setTokenMetrics(repo, total){
  const usedPct = repo ? (total / repo * 100) : 0;
  const saving = repo ? Math.max(0, 100 - usedPct) : 0;
  document.getElementById('repoTokens').textContent = fmt(repo);
  document.getElementById('ctxTokens').textContent = fmt(total);
  document.getElementById('packageRatio').textContent = repo ? pct(usedPct) : '-';
  document.getElementById('savingsValue').textContent = repo ? pct(saving) : '-';
  document.getElementById('factorValue').textContent = repo ? ratio(repo, total) : '-';
  document.getElementById('savingsHint').textContent = repo ? `${fmt(Math.max(0, repo - total))} token daha az, full-repo baseline'a göre.` : 'Context paketi oluşturulunca hesaplanır.';
  document.getElementById('savingsBar').style.width = Math.min(100, saving) + '%';
}
function resetPackageMetrics(repo){
  document.getElementById('repoTokens').textContent = fmt(repo);
  document.getElementById('ctxTokens').textContent = '-';
  document.getElementById('packageRatio').textContent = '-';
  document.getElementById('savingsValue').textContent = '-';
  document.getElementById('factorValue').textContent = '-';
  document.getElementById('savingsHint').textContent = 'Context paketi oluşturulunca hesaplanır.';
  document.getElementById('savingsBar').style.width = '0%';
}
function renderQuality(q){
  const score = q && q.score != null ? q.score : null;
  const cls = qualityClass(score || 0);
  const el = document.getElementById('quality');
  el.textContent = score != null ? `${score}/100` : '-';
  el.className = 'value ' + cls;
  const bar = document.getElementById('qualityBar');
  bar.className = 'fill ' + cls;
  bar.style.width = Math.min(100, score || 0) + '%';
  const bits = [];
  if(q && q.confidence) bits.push(`confidence: ${q.confidence}`);
  if(q && q.has_focus_symbols) bits.push('focused symbols var');
  if(q && q.all_fresh) bits.push('fresh');
  document.getElementById('qualityHint').textContent = bits.length ? bits.join(' - ') : 'Context paketi oluşturulunca kalite görünür.';
}
async function refreshStatus(){
  try {
    const data = await api('/api/status');
    document.getElementById('root').textContent = 'root: ' + (data.root || '-');
    document.getElementById('files').textContent = fmt(data.files);
    document.getElementById('symbols').textContent = fmt(data.symbols);
    if(!lastPackage) resetPackageMetrics(data.total_tokens || 0);
    document.getElementById('capsuleCount').textContent = data.capsule ? fmt(data.capsule.context_count || (data.capsule.context_items || []).length) : '-';
    renderUsage(data.usage || {});
    document.getElementById('statusLine').textContent = 'Index: ' + (data.last_indexed || '-');
    stamp();
  } catch(e) { document.getElementById('statusLine').innerHTML = '<span class="error">'+e.message+'</span>'; }
}
function setAutoRefresh(value, silent){
  if(autoRefreshTimer) clearInterval(autoRefreshTimer);
  autoRefreshTimer = null;
  const ms = Number(value || 0);
  if(ms > 0) autoRefreshTimer = setInterval(refreshStatus, ms);
  if(!silent) document.getElementById('statusLine').textContent = ms > 0 ? `Auto refresh aktif: ${ms / 1000} sn` : 'Auto refresh kapalı.';
}
function renderUsage(usage){
  const clients = usage.clients || [];
  const scopes = usage.scopes || [];
  const recent = usage.recent || [];
  const totals = usage.totals || {};
  const last = recent[0] || {};
  const clientNames = clients.map(c => c.client).filter(Boolean);
  const lastClient = last.client || clientNames[0] || '';
  document.getElementById('lastClient').textContent = lastClient ? `kaynak: ${clientLabel(lastClient)}` : 'kaynak: -';
  document.getElementById('clientCount').textContent = clients.length ? fmt(clients.length) : '-';
  document.getElementById('clientHint').textContent = clients.length ? `Son kaynak: ${clientLabel(lastClient)} - toplam paket: ${fmt(totals.events || 0)}` : 'Henüz gerçek MCP client kullanımı kaydı yok.';
  document.getElementById('usageTotals').textContent = totals.events
    ? `toplam tasarruf: ${pct(totals.savings_pct)} (paket ${fmt(totals.package_events || 0)} / tool ${fmt(totals.tool_events || 0)})`
    : 'toplam: -';
  document.getElementById('usagePreviewTotal').textContent = totals.events
    ? `${fmt(totals.package_events || 0)} paket / ${fmt(totals.tool_events || 0)} tool - ${pct(totals.savings_pct)}`
    : 'toplam: -';
  document.getElementById('usagePreview').innerHTML = clients.length ? clients.slice(0, 3).map(c => `
    <div class="mini-row">
      <strong>${clientLabel(c.client)}</strong>
      <span>${fmt(c.package_events || 0)} paket / ${fmt(c.tool_events || 0)} tool</span>
      <span>${pct(c.savings_pct)}</span>
    </div>`).join('') : '<div class="mini-row"><strong>Henüz kayıt yok</strong><span>-</span><span>-</span></div>';
  document.getElementById('usageRows').innerHTML = clients.length ? clients.map(c => `
    <tr>
      <td>${clientLabel(c.client)}</td>
      <td>${fmt(c.package_events || 0)} / ${fmt(c.tool_events || 0)}</td>
      <td>${fmt(c.context_tokens)}</td>
      <td>${pct(c.savings_pct)}</td>
      <td>${c.last_task || '-'}</td>
      <td>${c.last_scope || '-'}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="muted">Henüz kullanım kaydı yok.</td></tr>';
  document.getElementById('scopeHint').textContent = scopes.length
    ? `Aktif scope: ${scopes.slice(0, 3).map(s => `${s.scope} (${fmt(s.events)} paket)`).join(' - ')}`
    : 'Scope verisi henüz oluşmadı.';
}
async function runIndex(){
  document.getElementById('statusLine').textContent = 'Index alınıyor...';
  await api('/api/index', {method:'POST'});
  await refreshStatus();
}
async function runIndexAndBuild(){
  setBusy(true);
  document.getElementById('statusLine').textContent = 'Index alınıyor, ardından context paketi hazırlanacak...';
  try {
    await api('/api/index', {method:'POST'});
    await buildContext({keepBusy:true});
  } catch(e) {
    document.getElementById('statusLine').innerHTML = '<span class="error">'+e.message+'</span>';
  } finally {
    setBusy(false);
  }
}
async function runEval(){
  document.getElementById('statusLine').textContent = 'Eval koşuyor...';
  const data = await api('/api/eval');
  document.getElementById('evalRate').textContent = data.total ? pct(data.hit_rate * 100) : '-';
  document.getElementById('plan').textContent = JSON.stringify({ eval: {
    total: data.total, passed: data.passed, failed: data.failed,
    hit_rate: data.hit_rate,
    failed_tasks: (data.results || []).filter(x => !x.passed).map(x => x.task)
  }}, null, 2);
  document.getElementById('statusLine').textContent = 'Eval hazır.';
}
function itemTokens(item){
  if(item.token_estimate) return item.token_estimate;
  if(item.symbols) return item.symbols.reduce((a,s)=>a+(s.token_estimate||0),0);
  return 0;
}
async function buildContext(options){
  const keepBusy = options && options.keepBusy;
  if(!keepBusy) setBusy(true);
  document.getElementById('statusLine').textContent = 'Context paketi hazırlanıyor...';
  try {
    const task = document.getElementById('task').value;
    const budget = Number(document.getElementById('budget').value || '8000');
    const strictQuality = 80;
    const data = await api('/api/context', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task, budget, fresh: true, strict_quality: strictQuality}),
    });
    lastPackage = data;
    if (data.client) {
      const source = data.scope ? `${clientLabel(data.client)} (${data.scope})` : clientLabel(data.client);
      document.getElementById('lastClient').textContent = `kaynak: ${source}`;
    }
    const items = data.context_items || [];
    const total = data.context_tokens || items.reduce((a,i)=>a+itemTokens(i),0);
    const repo = data.repo_tokens || 0;
    renderQuality(data.context_quality || {});
    setTokenMetrics(repo, total);
    document.getElementById('itemSummary').textContent = `${items.length} parça - ${fmt(total)} token`;
    document.getElementById('items').innerHTML = items.length ? items.map((item, idx)=>`
      <tr onclick="showItem(${idx})" style="cursor:pointer">
        <td>${item.file}</td>
        <td>${item.action || '-'}</td>
        <td>${fmt(itemTokens(item))}</td>
        <td>${item.reason || (item.why || []).join('; ') || '-'}</td>
        <td>${item.freshness && item.freshness.reindexed ? 're-indexed' : 'fresh'}</td>
      </tr>`).join('') : '<tr><td colspan="5" class="muted">Context item yok.</td></tr>';
    document.getElementById('plan').textContent = JSON.stringify({
      confidence: data.routing && data.routing.confidence,
      quality: data.context_quality,
      strict_quality: data.strict_quality,
      capsule: data.capsule,
      usage: data.usage,
      context_plan: data.routing && data.routing.context_plan,
      execution_plan: data.routing && data.routing.execution_plan,
      budget: data.budget
    }, null, 2);
    showItem(0);
    await refreshStatus();
    document.getElementById('statusLine').textContent = 'Paket hazır.';
  } catch(e) {
    document.getElementById('statusLine').innerHTML = '<span class="error">'+e.message+'</span>';
  } finally {
    if(!keepBusy) setBusy(false);
  }
}
function showItem(idx){
  const item = lastPackage && lastPackage.context_items && lastPackage.context_items[idx];
  if(!item) return;
  let text = item.content || '';
  if(item.symbols && item.symbols.length) text = item.symbols.map(s=>`# ${s.signature || s.name}\n${s.content || ''}`).join('\n\n');
  document.getElementById('preview').textContent = text || JSON.stringify(item, null, 2);
}
// ── Control Plane (autoroute §12-14) ──
function escHtml(value){
  return String(value == null ? '' : value).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function pre(el, data){ document.getElementById(el).textContent = JSON.stringify(data, null, 2); }
function badge(value, kind){ return `<span class="badge ${kind || 'muted'}">${escHtml(value)}</span>`; }
const pageLoaders = {
  projects: loadProjectsPage, memory: loadMemory, sessions: loadSessions,
  execution: loadExecution, providers: loadProviders, models: loadModels,
  router: loadRouterPage, usagecost: loadUsageCost, history: loadHistory,
  diagnostics: loadDiagnostics, settings: loadSettings,
};
function showPage(name){
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('visible', p.id === 'page-' + name));
  document.querySelectorAll('#pageNav button').forEach(b => b.classList.toggle('active', b.dataset.page === name));
  const loader = pageLoaders[name];
  if(loader) loader();
}
async function loadProjectsPage(){
  try {
    const data = await api('/api/projects');
    const projects = data.projects || [];
    document.getElementById('projectsRows').innerHTML = projects.length ? projects.map(p => `
      <tr>
        <td><strong>${escHtml(projectLabel(p.root))}</strong></td>
        <td class="mono">${escHtml(p.root)}</td>
        <td class="mono">${escHtml(p.url || '-')}</td>
        <td><button class="secondary compact" onclick="selectProject('${escHtml(p.root)}'); showPage('overview')">Seç</button></td>
      </tr>`).join('') : '<tr><td colspan="4" class="muted">Kayıtlı proje yok.</td></tr>';
  } catch(e) { pre('projectsRows', {error: e.message}); }
}
function addProjectFromPage(){
  const raw = document.getElementById('projPathInput').value.trim();
  if(!raw) return;
  document.getElementById('pathInput').value = raw;
  openPath().then(() => loadProjectsPage());
}
async function loadMemory(){
  try {
    const data = await api('/api/memory');
    const rows = data.memories || data.items || [];
    document.getElementById('memoryRows').innerHTML = rows.length ? rows.map(m => `
      <tr>
        <td class="mono">${escHtml(m.id != null ? m.id : '-')}</td>
        <td>${badge(m.type || '-', 'muted')}</td>
        <td>${escHtml(m.content || '')}</td>
        <td>${escHtml(m.source || '-')}</td>
        <td>${escHtml(m.confidence != null ? m.confidence : '-')}</td>
        <td>${badge(m.status || '-', m.status === 'ACTIVE' ? 'good' : 'warn')}</td>
      </tr>`).join('') : '<tr><td colspan="6" class="muted">Henüz memory kaydı yok.</td></tr>';
  } catch(e) { document.getElementById('memoryRows').innerHTML = `<tr><td colspan="6" class="muted">${escHtml(e.message)}</td></tr>`; }
}
async function loadSessions(){
  try {
    const [identity, continuity, handoff, sessions] = await Promise.all([
      api('/api/identity'), api('/api/continuity'), api('/api/handoff'), api('/api/sessions'),
    ]);
    pre('identityPre', identity);
    pre('handoffPre', handoff);
    const level = continuity.cross_ide_continuity || continuity.status || '-';
    const kind = level === 'VERIFIED' ? 'good' : level === 'PARTIAL' ? 'warn' : 'bad';
    document.getElementById('contPill').innerHTML = `CROSS_IDE_CONTINUITY: ${badge(level, kind)}`;
    document.getElementById('idePill').textContent = 'IDE: ' + ((identity.identity || identity).ide_instance_id || '-');
    pre('sessionHistoryPre', sessions.history || sessions);
  } catch(e) { pre('identityPre', {error: e.message}); }
}
async function loadExecution(){
  try {
    const status = await api('/api/router/status');
    document.getElementById('execMode').textContent = status.execution_mode || '-';
    document.getElementById('execEffective').textContent = status.effective_mode || '-';
    document.getElementById('execNote').textContent = status.mode_note || 'AUTO ROUTER modu, gateway yoksa IDE MODEL olarak çalışır.';
    document.getElementById('execUsable').textContent = status.usable_models != null ? status.usable_models : '-';
    const gw = status.gateway || {};
    document.getElementById('execGateway').textContent = gw.available ? 'ONLINE' : 'OFFLINE';
    document.getElementById('execGatewayHint').textContent = gw.available ? `127.0.0.1:${gw.port} (pid ${gw.pid})` : 'Gateway kapali; AUTO ROUTER bu durumda IDE MODEL moduna duser.';
    document.getElementById('contextToggle').checked = status.context_optimization_enabled !== false;
    document.getElementById('routingToggle').checked = !!status.provider_routing_enabled;
    document.getElementById('routingToggle').disabled = status.context_optimization_enabled === false;
  } catch(e) { document.getElementById('execNote').textContent = e.message; }
}
async function setFeatureToggle(feature, enabled){
  try {
    if(feature === 'context'){
      await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'set', layer:'PROJECT', key:'context.optimization_enabled', value:enabled})});
      if(!enabled){
        await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({action:'set', layer:'PROJECT', key:'routing.enabled', value:false})});
        await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({action:'set', layer:'PROJECT', key:'execution.mode', value:'IDE_MODEL'})});
      }
    } else {
      await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'set', layer:'PROJECT', key:'routing.enabled', value:enabled})});
      await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({action:'set', layer:'PROJECT', key:'execution.mode', value:enabled ? 'PROVIDER_ROUTING' : 'IDE_MODEL'})});
    }
    await loadExecution();
  } catch(e) { alert(e.message); await loadExecution(); }
}
async function setExecutionMode(mode){
  try {
    await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'set', layer:'PROJECT', key:'execution.mode', value:mode})});
    await loadExecution();
  } catch(e) { alert(e.message); }
}
async function ensureAnalysisFor(taskText){
  if(lastPackage && lastPackage.routing && Object.keys(lastPackage.routing.machine_readable || {}).length) {
    return lastPackage.routing.machine_readable;
  }
  const data = await api('/api/context', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({task: taskText || 'analyze task', budget: 8000, fresh: true, strict_quality: 80})});
  lastPackage = data;
  return (data.routing || {}).machine_readable || {};
}
async function previewRoute(){
  const task = document.getElementById('routeTask').value.trim() || 'analyze task';
  const tokens = Number(document.getElementById('routeTokens').value || 0);
  try {
    const analysis = await ensureAnalysisFor(task);
    const result = await api('/api/router/route', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({task, analysis, context_tokens: tokens})});
    pre('routePreviewPre', {analysis, decision: result});
  } catch(e) { pre('routePreviewPre', {error: e.message}); }
}
async function previewDelegate(){
  const task = document.getElementById('routeTask').value.trim() || 'analyze task';
  const tokens = Number(document.getElementById('routeTokens').value || 0);
  try {
    const analysis = await ensureAnalysisFor(task);
    const result = await api('/api/router/delegate', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({task, analysis, context_tokens: tokens})});
    pre('routePreviewPre', {analysis, delegation: result});
  } catch(e) { pre('routePreviewPre', {error: e.message}); }
}
async function loadProviders(){
  try {
    const data = await api('/api/providers');
    const providers = data.providers || [];
    document.getElementById('providerRows').innerHTML = providers.length ? providers.map(p => `
      <tr>
        <td><strong>${escHtml(p.id)}</strong></td>
        <td>${escHtml(p.type || '-')}</td>
        <td class="mono">${escHtml(p.base_url || '-')}</td>
        <td>${p.enabled ? badge('ENABLED','good') : badge('DISABLED','muted')}</td>
        <td>${p.needs_key ? (p.has_secret ? badge(p.mask || 'SET','good') : badge('NO KEY','warn')) : badge('NOT REQUIRED','muted')}</td>
        <td>${badge(p.health || 'UNKNOWN', p.health === 'CONNECTED' ? 'good' : p.health === 'AUTHENTICATION_FAILED' ? 'bad' : (p.health === 'RATE_LIMITED' || p.health === 'UNAVAILABLE') ? 'warn' : 'muted')}</td>
        <td>
          ${p.enabled
            ? `<button class="secondary compact" onclick="providerAction('disable','${escHtml(p.id)}')">Kapat</button>`
            : `<button class="compact" onclick="providerAction('enable','${escHtml(p.id)}')">Aç</button>`}
          ${p.needs_key ? `<button class="ghost compact" onclick="setSecret('${escHtml(p.id)}')">Anahtar</button>` : ''}
        </td>
      </tr>`).join('') : '<tr><td colspan="7" class="muted">Provider yok.</td></tr>';
    loadSecrets();
  } catch(e) { document.getElementById('providerRows').innerHTML = `<tr><td colspan="7" class="muted">${escHtml(e.message)}</td></tr>`; }
}
async function providerAction(action, id){
  try { await api('/api/providers', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action, id})}); await loadProviders(); }
  catch(e) { alert(e.message); }
}
async function checkAllHealth(){
  try { await api('/api/providers', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'health', force:true})}); }
  catch(e) {}
  await loadProviders();
}
async function setSecret(id){
  const value = prompt(`${id} için API anahtarı girin (yalnız 127.0.0.1 üzerinde işlenir, sadece maskesi saklanır/gösterilir):`);
  if(!value) return;
  try {
    await api('/api/secrets', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'set', provider_id:id, value})});
    await loadProviders();
  } catch(e) { alert(e.message); }
}
async function loadSecrets(){
  try {
    const data = await api('/api/secrets');
    const secrets = data.secrets || [];
    document.getElementById('secretRows').innerHTML = secrets.length ? secrets.map(s => `
      <tr>
        <td><strong>${escHtml(s.provider_id)}</strong></td>
        <td class="mono">${escHtml(s.mask)}</td>
        <td>${escHtml(s.method || '-')}</td>
        <td>${escHtml(s.updated_at || '-')}</td>
        <td>
          <button class="ghost compact" onclick="testSecret('${escHtml(s.provider_id)}')">Test</button>
          <button class="ghost compact" onclick="setSecret('${escHtml(s.provider_id)}')">Replace</button>
          <button class="ghost compact" onclick="deleteSecret('${escHtml(s.provider_id)}')">Delete</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="5" class="muted">Kayıtlı anahtar yok. Bu bir hata değildir; IDE MODEL anahtarsız çalışır.</td></tr>';
  } catch(e) { document.getElementById('secretRows').innerHTML = `<tr><td colspan="5" class="muted">${escHtml(e.message)}</td></tr>`; }
}
async function testSecret(id){
  try { const r = await api('/api/secrets', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'test', provider_id:id})}); alert(r.result || r.status || JSON.stringify(r)); }
  catch(e) { alert(e.message); }
}
async function deleteSecret(id){
  if(!confirm(`${id} anahtarı silinsin mi?`)) return;
  try { await api('/api/secrets', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'delete', provider_id:id})}); await loadProviders(); }
  catch(e) { alert(e.message); }
}
async function loadModels(){
  try {
    const data = await api('/api/models');
    const models = data.models || [];
    document.getElementById('modelRows').innerHTML = models.length ? models.map(m => `
      <tr>
        <td><strong>${escHtml(m.model_id)}</strong></td>
        <td>${escHtml(m.provider_id)}</td>
        <td class="mono">${escHtml(m.remote_model_name || '-')}</td>
        <td>${badge(m.quality_tier || '-', m.quality_tier === 'ECONOMY' ? 'good' : m.quality_tier === 'FRONTIER' ? 'warn' : 'muted')}</td>
        <td>${escHtml(m.input_cost != null ? m.input_cost : '-')}</td>
        <td>${escHtml(m.output_cost != null ? m.output_cost : '-')}</td>
        <td>${escHtml(m.latency_class || '-')}</td>
        <td>${m.global_enabled ? badge('IN ROUTING','good') : badge('EXCLUDED','muted')}</td>
        <td>${m.global_enabled
          ? `<button class="secondary compact" onclick="modelAction('disable','${escHtml(m.model_id)}')">Global çıkar</button>`
          : `<button class="compact" onclick="modelAction('enable','${escHtml(m.model_id)}')">Global ekle</button>`}</td>
      </tr>`).join('') : '<tr><td colspan="9" class="muted">Model kataloğu boş.</td></tr>';
  } catch(e) { document.getElementById('modelRows').innerHTML = `<tr><td colspan="9" class="muted">${escHtml(e.message)}</td></tr>`; }
}
async function modelAction(action, id){
  try { await api('/api/models', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action, id})}); await loadModels(); }
  catch(e) { alert(e.message); }
}
async function loadRouterPage(){
  try {
    const [status, policy] = await Promise.all([api('/api/router/status'), api('/api/policy')]);
    pre('routerStatusPre', status);
    pre('policyPre', policy);
  } catch(e) { pre('routerStatusPre', {error: e.message}); }
}
async function loadUsageCost(){
  try {
    const usage = await api('/api/router/usage');
    const today = usage.today || {};
    const month = usage.month || {};
    const last = usage.task_last || {};
    document.getElementById('costToday').textContent = '$' + Number(today.estimated_cost_usd || 0).toFixed(4);
    document.getElementById('costTodayRoutes').textContent = 'route: ' + (today.routes || 0);
    document.getElementById('costMonth').textContent = '$' + Number(month.estimated_cost_usd || 0).toFixed(4);
    document.getElementById('costMonthRoutes').textContent = 'route: ' + (month.routes || 0);
    document.getElementById('costLast').textContent = last.routes ? '$' + Number(last.estimated_cost_usd || 0).toFixed(4) : '-';
    const byModel = usage.by_model || [];
    document.getElementById('costByModel').innerHTML = byModel.length ? byModel.map(r => `
      <tr><td class="mono">${escHtml(r.final_model)}</td><td>${fmt(r.routes)}</td><td>$${Number(r.estimated_cost_usd || 0).toFixed(4)}</td></tr>`).join('')
      : '<tr><td colspan="3" class="muted">Henüz router kullanım kaydı yok.</td></tr>';
  } catch(e) { pre('costByModel', {error: e.message}); }
}
async function loadHistory(){
  try {
    const data = await api('/api/router/history?limit=50');
    const rows = data.history || [];
    document.getElementById('historyRows').innerHTML = rows.length ? rows.map(r => `
      <tr onclick="explainRoute(${r.id})" style="cursor:pointer">
        <td class="mono">${escHtml(r.id)}</td>
        <td>${escHtml((r.created_at || '').replace('T',' ').slice(0,19))}</td>
        <td>${escHtml((r.task || '').slice(0,60))}</td>
        <td>${badge(r.decision || '-', r.decision === 'EXECUTED' ? 'good' : r.decision === 'ROUTING_BLOCKED' ? 'bad' : 'warn')}</td>
        <td class="mono">${escHtml(r.final_model || '-')}</td>
        <td>${escHtml(r.required_tier || '-')}</td>
        <td>${escHtml(r.executed_by || '-')}</td>
        <td>${r.estimated_cost_usd != null ? '$' + Number(r.estimated_cost_usd).toFixed(5) : '-'}</td>
      </tr>`).join('') : '<tr><td colspan="8" class="muted">Henüz routing kaydı yok.</td></tr>';
  } catch(e) { document.getElementById('historyRows').innerHTML = `<tr><td colspan="8" class="muted">${escHtml(e.message)}</td></tr>`; }
}
async function explainRoute(id){
  try {
    const data = await api('/api/router/history?limit=200');
    const row = (data.history || []).find(r => r.id === id);
    pre('historyDetail', row || {error: 'not_found'});
  } catch(e) { pre('historyDetail', {error: e.message}); }
}
async function loadDiagnostics(){
  const report = {generated_at: new Date().toISOString(), note: 'sanitized: no secrets, no raw tokens, no file contents'};
  try { report.identity = await api('/api/continuity'); } catch(e) { report.identity = {error: e.message}; }
  try { report.router = await api('/api/router/status'); } catch(e) { report.router = {error: e.message}; }
  try { report.config = await api('/api/config'); } catch(e) { report.config = {error: e.message}; }
  try { report.gateway = await api('/api/gateway'); } catch(e) { report.gateway = {error: e.message}; }
  try { report.context_status = await api('/api/status'); } catch(e) { report.context_status = {error: e.message}; }
  pre('diagnosticsPre', report);
}
async function loadSettings(){
  try {
    const data = await api('/api/config');
    const entries = Object.entries(data.effective || data.config || data || {});
    document.getElementById('configRows').innerHTML = entries.length ? entries.map(([key, info]) => {
      const value = info && typeof info === 'object' ? info.value : info;
      const source = info && typeof info === 'object' ? info.source : '-';
      const editable = source !== 'SYSTEM';
      return `
      <tr>
        <td class="mono">${escHtml(key)}</td>
        <td><strong>${escHtml(value)}</strong></td>
        <td>${badge(source || '-', source === 'SYSTEM' ? 'muted' : source === 'PROJECT' ? 'good' : 'warn')}</td>
        <td><input id="cfg-${key.replace(/[^a-z0-9]/gi,'_')}" style="width:160px" placeholder="yeni değer"></td>
        <td>
          ${editable ? `<button class="compact" onclick="setConfig('${escHtml(key)}','cfg-${key.replace(/[^a-z0-9]/gi,'_')}')">Set</button>
          <button class="ghost compact" onclick="resetConfig('${escHtml(key)}')">Reset To Inherited</button>` : badge('READ-ONLY','muted')}
        </td>
      </tr>`;
    }).join('') : '<tr><td colspan="5" class="muted">Konfigürasyon anahtarı yok.</td></tr>';
  } catch(e) { document.getElementById('configRows').innerHTML = `<tr><td colspan="5" class="muted">${escHtml(e.message)}</td></tr>`; }
}
async function setConfig(key, inputId){
  const input = document.getElementById(inputId);
  const value = (input && input.value || '').trim();
  if(!value) return;
  try {
    await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'set', layer:'PROJECT', key, value})});
    await loadSettings();
  } catch(e) { alert(e.message); }
}
async function resetConfig(key){
  try {
    await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({action:'reset', layer:'PROJECT', key})});
    await loadSettings();
  } catch(e) { alert(e.message); }
}
async function checkOnboarding(){
  try {
    const status = await api('/api/router/status');
    const onboard = document.getElementById('onboarding');
    const fresh = !status.providers_configured && !status.router_enabled;
    onboard.style.display = fresh ? '' : 'none';
    if(fresh){
      document.getElementById('onboardTitle').textContent =
        (status.effective_mode || 'IDE_MODEL') + ' modu aktif: anahtar gerekmez';
    }
  } catch(e) {}
}
renderQuality({});
checkOnboarding();
loadProjects().then(() => {
  refreshStatus();
  scheduleAutoBuild();
});
setAutoRefresh(document.getElementById('refreshEvery').value, true);
</script>
</body>
</html>"""


def find_root():
    from paths import find_project_root
    return find_project_root()


def registry_path():
    return Path.home() / ".context-agent" / "dashboard_registry.json"


def load_projects(current_root):
    projects = {str(current_root): {"root": str(current_root), "url": "", "port": 0}}
    path = registry_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("projects", []):
                root = item.get("root")
                if root and (Path(root) / ".context").exists():
                    projects[str(Path(root).resolve())] = {
                        "root": str(Path(root).resolve()),
                        "url": item.get("url", ""),
                        "port": item.get("port", 0),
                    }
        except Exception:
            pass
    return sorted(projects.values(), key=lambda item: item.get("root", "").lower())


def valid_context_root(path):
    try:
        root = Path(path).expanduser().resolve()
    except Exception:
        return None
    if not root.exists() or not root.is_dir():
        return None
    if not (root / ".context").exists():
        return None
    return root


def save_project_registration(root, host, port):
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"projects": []}
    except Exception:
        data = {"projects": []}
    root_str = str(root)
    projects = [item for item in data.get("projects", []) if item.get("root") != root_str]
    projects.append({"root": root_str, "url": f"http://{host}:{port}", "port": port})
    data["projects"] = sorted(projects, key=lambda item: item.get("root", "").lower())
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def add_project_path(current_root, requested, host, port):
    root = valid_context_root(requested)
    if not root:
        return {"error": "context_not_found", "message": "Bu path içinde .context klasörü bulunamadı."}
    save_project_registration(root, host, port)
    return {"current_root": str(root), "projects": load_projects(current_root)}


def resolve_requested_root(current_root, query):
    requested = query.get("root", [""])[0].strip()
    if not requested:
        return current_root
    candidate = valid_context_root(requested)
    if not candidate:
        return current_root
    allowed = {Path(item["root"]).resolve() for item in load_projects(current_root)}
    if candidate in allowed or (candidate / ".context").exists():
        return candidate
    return current_root


def run_script(root, script, *args, timeout=120, env_overrides=None):
    path = root / ".context" / "scripts" / f"{script}.py"
    if not path.exists():
        return {"error": f"script_not_found: {path}"}
    env = os.environ.copy()
    # A central dashboard can switch between registered projects. CWD alone
    # is not enough when the dashboard itself inherited another project's
    # CONTEXT_AGENT_PROJECT_ROOT.
    env["CONTEXT_AGENT_PROJECT_ROOT"] = str(Path(root).resolve())
    if env_overrides:
        env.update({k: str(v) for k, v in env_overrides.items() if v is not None})
    result = subprocess.run(
        [sys.executable, str(path), *[str(a) for a in args if a is not None]],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
    if result.returncode != 0:
        return {"error": "script_failed", "stderr": result.stderr[-2000:], "stdout": result.stdout[-2000:]}
    if not result.stdout.strip():
        return {"ok": True}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw": result.stdout}


_STATUS_CACHE = {}
_STATUS_CACHE_LOCK = threading.Lock()


def cached_status(root, ttl=1.5):
    """Small dashboard snapshot cache; avoids a subprocess per UI repaint."""
    key = str(Path(root).resolve())
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        cached = _STATUS_CACHE.get(key)
        if cached and now - cached[0] <= float(ttl):
            return cached[1]
    value = run_script(root, "agent", "--status")
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[key] = (time.monotonic(), value)
    return value


def dashboard_preview_env():
    return {
        "CONTEXT_AGENT_CLIENT": "dashboard",
        "CONTEXT_AGENT_IDE": "dashboard",
        "CONTEXT_AGENT_SCOPE": "dashboard-preview",
        "CONTEXT_AGENT_RUNTIME_SCOPE": "dashboard-preview",
        "CONTEXT_AGENT_SESSION": "dashboard-preview",
        "CONTEXT_AGENT_MODEL_SESSION_ID": "dashboard-preview",
        "CONTEXT_AGENT_CONVERSATION_ID": "dashboard-preview",
    }


def compact_context_package(data):
    sections = data.get("sections", {})
    routing = sections.get("routing", {})
    items = sections.get("context_items", [])
    context_tokens = sum(item.get("token_estimate", 0) for item in items)
    repo_tokens = sections.get("project_map", {}).get("stats", {}).get("total_tokens", 0)
    return {
        "task": data.get("task"),
        "scope": data.get("scope"),
        "runtime_scope": data.get("runtime_scope"),
        "routing": {
            "confidence": routing.get("confidence", {}),
            "context_plan": routing.get("context_plan", {}),
            "execution_plan": routing.get("execution_plan", {}),
            "machine_readable": routing.get("machine_readable", {}),
        },
        "context_items": items,
        "context_quality": sections.get("context_quality", {}),
        "strict_quality": sections.get("strict_quality", {}),
        "capsule": sections.get("capsule", {}),
        "usage": sections.get("usage", {}),
        "client": data.get("client"),
        "context_tokens": context_tokens,
        "repo_tokens": repo_tokens,
        "budget": data.get("budget", {}),
    }


class Handler(BaseHTTPRequestHandler):
    root = None

    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
            return
        query = parse_qs(parsed.query)
        root = resolve_requested_root(self.root, query)
        if parsed.path == "/api/projects":
            self.send_json({"current_root": str(root), "projects": load_projects(self.root)})
            return
        if parsed.path == "/api/status":
            self.send_json(cached_status(root))
            return
        if parsed.path == "/api/eval":
            self.send_json(run_script(root, "eval", timeout=180))
            return
        if parsed.path == "/api/capsule":
            self.send_json(run_script(root, "capsule", "--current"))
            return
        if parsed.path == "/api/usage":
            self.send_json(run_script(root, "usage", "--summary"))
            return
        if parsed.path == "/api/context":
            self.send_json({"error": "method_not_allowed", "hint": "Use POST /api/context"}, 405)
            return
        if parsed.path == "/api/identity":
            self.send_json(run_script(root, "identity", "--show"))
            return
        if parsed.path == "/api/continuity":
            self.send_json(run_script(root, "identity", "--continuity"))
            return
        if parsed.path == "/api/handoff":
            args = ["--handoff"]
            session = str(query.get("session", [""])[0])
            if session:
                args += ["--session", session]
            self.send_json(run_script(root, "identity", *args))
            return
        if parsed.path == "/api/memory":
            self.send_json(run_script(root, "memory", "--list"))
            return
        if parsed.path == "/api/sessions":
            current = run_script(root, "session", "--show")
            history = run_script(root, "session", "--history")
            self.send_json({"current": current, "history": history})
            return
        if parsed.path == "/api/config":
            self.send_json(run_script(root, "config_store", "--effective"))
            return
        if parsed.path == "/api/providers":
            self.send_json(run_script(root, "router", "providers", "list"))
            return
        if parsed.path == "/api/models":
            self.send_json(run_script(root, "router", "models", "list"))
            return
        if parsed.path == "/api/policy":
            self.send_json(run_script(root, "router", "policy", "show"))
            return
        if parsed.path == "/api/secrets":
            self.send_json(run_script(root, "secret_store", "--list"))
            return
        if parsed.path == "/api/router/status":
            self.send_json(run_script(root, "router", "status"))
            return
        if parsed.path == "/api/router/history":
            limit = str(query.get("limit", ["50"])[0])
            self.send_json(run_script(root, "router", "history", "--limit", limit))
            return
        if parsed.path == "/api/router/usage":
            self.send_json(run_script(root, "router", "usage"))
            return
        if parsed.path == "/api/gateway":
            self.send_json(run_script(root, "model_gateway", "status"))
            return
        self.send_json({"error": "not_found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        root = resolve_requested_root(self.root, query)
        body = self.read_json_body()
        if parsed.path == "/api/projects":
            add_path = str(body.get("add", "")).strip()
            if not add_path:
                self.send_json({"error": "add_path_required"}, 400)
                return
            result = add_project_path(self.root, add_path, self.server.server_address[0], self.server.server_address[1])
            self.send_json(result, 400 if result.get("error") else 200)
            return
        if parsed.path == "/api/index":
            self.send_json(run_script(root, "index", timeout=300))
            return
        if parsed.path == "/api/context":
            task = str(body.get("task", "")).strip()
            budget = str(body.get("budget", "8000")).strip() or "8000"
            fresh = bool(body.get("fresh", True))
            strict_quality = int(body.get("strict_quality", 0) or 0)
            strict_quality = max(0, min(100, strict_quality))
            if not task:
                self.send_json({"error": "task_required"}, 400)
                return
            env = dashboard_preview_env()
            if fresh:
                run_script(root, "dedup", "--clear", env_overrides=env)
                run_script(root, "budget", "--reset", env_overrides=env)
            cmd = [
                task,
                "--budget",
                budget,
                "--skip-usage",
            ]
            if strict_quality > 0:
                cmd += ["--strict-quality", str(strict_quality)]
            data = run_script(
                root,
                "agent",
                *cmd,
                timeout=180,
                env_overrides=env,
            )
            self.send_json(compact_context_package(data))
            return
        if parsed.path == "/api/config":
            action = str(body.get("action", "set"))
            layer = str(body.get("layer", "")).strip().upper()
            key = str(body.get("key", "")).strip()
            if not layer or not key:
                self.send_json({"error": "layer_and_key_required"}, 400)
                return
            if action == "reset":
                self.send_json(run_script(root, "config_store", "--reset", layer, key))
            else:
                value = str(body.get("value", ""))
                self.send_json(run_script(root, "config_store", "--set", layer, key, value))
            return
        if parsed.path == "/api/providers":
            action = str(body.get("action", ""))
            pid = str(body.get("id", ""))
            if not action or (not pid and action != "health"):
                self.send_json({"error": "action_and_id_required"}, 400)
                return
            if action == "health":
                cmd = ["router", "providers", "health"]
                if pid:
                    cmd += ["--id", pid]
                if body.get("force"):
                    cmd.append("--force")
                self.send_json(run_script(root, *cmd, timeout=120))
            else:
                self.send_json(run_script(root, "router", "providers", action, "--id", pid))
            return
        if parsed.path == "/api/models":
            action = str(body.get("action", ""))
            mid = str(body.get("id", ""))
            if action not in ("enable", "disable") or not mid:
                self.send_json({"error": "action_enable_disable_and_id_required"}, 400)
                return
            self.send_json(run_script(root, "router", "models", action, "--id", mid))
            return
        if parsed.path == "/api/secrets":
            action = str(body.get("action", ""))
            pid = str(body.get("provider_id", ""))
            if action not in ("set", "delete", "test") or not pid:
                self.send_json({"error": "action_set_delete_test_and_provider_id_required"}, 400)
                return
            if action == "set":
                secret_value = str(body.get("value", ""))
                if not secret_value:
                    self.send_json({"error": "value_required"}, 400)
                    return
                # Secret travels via env var only; subprocess argv and API
                # responses never contain the raw value (mask only).
                result = run_script(root, "secret_store", "--set", pid,
                                    env_overrides={"SECRET_VALUE": secret_value})
                self.send_json(result)
            elif action == "delete":
                self.send_json(run_script(root, "secret_store", "--delete", pid))
            else:  # test -> live provider health probe; validates the stored key
                result = run_script(root, "router", "providers", "health", "--id", pid, "--force")
                entries = result.get("health") if isinstance(result, dict) else None
                entry = entries[0] if entries else {}
                self.send_json({"provider_id": pid,
                                "status": entry.get("status", "UNKNOWN"),
                                "last_error_category": entry.get("last_error_category"),
                                "checked_at": entry.get("checked_at")})
            return
        if parsed.path == "/api/policy":
            action = str(body.get("action", ""))
            value = str(body.get("value", ""))
            if action == "mode":
                self.send_json(run_script(root, "router", "policy", "mode", value))
            elif action == "custom-set":
                self.send_json(run_script(root, "router", "policy", "custom-set", value))
            else:
                self.send_json({"error": "action_mode_or_custom_set_required"}, 400)
            return
        if parsed.path in ("/api/router/route", "/api/router/delegate"):
            analysis = body.get("analysis") or {}
            task = str(body.get("task", ""))
            tokens = str(int(body.get("context_tokens", 0) or 0))
            subcommand = "route" if parsed.path.endswith("/route") else "delegate"
            cmd = [subcommand, "--analysis-json", json.dumps(analysis),
                   "--task", task, "--context-tokens", tokens]
            if subcommand == "route":
                cmd.append("--no-record")  # dashboard previews never pollute history
            self.send_json(run_script(root, "router", *cmd, timeout=120))
            return
        self.send_json({"error": "not_found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write(fmt % args + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--root", default="",
        help="Project root to serve (avoids ambiguity in multi-root workspaces).")
    args = parser.parse_args()
    if args.root:
        root = Path(args.root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            parser.error(f"project root does not exist: {root}")
        os.environ["CONTEXT_AGENT_PROJECT_ROOT"] = str(root)
    else:
        root = find_root()
    Handler.root = root
    save_project_registration(root, args.host, args.port)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Context Agent dashboard: http://{args.host}:{args.port}  root={root}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
