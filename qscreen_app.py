#!/usr/bin/env python3
"""
qscreen_app.py — local browser app for the QSE filing ingestor.

Run it on your laptop, open the page, drag in a PDF, fill four fields, click
Extract. It runs the SAME engine as qscreen_ingest.py (imported, not
re-implemented) and gives you a downloadable JSON report to upload to
qscreen.app. Nothing is auto-uploaded — you stay in control.

    pip install flask pdfplumber requests
    python3 qscreen_app.py
    # then open http://127.0.0.1:8765 in your browser

The OpenRouter key is read from the tool's .env (same as the CLI) or the
OPENROUTER_API_KEY env var. No agent, no command line per filing. Upload is
opt-in: a button appears only when the server has INGEST_TOKEN set, and even
then nothing leaves your machine until you click it.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import traceback
from types import SimpleNamespace
from pathlib import Path

# Reuse the exact, tested engine — do NOT reimplement any of it here.
import qscreen_ingest as engine
import qscreen_analyze
import qscreen_dcf
import qscreen_report
import qscreen_portfolio
import qscreen_workbook
import qscreen_statements
import qscreen_periods

try:
    from flask import Flask, request, Response, send_file
except ImportError:
    sys.exit("Flask not installed. Run:  pip install flask pdfplumber requests")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB upload cap


def _safe_filename(s, fallback: str = "filing") -> str:
    """A download filename safe to drop into a Content-Disposition header — no
    quotes, path separators, or control chars (which a filing's symbol could carry)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(s or "")).strip("._")
    return cleaned[:64] or fallback

# ── QSE taxonomy + per-stock knowledge ───────────────────────────────────────
# The sector → sub-sector tree and the symbol map now live in the qatar/ package
# (the single source of truth, with per-stock temporal profiles). Each sub-sector
# still maps to one of the engine's 5 EXTRACTION archetypes, which drive the LLM's
# parsing hint (conventional_bank / islamic_bank / insurance / industrial / other).
import qatar

QSE_TAXONOMY = qatar.QSE_TAXONOMY
SUBSECTOR_TO_EXTRACTION = qatar.SUBSECTOR_TO_EXTRACTION
SYMBOL_SUBSECTOR = qatar.SYMBOL_SUBSECTOR


def _subsector_options_html() -> str:
    out = []
    for group, subs in QSE_TAXONOMY.items():
        out.append(f'<optgroup label="{group}">')
        for sub, _cat in subs:
            out.append(f'<option value="{sub}">{sub}</option>')
        out.append("</optgroup>")
    return "\n".join(out)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>QScreen Filing Ingestor</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-size: 22px; } .sub { color: #666; margin-top: -8px; }
  label { display: block; margin: 14px 0 4px; font-weight: 600; }
  input, select { width: 100%; padding: 9px; border: 1px solid #ccc; border-radius: 6px; font-size: 15px; box-sizing: border-box; }
  .row { display: flex; gap: 12px; } .row > div { flex: 1; }
  button { margin-top: 20px; padding: 12px 20px; font-size: 16px; font-weight: 600; background: #0b6; color: #fff; border: 0; border-radius: 8px; cursor: pointer; }
  button:disabled { background: #999; cursor: wait; }
  #out { white-space: pre-wrap; background: #f6f6f6; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px; margin-top: 20px; display: none; }
  .ok { color: #0a7; } .warn { color: #c80; } .err { color: #c33; }
  .hint { color: #888; font-size: 13px; margin: 6px 0 0; min-height: 16px; }
  a.dl { display: inline-block; margin-top: 14px; margin-right: 8px; padding: 10px 16px; background: #06c; color: #fff; border-radius: 8px; text-decoration: none; font-weight: 600; }
  a.up { background: #0b6; } a.up.busy { background: #999; pointer-events: none; }
  .muted { color: #888; font-weight: 400; font-size: 12px; }
  details.adv { margin-top: 14px; } summary { cursor: pointer; color: #06c; font-weight: 600; }
  .keyhint { background: #eef6ff; border: 1px solid #cfe3ff; border-radius: 8px; padding: 10px 12px; margin-top: 10px; font-size: 13px; line-height: 1.5; }
  .keyhint a { color: #06c; font-weight: 700; } .keyhint code { background: #dceaff; padding: 1px 5px; border-radius: 4px; }
  label.guided { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: 13px; font-weight: 600; }
  label.guided input { width: auto; }
  .seg { margin-top: 18px; } .seg h3 { font-size: 16px; margin: 8px 0; } .seg h4 { font-size: 13px; color: #555; text-transform: capitalize; margin: 12px 0 4px; }
  table.seg { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.seg th, table.seg td { border-bottom: 1px solid #eee; padding: 5px 8px; text-align: right; }
  table.seg th:first-child, table.seg td:first-child { text-align: left; }
  table.seg th { color: #888; font-weight: 600; }
  .fx { background: #fde8c8; color: #a05a00; border-radius: 4px; padding: 0 5px; font-size: 11px; font-weight: 700; }
  .ev { color: #06c; cursor: help; }
  .neg { color: #c33; } .pos { color: #0a7; }
  .rep { color: #0a7; cursor: help; font-size: 11px; } ul.flags { margin: 6px 0; padding-left: 0; list-style: none; }
  ul.flags li { padding: 4px 0; font-size: 13px; } ul.flags li.alert { color: #c33; font-weight: 600; } ul.flags li.warn2 { color: #b06b00; }
  .dcf label { display: inline-block; font-weight: 600; font-size: 12px; margin: 6px 8px 2px 0; }
  .dcf input { width: 78px; padding: 5px; font-size: 13px; }
  .dcf button { margin: 8px 0; padding: 8px 14px; font-size: 14px; }
  .dcfval { font-size: 18px; font-weight: 700; } .grid td.base { background: #fff3cd; font-weight: 700; }
  details.cmp { margin-top: 22px; border-top: 1px solid #eee; padding-top: 12px; } details.cmp summary { cursor: pointer; font-weight: 600; }
  table.cmp { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
  table.cmp th, table.cmp td { border-bottom: 1px solid #eee; padding: 5px 8px; text-align: right; }
  table.cmp th:first-child, table.cmp td:first-child { text-align: left; }
  table.cmp tr.target { background: #eef6ff; font-weight: 600; } table.cmp .r1 { color: #0a7; font-weight: 700; }
  table.cmp sup { color: #999; font-weight: 400; }
  label.inc { font-size: 12px; color: #555; margin-left: 10px; } label.inc input { vertical-align: middle; }
  .outputs { margin: 14px 0 4px; padding-top: 10px; border-top: 1px solid #eee; }
  .olabel { display: block; font-weight: 600; color: #555; font-size: 13px; margin-bottom: 6px; }
</style></head><body>
<h1>QScreen Filing Ingestor</h1>
<p class="sub">Drop a QSE financial-report PDF, fill the fields, click Extract. Then download the report and upload it to qscreen.app. Type a known symbol and the sub-sector auto-fills.</p>
<form id="f">
  <label>Filing PDF</label>
  <input type="file" name="pdf" accept="application/pdf" required>
  <div class="row">
    <div><label>Symbol</label><input name="symbol" id="symbol" placeholder="QIBK" autocomplete="off" required></div>
    <div><label>QSE Sector / Sub-sector</label>
      <select name="subsector" id="subsector" required>
        __SUBSECTOR_OPTIONS__
      </select>
    </div>
  </div>
  <p class="hint" id="hint"></p>
  <div class="row">
    <div><label>Year</label><input name="year" type="number" placeholder="2024" required></div>
    <div><label>Period</label>
      <select name="period">
        <option>FY</option><option>Q1</option><option>Q2</option><option>Q3</option>
        <option>Q4</option><option>H1</option><option>9M</option>
      </select>
    </div>
  </div>
  <details class="adv" open><summary>Provider / model — cloud key OR a local model on your laptop</summary>
    <div class="row">
      <div><label>AI Provider</label>
        <select name="provider" id="provider">
          <option value="">auto (use whichever key is set)</option>
          <optgroup label="Cloud (needs an API key)">
            <option value="minimax">MiniMax</option>
            <option value="openrouter">OpenRouter</option>
            <option value="kimi">Kimi (Moonshot)</option>
            <option value="openai">OpenAI</option>
            <option value="anthropic">Claude (Anthropic)</option>
          </optgroup>
          <optgroup label="Local — on your laptop, no API key">
            <option value="ollama">Ollama (local)</option>
            <option value="lmstudio">LM Studio (local)</option>
            <option value="llamacpp">llama.cpp (local)</option>
            <option value="jan">Jan (local)</option>
            <option value="gpt4all">GPT4All (local)</option>
            <option value="mlx">MLX — Apple (local)</option>
          </optgroup>
        </select>
      </div>
      <div><label>Model <span class="muted">(blank = provider default)</span></label>
        <input name="model" id="model" placeholder="default" autocomplete="off"></div>
    </div>
    <p class="keyhint" id="provkey"></p>
    <div class="row">
      <div><label>Mode</label>
        <select name="mode" id="mode">
          <option value="auto">Auto (Basic for local, Pro for cloud)</option>
          <option value="basic">Basic — deterministic, great for tiny / local models</option>
          <option value="pro">Pro — model extracts everything (use a strong model)</option>
        </select>
      </div>
    </div>
    <label class="guided"><input type="checkbox" name="no_llm" id="no_llm" value="1">
      Run fully offline — read numbers from the PDF tables with <b>no model at all</b>
      <span class="muted">(Basic; needs no key)</span></label>
    <p class="keyhint" id="modehint"></p>
  </details>
  <button type="submit" id="go">Extract</button>
</form>
<div id="out"></div>

<details class="cmp"><summary>Compare / screen extracted filings</summary>
  <p class="muted">Select already-extracted <code>*_filing.json</code> files.
  <b>Compare</b> ranks them as peers (on the first file's company type);
  <b>Dashboard</b> screens the whole basket; <b>Excel workbook</b> combines several
  years of one company into a single multi-year transcript; <b>TTM</b> rolls interim
  (YTD) filings into a trailing-twelve-month view.</p>
  <input type="file" id="cmpfiles" accept="application/json,.json" multiple>
  <button id="cmpgo" type="button">Compare</button>
  <button id="dashgo" type="button">Dashboard</button>
  <button id="wbgo" type="button">Excel workbook</button>
  <button id="ttmgo" type="button">TTM</button>
  <div id="cmpout"></div>
</details>
<script>
const SYMBOL_SUBSECTOR = __SYMBOL_MAP_JSON__;
const UPLOAD_ENABLED = __UPLOAD_ENABLED__;
const PROVIDER_INFO = __PROVIDER_INFO_JSON__;
const f = document.getElementById('f'), out = document.getElementById('out'), go = document.getElementById('go');
const provEl = document.getElementById('provider'), modelEl = document.getElementById('model'),
      provKey = document.getElementById('provkey'), modeEl = document.getElementById('mode'),
      noLlmEl = document.getElementById('no_llm'), modeHint = document.getElementById('modehint');
function updateProvider() {
  const info = PROVIDER_INFO[provEl.value];
  if (info && info.local) {
    modelEl.placeholder = info.model || 'default';
    provKey.innerHTML = '💻 <b>' + info.label + '</b> runs on your laptop — <b>no API key needed</b>. ' +
      (info.setup ? '<code>' + esc(info.setup) + '</code>. ' : '') +
      '<a href="' + info.url + '" target="_blank" rel="noopener">Download / docs &#8599;</a>. ' +
      'Make sure it is running, then click Extract.';
    if (modeEl && modeEl.value === 'auto') modeEl.value = 'basic';   // tiny models → Basic
  } else if (info) {
    modelEl.placeholder = info.model || 'default';
    provKey.innerHTML = '🔑 Need a key for <b>' + info.label + '</b>? ' +
      '<a href="' + info.url + '" target="_blank" rel="noopener">Click here to get one &#8599;</a>' +
      ', then add <code>' + info.env + '=your-key</code> to the <code>.env</code> file next to the app and restart it.';
  } else {
    modelEl.placeholder = 'default';
    provKey.innerHTML = '🔑 Use a cloud key (one <code>*_API_KEY</code> in <code>.env</code>) ' +
      'or pick a <b>local</b> model above to run fully offline with no key.';
  }
  updateMode();
}
function updateMode() {
  if (!modeHint) return;
  if (noLlmEl && noLlmEl.checked) {
    modeHint.innerHTML = '⚙️ <b>Fully offline.</b> Line items are read straight from the PDF\'s tables — ' +
      'no model is called. Audit/notes are skipped. Works with no key and no model running.';
    return;
  }
  const m = modeEl ? modeEl.value : 'auto';
  if (m === 'pro') {
    modeHint.innerHTML = '🧠 <b>Pro.</b> The model extracts everything (richer notes & segments). ' +
      'Use a strong model — GPT‑4.5+/Claude Sonnet 4+/MiniMax‑M2.';
  } else if (m === 'basic') {
    modeHint.innerHTML = '🧭 <b>Basic.</b> Numbers are read from the PDF\'s tables in code; the model only ' +
      'fills gaps and classifies the audit opinion. Great for a tiny / local model (e.g. Gemma 3 270M via MLX).';
  } else {
    modeHint.innerHTML = '🧭 <b>Auto.</b> Basic for local models, Pro for cloud models.';
  }
}
if (provEl) { provEl.addEventListener('change', updateProvider); }
if (modeEl) { modeEl.addEventListener('change', updateMode); }
if (noLlmEl) { noLlmEl.addEventListener('change', updateMode); }
updateProvider();

function fmtNum(x){ return (x==null)?'—':Number(x).toLocaleString(); }
function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmtPct(x){ if(x==null) return '<span>—</span>'; const c=x<0?'neg':'pos'; return '<span class="'+c+'">'+(x*100).toFixed(0)+'%</span>'; }
function renderSegments(sa){
  if(!sa || !sa.dimensions || !Object.keys(sa.dimensions).length) return '';
  let h = '<div class="seg"><h3>Segment breakdown ('+esc(sa.reporting_currency||'')+')</h3>';
  for(const dim of Object.keys(sa.dimensions)){
    const d = sa.dimensions[dim];
    h += '<h4>by '+esc(dim.replace('_',' '))+'</h4><table class="seg"><tr><th>Segment</th>'
       + '<th>Revenue</th><th>YoY</th><th>Share</th><th>Net profit</th><th>YoY</th></tr>';
    for(const r of d.segments){
      const m=r.metrics||{}, y=r.yoy||{}, s=r.share||{};
      const fx = r.fx_exposed ? ' <span class="fx" title="'+esc(r.fx_note||'')+'">FX '+esc(r.currency||'')+'</span>' : '';
      const ev = (r.events&&r.events.length) ? ' <span class="ev" title="'+esc(r.events.join(' · '))+'">ⓘ</span>' : '';
      h += '<tr><td>'+esc(r.name)+fx+ev+'</td><td>'+fmtNum(m.revenue)+'</td><td>'+fmtPct(y.revenue)
         + '</td><td>'+fmtPct(s.revenue)+'</td><td>'+fmtNum(m.net_profit)+'</td><td>'+fmtPct(y.net_profit)+'</td></tr>';
    }
    h += '</table>';
  }
  return h + '</div>';
}
function fmtCmp(name, v){
  if(v==null) return '—';
  if(name==='liabilities_to_equity') return Number(v).toFixed(2)+'×';
  const pctSet = ['roe','roa','nim','cost_income','npl','car','ldr','net_margin','operating_margin','loss_ratio','combined_ratio'];
  if(pctSet.indexOf(name)>=0) return (v*100).toFixed(1)+'%';   // values are fractions
  return Number(v).toLocaleString();
}
function renderCompare(d){
  if(!d || !d.rows || !d.rows.length) return '<span class="warn">'+esc((d&&d.error)||'nothing to compare')+'</span>';
  const metrics = d.metrics.map(m=>m.name);
  let h = '<table class="cmp"><tr><th>Company</th>';
  for(const m of metrics) h += '<th>'+esc(m.replace(/_/g,' '))+'</th>';
  h += '</tr>';
  for(const r of d.rows){
    h += '<tr class="'+(r.is_target?'target':'')+'"><td title="'+esc(r.symbol)+'">'+esc(r.symbol)+(r.is_target?' ★':'')+'</td>';
    for(const m of metrics){ const rk=r.ranks[m];
      h += '<td class="'+(rk===1?'r1':'')+'">'+fmtCmp(m, r.ratios[m])+(rk?'<sup>#'+rk+'</sup>':'')+'</td>'; }
    h += '</tr>';
  }
  return h + '</table><p class="muted">★ = target · #n = rank among peers · green = best</p>';
}
async function runCompare(){
  const inp = document.getElementById('cmpfiles'), out = document.getElementById('cmpout');
  if(!inp.files || inp.files.length < 2){ out.innerHTML='<span class="warn">Pick at least two filing JSON files.</span>'; return; }
  out.textContent = 'Comparing…';
  try {
    const filings = await Promise.all([...inp.files].map(f => f.text().then(t => JSON.parse(t))));
    const r = await fetch('/compare', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filings})});
    out.innerHTML = renderCompare(await r.json());
  } catch(e){ out.innerHTML = '<span class="err">'+esc(e)+'</span>'; }
}
async function runDashboard(){
  const inp = document.getElementById('cmpfiles'), out = document.getElementById('cmpout');
  if(!inp.files || !inp.files.length){ out.innerHTML='<span class="warn">Pick filing JSON files.</span>'; return; }
  out.textContent = 'Screening…';
  try {
    const filings = await Promise.all([...inp.files].map(f => f.text().then(t => JSON.parse(t))));
    const r = await fetch('/portfolio', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filings})});
    const d = await r.json(); if(!r.ok) throw new Error(d.error||'failed');
    const url = URL.createObjectURL(new Blob([d.html], {type:'text/html'}));
    const a = document.createElement('a'); a.href = url; a.download = 'watchlist.html'; a.click(); URL.revokeObjectURL(url);
    out.innerHTML = '<span class="muted">Downloaded watchlist.html — screened '+d.count+' stock(s).</span>';
  } catch(e){ out.innerHTML = '<span class="err">'+esc(e)+'</span>'; }
}
async function runTtm(){
  const inp = document.getElementById('cmpfiles'), out = document.getElementById('cmpout');
  if(!inp.files || !inp.files.length){ out.innerHTML='<span class="warn">Pick filing JSON files (one company, annual and/or interim).</span>'; return; }
  out.textContent = 'Rolling up…';
  try {
    const filings = await Promise.all([...inp.files].map(f => f.text().then(t => JSON.parse(t))));
    const r = await fetch('/ttm', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filings})});
    const d = await r.json(); if(!r.ok) throw new Error(d.error||'failed');
    function tbl(obj){ const rows = Object.entries(obj||{}).sort(); if(!rows.length) return '<p class="muted">—</p>';
      return '<table><tr><th>Flow metric</th><th>Value</th></tr>' + rows.map(([c,v]) => '<tr><td>'+esc(c)+'</td><td>'+fmtNum(v)+'</td></tr>').join('') + '</table>'; }
    let h = '<div class="seg"><h3>TTM — as of '+esc(d.as_of||'?')+'</h3><p class="muted">'+esc(d.basis||'')+'</p>'+tbl(d.flows);
    if(d.standalone_quarter) h += '<h3>'+esc(d.standalone_quarter.label)+'</h3>'+tbl(d.standalone_quarter.flows);
    if((d.warnings||[]).length) h += '<p class="warn">'+d.warnings.map(esc).join('<br>')+'</p>';
    h += '<p class="muted">Periods: '+(d.periods||[]).map(esc).join(', ')+'</p></div>';
    out.innerHTML = h;
  } catch(e){ out.innerHTML = '<span class="err">'+esc(e)+'</span>'; }
}
async function runWorkbook(){
  const inp = document.getElementById('cmpfiles'), out = document.getElementById('cmpout');
  if(!inp.files || !inp.files.length){ out.innerHTML='<span class="warn">Pick filing JSON files (same company, multiple years).</span>'; return; }
  out.textContent = 'Building workbook…';
  try {
    const filings = await Promise.all([...inp.files].map(f => f.text().then(t => JSON.parse(t))));
    const r = await fetch('/workbook', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filings})});
    if(!r.ok){ const d = await r.json().catch(()=>({})); throw new Error(d.error||'failed'); }
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement('a'); a.href = url; a.download = 'transcript.xlsx'; a.click(); URL.revokeObjectURL(url);
    out.innerHTML = '<span class="muted">Downloaded transcript.xlsx — '+filings.length+' filing(s).</span>';
  } catch(e){ out.innerHTML = '<span class="err">'+esc(e)+'</span>'; }
}
function renderDcfPanel(){
  return '<div class="seg dcf"><h3>Valuation (DCF) — adjustable</h3>'
    + '<div><label>Discount rate %</label><input id="d_r" type="number" step="0.5" value="10">'
    + '<label>Growth %</label><input id="d_g" type="number" step="0.5" placeholder="auto">'
    + '<label>Terminal %</label><input id="d_tg" type="number" step="0.25" value="2.5">'
    + '<label>Years</label><input id="d_yr" type="number" value="5">'
    + '<label>Shares</label><input id="d_sh" type="number" placeholder="optional">'
    + '<label>Price</label><input id="d_px" type="number" step="0.01" placeholder="optional"></div>'
    + '<button id="dcfgo">Run valuation</button><div id="dcfout"></div></div>';
}
function runDcf(){
  const num = (id)=>{ const v=document.getElementById(id).value; return v===''?null:Number(v); };
  const a = { discount_rate:(num('d_r')||10)/100, terminal_growth:(num('d_tg')||2.5)/100, years:num('d_yr')||5 };
  const g = num('d_g'); if(g!=null) a.growth = g/100;
  const body = { filing:lastFiling, symbol:lastSymbol, assumptions:a, price:num('d_px'), shares:num('d_sh') };
  const out = document.getElementById('dcfout'); out.textContent='Computing…';
  fetch('/dcf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(r=>r.json()).then(d=>{ out.innerHTML = renderDcfResult(d); })
    .catch(e=>{ out.innerHTML='<span class="err">'+esc(e)+'</span>'; });
}
function renderDcfResult(d){
  if(!d || !d.valuation){ return '<span class="warn">'+esc((d&&d.warnings&&d.warnings.join('; '))||'no valuation')+'</span>'; }
  const v=d.valuation, ccy=d.reporting_currency||'';
  const headline = (v.per_share!=null) ? (ccy+' '+v.per_share.toFixed(2)+' / share')
                                       : (ccy+' '+fmtNum(Math.round(v.equity_value))+' equity value');
  let h = '<p class="dcfval">'+headline+'</p>';
  h += '<p class="muted">model: '+esc(v.model)+' · terminal '+(v.terminal_pct*100).toFixed(0)+'% of value'
     + ((d.upside!=null) ? ' · upside <span class="'+(d.upside<0?'neg':'pos')+'">'+(d.upside*100).toFixed(0)+'%</span> vs '+d.price : '')+'</p>';
  const s=d.sensitivity;
  if(s){
    const bg=v.assumptions.growth, br=v.assumptions.discount_rate;
    h += '<h4>Sensitivity ('+((v.per_share!=null)?'per share':'equity')+') — growth → / discount ↓</h4><table class="seg grid"><tr><th></th>';
    for(const g of s.growth_values) h+='<th>'+(g*100).toFixed(1)+'%</th>';
    h+='</tr>';
    for(let i=0;i<s.rate_values.length;i++){ h+='<tr><th>'+(s.rate_values[i]*100).toFixed(1)+'%</th>';
      for(let j=0;j<s.growth_values.length;j++){ const cell=s.grid[i][j];
        const base = Math.abs(s.rate_values[i]-br)<1e-9 && Math.abs(s.growth_values[j]-bg)<1e-9;
        h+='<td class="'+(base?'base':'')+'">'+((cell==null)?'—':((v.per_share!=null)?Number(cell).toFixed(2):fmtNum(Math.round(cell))))+'</td>'; }
      h+='</tr>'; }
    h+='</table>';
  }
  return h;
}
const PCT_RATIOS = ['roe','roa','nim','cost_income','npl','car','coverage','ldr','net_margin','operating_margin','loss_ratio','expense_ratio','combined_ratio','dividend_payout'];
function fmtRatio(name, r){
  if(!r || r.value==null) return '—';
  const v=r.value, rep=(r.basis==='reported')?' <span class="rep" title="as reported by the company">®</span>':'';
  if(name==='fcf') return fmtNum(v)+rep;
  if(name==='liabilities_to_equity') return v.toFixed(2)+'×'+rep;
  return (v*100).toFixed(1)+'%'+rep;   // all ratio values are fractions
}
function renderAnalysis(an){
  if(!an || !an.ratios) return '';
  const yrs = Object.keys(an.ratios); if(!yrs.length) return '';
  const y = yrs[yrs.length-1], R = an.ratios[y];
  let h='<div class="seg"><h3>Key ratios — '+y+' ('+(an.archetype||'').replace(/_/g,' ')+') <span class="rep">® = as reported</span></h3>';
  h+='<table class="seg"><tr><th>Ratio</th><th>Value</th></tr>';
  for(const k of Object.keys(R)) h+='<tr><td>'+esc(k.replace(/_/g,' '))+'</td><td>'+fmtRatio(k,R[k])+'</td></tr>';
  h+='</table>';
  if(an.red_flags && an.red_flags.length){
    h+='<h4>Red flags</h4><ul class="flags">';
    for(const f of an.red_flags){ const cls=(f.severity==='alert')?'alert':'warn2';
      h+='<li class="'+cls+'">'+((f.severity==='alert')?'🚨':'⚠️')+' '+esc(f.message)+'</li>'; }
    h+='</ul>';
  }
  return h+'</div>';
}
const symbolEl = document.getElementById('symbol'), subEl = document.getElementById('subsector'), hintEl = document.getElementById('hint');
symbolEl.addEventListener('input', () => {
  const sym = symbolEl.value.trim().toUpperCase().replace(/\\.QA$/, '');
  const sub = SYMBOL_SUBSECTOR[sym];
  if (sub) {
    subEl.value = sub;
    hintEl.textContent = sym + ' → ' + sub + ' (auto-filled; change if wrong)';
  } else {
    hintEl.textContent = sym ? (sym + ' not in the known list — pick the sub-sector manually') : '';
  }
});
let lastBlob = null, lastName = 'filing.json', lastFiling = null, lastSymbol = '', lastAnalysis = null;
f.onsubmit = async (e) => {
  e.preventDefault();
  go.disabled = true; go.textContent = 'Extracting… (this can take a few minutes)';
  out.style.display = 'block'; out.textContent = 'Reading PDF and calling the model…';
  try {
    const res = await fetch('/extract', { method: 'POST', body: new FormData(f) });
    const data = await res.json();
    if (!res.ok) { out.innerHTML = '<span class="err">Error: ' + esc(data.error||'unknown') + '</span>\\n\\n' + esc(data.detail||''); }
    else {
      let html = '<span class="' + (data.problems.length ? 'warn' : 'ok') + '">' + esc(data.summary) + '</span>';
      if (data.problems.length) html += '\\n\\nNotes:\\n - ' + data.problems.map(esc).join('\\n - ');
      lastBlob = new Blob([JSON.stringify(data.filing, null, 2)], {type:'application/json'});
      lastName = data.filename; lastFiling = data.filing;
      lastSymbol = (data.filing && data.filing.metadata && data.filing.metadata.symbol) || '';
      lastAnalysis = data.analysis || null;
      html += '\\n\\n<div class="outputs"><span class="olabel">Outputs — pick what you need:</span>';
      html += '<a class="dl" id="dl" href="#">⬇ qscreen JSON</a>';
      html += '<a class="dl" id="xlsx" href="#">⬇ Excel transcript</a>';
      html += '<a class="dl" id="csv" href="#">⬇ CSV</a>';
      html += '<a class="dl" id="stmt" href="#">📄 Statements (HTML)</a>';
      html += '<a class="dl" id="rep" href="#">📰 Analyst report</a>';
      if (UPLOAD_ENABLED && !data.problems.length)
        html += '<a class="dl up" id="up" href="#">⬆ Upload to qscreen.app</a>'
             + '<label class="inc"><input type="checkbox" id="incan"> include analysis in upload</label>';
      html += '</div>';
      html += renderSegments((data.analysis||{}).segments);
      html += renderAnalysis(data.analysis);
      html += renderDcfPanel();
      out.innerHTML = html;
      document.getElementById('dl').onclick = (ev) => {
        ev.preventDefault();
        const url = URL.createObjectURL(lastBlob);
        const a = document.createElement('a'); a.href = url; a.download = lastName; a.click();
        URL.revokeObjectURL(url);
      };
      async function dlPost(path, fname){
        const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify({ filing: lastFiling })});
        if(!r.ok){ const d = await r.json().catch(()=>({})); throw new Error(d.error || ('HTTP '+r.status)); }
        const url = URL.createObjectURL(await r.blob());
        const a = document.createElement('a'); a.href = url; a.download = fname; a.click(); URL.revokeObjectURL(url);
      }
      const xl = document.getElementById('xlsx');
      if (xl) xl.onclick = async (ev) => { ev.preventDefault(); const t = xl.textContent; xl.textContent = '⬇ Building…';
        try { await dlPost('/workbook', (lastSymbol||'filing') + '_transcript.xlsx'); xl.textContent = t; }
        catch(e){ xl.textContent = '⬇ Excel failed'; } };
      const cv = document.getElementById('csv');
      if (cv) cv.onclick = async (ev) => { ev.preventDefault();
        try { await dlPost('/export.csv', (lastSymbol||'filing') + '_line_items.csv'); }
        catch(e){ cv.textContent = '⬇ CSV failed'; } };
      const dg = document.getElementById('dcfgo');
      if (dg) dg.onclick = (ev) => { ev.preventDefault(); runDcf(); };
      const st = document.getElementById('stmt');
      if (st) st.onclick = async (ev) => {
        ev.preventDefault(); const label = st.textContent; st.textContent = '📄 Building…';
        try {
          const r = await fetch('/statements', { method: 'POST', headers: {'Content-Type':'application/json'},
                                                 body: JSON.stringify({ filing: lastFiling }) });
          const d = await r.json(); if (!r.ok) throw new Error(d.error || 'failed');
          const url = URL.createObjectURL(new Blob([d.html], {type:'text/html'}));
          const a = document.createElement('a'); a.href = url; a.download = (lastSymbol||'filing') + '_statements.html'; a.click();
          URL.revokeObjectURL(url); st.textContent = label;
        } catch (e) { st.textContent = '📄 Statements failed'; }
      };
      const rp = document.getElementById('rep');
      if (rp) rp.onclick = async (ev) => {
        ev.preventDefault(); const label = rp.textContent; rp.textContent = '📰 Building…';
        try {
          const r = await fetch('/report', { method: 'POST', headers: {'Content-Type':'application/json'},
                                             body: JSON.stringify({ filing: lastFiling, symbol: lastSymbol }) });
          const d = await r.json(); if (!r.ok) throw new Error(d.error || 'failed');
          const url = URL.createObjectURL(new Blob([d.html], {type:'text/html'}));
          const a = document.createElement('a'); a.href = url; a.download = (lastSymbol||'report') + '_report.html'; a.click();
          URL.revokeObjectURL(url); rp.textContent = label;
        } catch (e) { rp.textContent = '📰 Report failed'; }
      };
      const up = document.getElementById('up');
      if (up) up.onclick = async (ev) => {
        ev.preventDefault();
        up.classList.add('busy'); up.textContent = '⬆ Uploading…';
        const note = document.createElement('div');
        try {
          const inc = document.getElementById('incan');
          const r = await fetch('/upload', { method: 'POST', headers: {'Content-Type':'application/json'},
                                             body: JSON.stringify({ filing: lastFiling,
                                               with_analysis: !!(inc && inc.checked), analysis: lastAnalysis }) });
          const d = await r.json();
          if (r.ok) { up.textContent = '✅ Uploaded to qscreen.app'; }
          else {
            up.classList.remove('busy'); up.textContent = '⬆ Retry upload';
            note.className = 'err';
            note.textContent = 'Upload failed: ' + (d.error || 'unknown') +
              (d.problems ? '\\n - ' + d.problems.join('\\n - ') : '');
            out.appendChild(note);
          }
        } catch (err) {
          up.classList.remove('busy'); up.textContent = '⬆ Retry upload';
          note.className = 'err'; note.textContent = 'Upload failed: ' + err; out.appendChild(note);
        }
      };
    }
  } catch (err) { out.innerHTML = '<span class="err">Request failed: ' + esc(err) + '</span>'; }
  go.disabled = false; go.textContent = 'Extract';
};
const cmpBtn = document.getElementById('cmpgo');
if (cmpBtn) cmpBtn.onclick = runCompare;
const dashBtn = document.getElementById('dashgo');
if (dashBtn) dashBtn.onclick = runDashboard;
const wbBtn = document.getElementById('wbgo');
if (wbBtn) wbBtn.onclick = runWorkbook;
const ttmBtn = document.getElementById('ttmgo');
if (ttmBtn) ttmBtn.onclick = runTtm;
</script>
</body></html>"""


@app.route("/")
def index():
    upload_enabled = bool(os.getenv("INGEST_TOKEN"))
    provider_info = {name: {"label": cfg["label"], "model": cfg["default_model"],
                            "url": cfg["key_url"], "env": cfg["env"][0],
                            "local": bool(cfg.get("local")), "setup": cfg.get("setup", "")}
                     for name, cfg in engine.PROVIDERS.items()}
    html = (PAGE
            .replace("__SUBSECTOR_OPTIONS__", _subsector_options_html())
            .replace("__SYMBOL_MAP_JSON__", json.dumps(SYMBOL_SUBSECTOR))
            .replace("__PROVIDER_INFO_JSON__", json.dumps(provider_info))
            .replace("__UPLOAD_ENABLED__", "true" if upload_enabled else "false"))
    return Response(html, mimetype="text/html")


@app.route("/extract", methods=["POST"])
def extract():
    try:
        up = request.files.get("pdf")
        if not up:
            return {"error": "no PDF uploaded"}, 400
        symbol = (request.form.get("symbol") or "").strip().upper()
        subsector = (request.form.get("subsector") or "").strip()
        year = request.form.get("year")
        period = (request.form.get("period") or "FY").strip()
        if not (symbol and subsector and year):
            return {"error": "symbol, sub-sector and year are required"}, 400
        try:
            year = int(year)
        except (TypeError, ValueError):
            return {"error": "year must be an integer"}, 400
        # The rich QSE sub-sector is stored; the extraction category (1 of 5)
        # drives how the LLM reads the statements.
        sector = SUBSECTOR_TO_EXTRACTION.get(subsector, "other")
        provider = (request.form.get("provider") or "").strip() or None  # None → auto-detect
        model = (request.form.get("model") or "").strip() or None
        mode = (request.form.get("mode") or "auto").strip()   # auto | basic | pro
        no_llm = bool(request.form.get("no_llm"))             # fully-offline checkbox

        # Build the same args object the CLI uses; resolve_provider picks the
        # base URL / model / key (from the matching env var) and validates them.
        args = SimpleNamespace(
            symbol=symbol, sector=sector, year=int(year), period=period,
            provider=provider, base_url=None, model=model,
            max_tokens=16384, timeout=600, retries=4,
            pages_per_chunk=12, overlap=1, no_chunk=False,
            no_json_mode=False, llm_key=None,
            mode=mode, basic=False, pro=False, no_llm=no_llm,
            guided=False, no_guided=False, guided_notes=False,
        )
        engine.apply_mode(args)               # --mode/--no-llm → guided flags
        # Fully-offline (--no-llm) needs no provider at all; otherwise resolve it.
        try:
            cfg = engine.resolve_provider(args)   # raises SystemExit (caught below) if no provider/key
        except SystemExit:
            if no_llm:
                cfg = engine.deterministic_cfg()
            else:
                raise
        args.guided = engine.resolve_guided(args, cfg)   # Basic vs Pro
        if no_llm:
            args.guided = True
        if args.guided:
            args.pages_per_chunk = engine.GUIDED_DEFAULT_PAGES
        args._profile = qatar.profile_for_year(symbol, int(year))  # company+year-aware prompting

        # Save the upload to a private temp file (not a predictable CWD path).
        fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="qscreen_upload_")
        os.close(fd)
        up.save(tmp_path)
        try:
            pages, sha = engine.pdf_to_pages(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        filing = engine.extract_filing(pages, args)
        filing.setdefault("metadata", {}).update({
            "symbol": symbol, "sector": sector, "sub_sector": subsector,
            "fiscal_year": int(year),
            "fiscal_period": period, "source_file": Path(up.filename or "").name, "source_sha256": sha,
            "extracted_at": engine.datetime.now(engine.timezone.utc).isoformat(),
            "extractor": {"provider": cfg["name"], "model": cfg["model"]},
        })
        problems = engine.validate_filing(filing)
        try:                                       # analysis must never sink a good extraction
            analysis = qscreen_analyze.analyze(symbol, [filing], args._profile)
        except Exception as ex:
            analysis = {"warnings": [f"analysis failed: {ex}"], "ratios": {}, "trends": {},
                        "red_flags": [], "segments": {"dimensions": {}, "warnings": []}}
        nseg = len(filing.get("segments", []))
        nflags = len(analysis.get("red_flags", []))
        summary = (f"Extracted {len(filing.get('statements', []))} statements, "
                   f"{nseg} segments, {len(filing.get('notes', []))} notes, "
                   f"audit={filing.get('audit', {}).get('opinion_type')}, {nflags} red flag(s).")
        if problems:
            summary += f" ({len(problems)} note(s) below — review before uploading.)"
        else:
            summary += " Clean — ready to upload to qscreen.app."

        return {
            "summary": summary,
            "problems": problems,
            "filing": filing,
            "analysis": analysis,
            "filename": f"{symbol}_{year}_{period}_filing.json",
        }
    except SystemExit as e:                       # provider/key/model config errors
        return {"error": str(e)}, 400
    except Exception as e:
        # Log the full traceback server-side; do NOT leak it to the client (paths,
        # library internals, and any input echoed in the message).
        traceback.print_exc()
        return {"error": f"{type(e).__name__}: {e}"}, 500


@app.route("/workbook", methods=["POST"])
def workbook_route():
    """Excel financial-transcript workbook for a filing (or several, for more
    years). Body: {filing|filings}. Returns the .xlsx bytes as a download."""
    payload = request.get_json(silent=True) or {}
    filings = payload.get("filings")
    if filings is None and isinstance(payload.get("filing"), dict):
        filings = [payload["filing"]]
    if not isinstance(filings, list) or not filings:
        return {"error": "missing 'filings' (list) or 'filing' (object)"}, 400
    try:
        data = qscreen_workbook.workbook_bytes(filings[-1], filings)
    except Exception as e:
        return {"error": str(e)}, 400
    sym = _safe_filename((filings[-1].get("metadata") or {}).get("symbol"))
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{sym}_transcript.xlsx"'})


@app.route("/ttm", methods=["POST"])
def ttm_route():
    """Period-aware TTM / quarterly roll-up for one company. Body: {filings|filing}."""
    payload = request.get_json(silent=True) or {}
    filings = payload.get("filings")
    if filings is None and isinstance(payload.get("filing"), dict):
        filings = [payload["filing"]]
    if not isinstance(filings, list) or not filings:
        return {"error": "missing 'filings' (list) or 'filing' (object)"}, 400
    try:
        return qscreen_periods.build_ttm(filings)
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/statements", methods=["POST"])
def statements_route():
    """Printable HTML statements document for a filing. Body: {filing}. Returns {html}."""
    payload = request.get_json(silent=True) or {}
    filing = payload.get("filing")
    if not isinstance(filing, dict):
        return {"error": "missing 'filing' object"}, 400
    try:
        return {"html": qscreen_statements.render_statements_html(filing)}
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/export.csv", methods=["POST"])
def export_csv_route():
    """Flat line-items CSV for a filing. Body: {filing}. Returns text/csv."""
    import csv
    import io as _io
    payload = request.get_json(silent=True) or {}
    filing = payload.get("filing")
    if not isinstance(filing, dict):
        return {"error": "missing 'filing' object"}, 400
    buf = _io.StringIO()
    w = csv.DictWriter(buf, fieldnames=engine.EXPORT_COLUMNS)
    w.writeheader()
    w.writerows(engine.flatten_line_items(filing))
    sym = _safe_filename((filing.get("metadata") or {}).get("symbol"))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{sym}_line_items.csv"'})


@app.route("/analyze", methods=["POST"])
def analyze_route():
    """Full analysis (ratios/trends/red-flags/segments) for one or more filing
    JSONs of the same stock. Accepts {filings:[...]} or {filing:{...}}."""
    payload = request.get_json(silent=True) or {}
    filings = payload.get("filings")
    if filings is None and isinstance(payload.get("filing"), dict):
        filings = [payload["filing"]]
    if not isinstance(filings, list) or not filings:
        return {"error": "missing 'filings' (list) or 'filing' (object)"}, 400
    meta = (filings[-1].get("metadata") or {})
    symbol = payload.get("symbol") or meta.get("symbol") or ""
    if not symbol:
        return {"error": "could not determine symbol"}, 400
    profile = qatar.profile_for_year(symbol, meta.get("fiscal_year"))
    try:
        return qscreen_analyze.analyze(symbol, filings, profile)
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/portfolio", methods=["POST"])
def portfolio_route():
    """Screen & rank a basket. Body: {filings:[...]} (grouped by symbol). Returns
    the ranked board plus a ready-to-download HTML dashboard."""
    payload = request.get_json(silent=True) or {}
    filings = payload.get("filings")
    if not isinstance(filings, list) or not filings:
        return {"error": "missing 'filings' (list)"}, 400
    groups = qscreen_analyze.group_by_symbol(filings)
    if not groups:
        return {"error": "no filings carry a metadata.symbol"}, 400
    profiles = {s: qatar.profile_for_year(s, (fs[0].get("metadata") or {}).get("fiscal_year"))
                for s, fs in groups.items()}
    try:
        board = qscreen_portfolio.roll_up(groups, profiles)
        return {"count": board["count"], "rows": board["rows"],
                "html": qscreen_portfolio.render_html(board)}
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/report", methods=["POST"])
def report_route():
    """Build the one-page analyst report (HTML + Markdown) for a filing/series."""
    payload = request.get_json(silent=True) or {}
    filings = payload.get("filings")
    if filings is None and isinstance(payload.get("filing"), dict):
        filings = [payload["filing"]]
    if not isinstance(filings, list) or not filings:
        return {"error": "missing 'filings' (list) or 'filing' (object)"}, 400
    meta = (filings[-1].get("metadata") or {})
    symbol = payload.get("symbol") or meta.get("symbol") or ""
    if not symbol:
        return {"error": "could not determine symbol"}, 400
    profile = qatar.profile_for_year(symbol, meta.get("fiscal_year"))
    try:
        rep = qscreen_report.build_report(symbol, filings, profile,
                                          assumptions=payload.get("assumptions") or {},
                                          price=payload.get("price"), shares=payload.get("shares"))
        return {"symbol": rep["symbol"], "html": rep["html"], "markdown": rep["markdown"]}
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/compare", methods=["POST"])
def compare_route():
    """Rank a stock against peers. Body: {filings:[...]} (grouped by symbol) or
    {filings_by_symbol:{SYM:[...]}}, optional {target}."""
    payload = request.get_json(silent=True) or {}
    fbs = payload.get("filings_by_symbol")
    if fbs is None:
        filings = payload.get("filings")
        if not isinstance(filings, list) or not filings:
            return {"error": "missing 'filings' (list) or 'filings_by_symbol' (object)"}, 400
        fbs = qscreen_analyze.group_by_symbol(filings)
    if not fbs:
        return {"error": "no filings carry a metadata.symbol"}, 400
    target = (payload.get("target") or next(iter(fbs))).upper()
    profiles = {s: qatar.profile_for_year(s, (fs[0].get("metadata") or {}).get("fiscal_year"))
                for s, fs in fbs.items()}
    try:
        return qscreen_analyze.compare(target, fbs, profiles)
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/dcf", methods=["POST"])
def dcf_route():
    """Run the valuation simulator for a filing/series with adjustable
    assumptions. Body: {filing|filings, symbol?, assumptions{}, price?, shares?}."""
    payload = request.get_json(silent=True) or {}
    filings = payload.get("filings")
    if filings is None and isinstance(payload.get("filing"), dict):
        filings = [payload["filing"]]
    if not isinstance(filings, list) or not filings:
        return {"error": "missing 'filings' (list) or 'filing' (object)"}, 400
    meta = (filings[-1].get("metadata") or {})
    symbol = payload.get("symbol") or meta.get("symbol") or ""
    if not symbol:
        return {"error": "could not determine symbol"}, 400
    profile = qatar.profile_for_year(symbol, meta.get("fiscal_year"))
    try:
        return qscreen_dcf.value(symbol, filings, profile, payload.get("assumptions") or {},
                                 price=payload.get("price"), shares=payload.get("shares"))
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/segments", methods=["POST"])
def segments():
    """Re-run the segment breakdown for a filing JSON (uses the Qatar profile
    for FX/event annotations when the symbol+year resolve)."""
    payload = request.get_json(silent=True) or {}
    filing = payload.get("filing")
    if not isinstance(filing, dict):
        return {"error": "missing 'filing' object"}, 400
    meta = filing.get("metadata") or {}
    profile = qatar.profile_for_year(meta.get("symbol") or payload.get("symbol") or "",
                                     meta.get("fiscal_year") or payload.get("year"))
    try:
        return qscreen_analyze.analyze_segments(filing, profile)
    except Exception as e:
        return {"error": str(e)}, 400


@app.route("/upload", methods=["POST"])
def upload():
    """Opt-in upload of an already-extracted filing to qscreen.app.

    Only enabled when the server has INGEST_TOKEN set; the extract step never
    uploads on its own — the user clicks Upload explicitly. A non-conforming
    filing is rejected here too, mirroring the CLI's safety gate.
    """
    token = os.getenv("INGEST_TOKEN")
    if not token:
        return {"error": "No INGEST_TOKEN configured on the server; cannot upload."}, 400
    payload = request.get_json(silent=True) or {}
    filing = payload.get("filing")
    if not isinstance(filing, dict):
        return {"error": "missing 'filing' object"}, 400
    problems = engine.validate_filing(filing)
    if problems:
        return {"error": "filing is non-conforming; not uploading", "problems": problems}, 400
    args = SimpleNamespace(
        api_url=os.getenv("QSCREEN_API_URL", "http://localhost:3004"), token=token)
    # Both outputs: optionally fold the derived analysis into the upload (additive).
    analysis = payload.get("analysis") if payload.get("with_analysis") else None
    try:
        resp = (engine.upload_filing(filing, args, analysis) if analysis is not None
                else engine.upload_filing(filing, args))
        return {"ok": True, "response": resp}
    except Exception as e:
        return {"error": str(e)}, 502


def main() -> None:
    host = os.getenv("QSCREEN_APP_HOST", "127.0.0.1")
    port = int(os.getenv("QSCREEN_APP_PORT", "8765"))
    print(f"\n  QScreen Filing Ingestor — open  http://{host}:{port}  in your browser\n")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  ⚠️  Binding to {host} exposes this tool (and any INGEST_TOKEN) on your "
              "network. It has no authentication — only do this on a trusted network.\n")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
