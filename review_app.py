"""simsa 심사 검토 서버 — 업로드 → VLM 탐지 → 빨간 박스 검토 → 골든셋 확정.

ZIP 업로드하면 batch_convert 로 PDF 정규화 후 파일마다 팩 규칙 기반 필드 탐지
(detect_fields)를 돌리고, 페이지 이미지 위 박스 오버레이로 검토한다.
사용자가 맞음/틀림 피드백을 주고 파일 판정을 확정하면 golden_verdicts 에 쌓인다.

usage: python review_app.py [--host 127.0.0.1] [--port 8766]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import db
from detect_fields import PROMPT_VERSION, detect, render_pages, suggest_rules
from vlm_cache import VLMCache
from vlm_screen import load_api_key

BASE = Path(__file__).parent
DATA = BASE / "data/review"

INDEX_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>simsa 심사 검토</title><style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f6f7f9;color:#17202a}
.wrap{max-width:1280px;margin:0 auto;padding:20px}header{background:white;border-bottom:1px solid #d9dee7}
header .wrap{display:flex;align-items:center;gap:14px;padding-top:14px;padding-bottom:14px}
h1{font-size:20px;margin:0}h1 a{color:inherit;text-decoration:none}h2{font-size:15px;margin:0 0 12px}
main{display:grid;grid-template-columns:440px 1fr;gap:18px;align-items:start}
.card{background:white;border:1px solid #d9dee7;border-radius:8px;padding:16px;margin-bottom:18px}
label{display:block;margin:12px 0 6px;font-weight:700;font-size:13px}
input,select,button,textarea{border-radius:6px;font-size:13px;box-sizing:border-box}
input,select,textarea{border:1px solid #d9dee7;padding:7px;width:100%}
button{border:0;background:#146c5f;color:white;font-weight:700;cursor:pointer;padding:8px 12px}
button.ghost{background:white;color:#17202a;border:1px solid #d9dee7}button.sm{padding:4px 8px;font-size:12px}
button:disabled{background:#98a2b3}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #eef1f5;padding:7px 6px;text-align:left;vertical-align:top}
.hint{color:#667085;font-size:12px;line-height:1.5}.row{display:flex;gap:8px;align-items:center}
.badge{display:inline-block;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700}
.b-ready,.b-detected,.b-pass{background:#e5f5ec;color:#087443}.b-processing{background:#fff3e6;color:#9a3412}
.b-error,.b-fail{background:#fde8e8;color:#b42318}.b-skipped{background:#eef1f5;color:#667085}
.b-uncertain{background:#fff8e1;color:#92400e}.b-golden{background:#ede9fe;color:#5b21b6}
.item{border:1px solid #eef1f5;border-radius:8px;padding:10px;margin-bottom:8px;cursor:pointer}
.item:hover{border-color:#146c5f}.item.active{border-color:#146c5f;background:#f0faf7}
.pagewrap{position:relative;margin-bottom:14px;border:1px solid #d9dee7;border-radius:6px;overflow:hidden}
.pagewrap img{width:100%;display:block}
.dbox{position:absolute;border:2px solid #e24b4a;border-radius:2px;cursor:pointer}
.dbox.correct{border-color:#087443}.dbox.wrong{border-color:#98a2b3;border-style:dashed}
.dbox .tag{position:absolute;top:-20px;left:-2px;background:#fde8e8;color:#791f1f;font-size:11px;font-weight:700;padding:1px 6px;border-radius:3px;white-space:nowrap}
.dbox.correct .tag{background:#e5f5ec;color:#087443}.dbox.wrong .tag{background:#eef1f5;color:#667085}
.det{border:1px solid #eef1f5;border-radius:8px;padding:10px;margin-bottom:8px}
.det .val{font-family:ui-monospace,monospace;font-size:13px;margin:4px 0}
.det.correct{border-color:#087443}.det.wrong{border-color:#98a2b3;opacity:.75}
.agg-val{display:flex;justify-content:space-between;font-size:13px;padding:3px 0}
.warn{color:#b42318;font-weight:700}
@media(max-width:960px){main{grid-template-columns:1fr}}
</style></head><body>
<header><div class="wrap"><h1><a href="#home">simsa 심사 검토</a></h1><span class="hint" id="crumb"></span>
<a href="#golden" style="margin-left:auto;font-size:13px;font-weight:700;color:#146c5f;text-decoration:none">골든셋 검사</a></div></header>
<div class="wrap"><main id="main"></main></div>
<script>
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=async(url,opt)=>{const r=await fetch(url,opt);if(!r.ok)throw new Error(await r.text());return r.json()};
let pollTimer=null;

function route(){clearInterval(pollTimer);const h=location.hash||'#home';
  if(h.startsWith('#sub/'))return viewSubmission(+h.slice(5));
  if(h.startsWith('#file/'))return viewFile(+h.slice(6));
  if(h==='#golden')return viewGolden();
  viewHome()}
window.addEventListener('hashchange',route);

async function viewHome(){$('#crumb').textContent='';
  const d=await api('/api/bootstrap');const pack=d.packs[0];
  $('#main').innerHTML=`
  <section><div class="card"><h2>제출건 업로드</h2><form id="upform">
    <label>심사 팩</label><select name="pack_id">${d.packs.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select>
    <label>제출 ZIP (제출건 폴더들이 든 ZIP)</label><input name="zip" type="file" accept=".zip" required>
    <button style="margin-top:14px;width:100%">업로드 + 탐지 시작</button>
    <p class="hint">파일마다 문서 유형을 판별하고 오른쪽 규칙표의 필드를 탐지합니다. 같은 파일+같은 규칙이면 캐시를 써서 API 호출이 없습니다.</p></form></div>
  <div class="card"><h2>제출건</h2><div id="subs">불러오는 중…</div></div></section>
  <section><div class="card"><h2>문서 유형 레지스트리 — ${esc(pack.name)}</h2>
    <p class="hint">"어떤 서류가 들어와야 하는가"의 기준. 내용 기반 VLM 판별이 주, 파일명 힌트는 보조입니다. 필수 유형이 빠지면 제출건 화면에 경고가 뜹니다.</p>
    <table><thead><tr><th>유형</th><th>필수</th><th>파일명 힌트</th><th></th></tr></thead>
    <tbody>${pack.doc_types.map(t=>`<tr><td><b>${esc(t.name)}</b><div class="hint">${esc(t.description)}</div></td>
      <td>${t.required?'✓':''}</td><td class="hint">${esc((t.filename_hints||[]).join(', '))}</td>
      <td><button class="ghost sm" onclick="delDocType(${t.id})">삭제</button></td></tr>`).join('')}</tbody></table>
    <form id="dtform" class="row" style="margin-top:12px">
      <input name="name" placeholder="유형 이름" required style="width:160px">
      <label class="row" style="margin:0;white-space:nowrap;font-weight:400"><input type="checkbox" name="required" style="width:auto" checked> 필수</label>
      <input name="filename_hints" placeholder="파일명 힌트 (쉼표 구분)" style="width:160px">
      <input name="description" placeholder="설명 (선택)">
      <button class="sm" style="white-space:nowrap">유형 추가</button></form></div>
  <div class="card"><h2>탐지 규칙</h2>
    <p class="hint">extract 는 값을 뽑아 교차대조, verify 는 pass/fail/uncertain 판정(서명 유효성 등). 문서 유형 '*' 는 모든 문서에 적용. 규칙 변경 후엔 파일 화면에서 '재탐지'.</p>
    <table><thead><tr><th>종류</th><th>문서 유형</th><th>필드</th><th>설명</th><th></th></tr></thead>
    <tbody>${pack.rules.map(r=>`<tr><td><span class="badge ${r.rule_type==='verify'?'b-uncertain':'b-skipped'}">${esc(r.rule_type)}</span></td>
      <td>${esc(r.doc_type)}</td><td><b>${esc(r.field)}</b></td><td class="hint">${esc(r.instruction)}</td>
      <td><button class="ghost sm" onclick="delRule(${r.id})">삭제</button></td></tr>`).join('')}</tbody></table>
    <form id="ruleform" class="row" style="margin-top:12px">
      <select name="rule_type" style="width:92px"><option value="extract">extract</option><option value="verify">verify</option></select>
      <input name="doc_type" placeholder="문서 유형 (예: 사업자등록증, *)" required style="width:150px">
      <input name="field" placeholder="필드 (예: 대표자 성명)" required style="width:130px">
      <input name="instruction" placeholder="설명 (선택)">
      <button class="sm" style="white-space:nowrap">규칙 추가</button></form></div></section>`;
  $('#ruleform').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);
    await api('/api/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pack_id:pack.id,rule_type:f.get('rule_type'),doc_type:f.get('doc_type'),field:f.get('field'),instruction:f.get('instruction')})});viewHome()});
  $('#dtform').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);
    await api('/api/doc_types',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pack_id:pack.id,name:f.get('name'),required:!!f.get('required'),filename_hints:f.get('filename_hints'),description:f.get('description')})});viewHome()});
  $('#upform').addEventListener('submit',async e=>{e.preventDefault();e.target.querySelector('button').disabled=true;
    const j=await api('/api/submissions',{method:'POST',body:new FormData(e.target)});location.hash='#sub/'+j.id});
  renderSubs();pollTimer=setInterval(renderSubs,3000)}

async function renderSubs(){const subs=await api('/api/submissions');
  const el=$('#subs');if(!el)return;
  el.innerHTML=subs.length?subs.map(s=>`<div class="item" onclick="location.hash='#sub/${s.id}'">
    <div class="row" style="justify-content:space-between"><b>${esc(s.name)}</b><span class="badge b-${esc(s.status)}">${esc(s.status)}</span></div>
    <div class="hint">파일 ${s.file_count} · 탐지 ${s.detection_count} · 골든 ${s.golden_count} · ${esc((s.created_at||'').slice(0,16).replace('T',' '))}</div>
    ${s.error?`<div class="hint warn">${esc(s.error)}</div>`:''}</div>`).join(''):'<p class="hint">아직 없음. ZIP 을 올려보세요.</p>'}

async function viewSubmission(id){const d=await api('/api/submissions/'+id);
  $('#crumb').textContent='› '+d.name;
  const aggHtml=Object.entries(d.aggregation).map(([field,vals])=>{
    const multi=vals.length>1;
    return `<div class="det"><div class="row" style="justify-content:space-between"><b>${esc(field)}</b>
      <span class="${multi?'warn':'hint'}" style="font-size:12px">${multi?'값 '+vals.length+'종 — 확인 필요':vals[0]?vals[0].n+'개 파일 일치':''}</span></div>
      ${vals.map(v=>`<div class="agg-val"><span style="font-family:ui-monospace,monospace">${esc(v.val)}</span><span class="hint">${v.n}개 파일</span></div>`).join('')}</div>`}).join('');
  const missing=d.checklist.filter(c=>c.required&&!c.present);
  const checkHtml=d.checklist.map(c=>`<div class="agg-val">
    <span>${c.present?'<span style="color:#087443">✓</span>':(c.required?'<span class="warn">✗</span>':'<span class="hint">–</span>')}
    ${esc(c.name)}${c.required?'':' <span class="hint">(선택)</span>'}</span>
    <span class="hint">${c.files.length?esc(c.files.length+'건'):''}</span></div>`).join('');
  $('#main').innerHTML=`
  <section class="card"><h2>파일 ${d.files.length}건 <span class="badge b-${esc(d.status)}">${esc(d.status)}</span></h2>
    ${d.files.map(f=>`<div class="item" onclick="location.hash='#file/${f.id}'">
      <div class="row" style="justify-content:space-between"><span style="font-size:13px">${esc(f.filename)}</span>
      <span class="row">${f.golden_count?`<span class="badge b-golden">골든 ${f.golden_count}</span>`:''}<span class="badge b-${esc(f.status)}">${esc(f.status)}</span></span></div>
      <div class="hint">${esc(f.doc_type||'유형 미판별')}${f.status==='detected'&&!f.doc_type_registered?' <span class="badge b-uncertain">미등록 유형</span>':''} · ${f.page_count}p · 탐지 ${f.detection_count}건${f.error?' · <span class=warn>'+esc(f.error)+'</span>':''}</div></div>`).join('')}</section>
  <section><div class="card"><h2>필수서류 완비 체크 ${missing.length?`<span class="badge b-fail">필수 ${missing.length}종 누락</span>`:`<span class="badge b-pass">완비</span>`}</h2>
    ${checkHtml}
    ${d.unmatched_files.length?`<p class="hint" style="margin-bottom:0"><b>어느 유형에도 안 잡힌 파일:</b> ${d.unmatched_files.map(esc).join(', ')}</p>`:''}</div>
  <div class="card"><h2>교차 탐지 현황</h2><p class="hint">틀림 처리한 탐지는 제외. 같은 필드에 값이 여러 종이면 표기 불일치 또는 오탐입니다.</p>${aggHtml||'<p class="hint">탐지 결과 대기 중…</p>'}</div></section>`;
  if(d.status==='processing')pollTimer=setInterval(async()=>{const s=await api('/api/submissions/'+id);if(s.status!=='processing'){clearInterval(pollTimer);viewSubmission(id)}},3000)}

let FD=null;
async function viewFile(id){FD=await api('/api/files/'+id);
  $('#crumb').innerHTML=`› <a href="#sub/${FD.submission_id}">${esc(FD.submission_name)}</a> › ${esc(FD.filename)}`;
  const dets=FD.detections;
  const pagesHtml=FD.pages.map(p=>{
    const boxes=dets.filter(d=>d.page_no===p.page_no).map(d=>{
      const[y1,x1,y2,x2]=d.box;
      return `<div class="dbox ${esc(d.feedback)}" id="box${d.id}" onclick="focusDet(${d.id})"
        style="top:${y1/10}%;left:${x1/10}%;width:${(x2-x1)/10}%;height:${(y2-y1)/10}%">
        <span class="tag">${esc(d.field)}</span></div>`}).join('');
    return `<div class="pagewrap"><img src="/pageimg/${p.id}" loading="lazy">${boxes}
      <span class="hint" style="position:absolute;right:6px;bottom:4px">p${p.page_no}</span></div>`}).join('');
  const detsHtml=dets.map(d=>`<div class="det ${esc(d.feedback)}" id="det${d.id}">
    <div class="row" style="justify-content:space-between"><span><b>${esc(d.field)}</b>
    ${d.verdict?` <span class="badge b-${esc(d.verdict)}">${esc(d.verdict)}</span>`:''}</span>
    <span class="hint">p${d.page_no} · conf ${(d.confidence??0).toFixed(2)}</span></div>
    <div class="val">${esc(d.value)}</div>
    <div class="row"><button class="sm ${d.feedback==='correct'?'':'ghost'}" onclick="feedback(${d.id},'correct')">맞음</button>
    <button class="sm ${d.feedback==='wrong'?'':'ghost'}" onclick="feedback(${d.id},'wrong')">틀림</button>
    <input placeholder="정정 값 (틀림일 때)" value="${esc(d.corrected_value)}" onchange="feedback(${d.id},'wrong',this.value)" style="flex:1"></div></div>`).join('');
  const goldenBadge=FD.golden.length?`<span class="badge b-golden">골든셋 등록됨 (${FD.golden.length}필드)</span>`:'';
  const unregHtml=FD.status==='detected'&&!FD.doc_type_registered?`
    <div class="det" style="border-color:#f0b429;background:#fffbeb">
      <b>미등록 유형: ${esc(FD.doc_type||'미상')}</b>
      <p class="hint" style="margin:4px 0 8px">${esc(FD.doc_type_evidence)}</p>
      <button class="sm" onclick="registerDocType()">이 유형을 레지스트리에 등록</button></div>`:'';
  $('#main').innerHTML=`
  <section>${pagesHtml||'<div class="card"><p class="hint">페이지 이미지 없음 (변환 안 된 파일)</p></div>'}</section>
  <section style="position:sticky;top:14px"><div class="card" style="max-height:52vh;overflow:auto"><h2>탐지 ${dets.length}건 — ${esc(FD.doc_type||'유형 미판별')}</h2>
    ${unregHtml}${detsHtml||'<p class="hint">탐지된 필드 없음</p>'}
    <button class="ghost sm" style="width:100%" onclick="suggestRules()" id="sugbtn">이 문서에서 점검할 규칙 제안 받기</button>
    <div id="suggestions"></div></div>
  <div class="card"><h2>골든셋 확정 ${goldenBadge}</h2>
    <p class="hint">'맞음' 표시(또는 정정 값 입력)된 탐지가 이 파일의 정답으로 저장됩니다.</p>
    <label>파일 판정</label><select id="gverdict"><option value="pass">pass</option><option value="fail">fail</option><option value="uncertain">uncertain</option></select>
    <label>메모</label><input id="gnote" placeholder="적발 사항 등 (선택)">
    <button style="margin-top:12px;width:100%" onclick="saveGolden()">골든셋으로 저장</button>
    <button class="ghost" style="margin-top:8px;width:100%" onclick="redetect()">현재 규칙으로 재탐지</button></div></section>`;
  if(FD.golden.length){$('#gverdict').value=FD.golden[0].verdict;$('#gnote').value=FD.golden[0].note}}

function focusDet(id){const el=document.getElementById('det'+id);if(el){el.scrollIntoView({block:'center',behavior:'smooth'});el.style.outline='2px solid #146c5f';setTimeout(()=>el.style.outline='',900)}}
async function feedback(id,fb,cv){const d=FD.detections.find(x=>x.id===id);
  if(cv===undefined&&d.feedback===fb)fb='';
  await api('/api/detections/'+id+'/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({feedback:fb,corrected_value:cv??d.corrected_value})});
  viewFile(FD.id)}
async function saveGolden(){
  const items=FD.detections.filter(d=>d.feedback==='correct'||d.corrected_value)
    .map(d=>({field:d.field,expected_value:d.verdict?d.verdict:(d.corrected_value||d.value),source_detection_id:d.id}));
  if(!items.length&&!confirm('맞음 표시된 탐지가 없습니다. 판정만 저장할까요?'))return;
  await api('/api/files/'+FD.id+'/golden',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({verdict:$('#gverdict').value,note:$('#gnote').value,items})});
  viewFile(FD.id)}
async function redetect(){if(!confirm('현재 규칙으로 이 파일을 다시 탐지합니다. 기존 피드백 없는 탐지는 교체됩니다.'))return;
  await api('/api/files/'+FD.id+'/redetect',{method:'POST'});
  const wait=setInterval(async()=>{const f=await api('/api/files/'+FD.id);if(f.status!=='pending'){clearInterval(wait);viewFile(FD.id)}},2000)}
async function viewGolden(){$('#crumb').textContent='› 골든셋 검사';
  const runs=await api('/api/golden_runs');
  $('#main').innerHTML=`
  <section class="card"><h2>골든셋 검사 — #2 골든 러너</h2>
    <p class="hint">검토 화면에서 확정한 정답(골든셋) 파일을 <b>현재 규칙으로 다시 탐지</b>해 정답과 대조합니다.
    규칙·프롬프트를 바꾼 뒤 돌리면 기존 정답이 깨졌는지(회귀) 바로 보여요. 같은 파일+같은 규칙이면 캐시를 써서 비용이 없습니다.</p>
    <button style="width:100%" onclick="startGolden()" id="grunbtn">지금 검사 실행</button>
    <h2 style="margin-top:18px">실행 이력</h2>
    <div id="runlist">${runs.map(r=>`<div class="item" onclick="showRun(${r.id})">
      <div class="row" style="justify-content:space-between"><b>검사 #${r.id}</b>
      <span class="badge ${r.status==='error'?'b-error':r.status==='running'?'b-processing':(r.matched===r.total?'b-pass':'b-fail')}">
      ${r.status==='done'?`${r.matched}/${r.total} 일치`:esc(r.status)}</span></div>
      <div class="hint">${esc((r.created_at||'').slice(0,19).replace('T',' '))}</div></div>`).join('')||'<p class="hint">아직 실행한 검사가 없어요.</p>'}</div></section>
  <section class="card" id="rundetail"><p class="hint">왼쪽에서 검사를 실행하거나 이력을 선택하면 결과가 여기 표시됩니다.</p></section>`;
  if(runs[0])showRun(runs[0].id)}
async function showRun(id){const d=await api('/api/golden_runs/'+id);const el=$('#rundetail');if(!el)return;
  if(d.status==='running'){el.innerHTML='<p class="hint">검사 실행 중… (규칙이 바뀐 파일은 VLM 재호출)</p>';
    clearInterval(pollTimer);pollTimer=setInterval(()=>showRun(id),2500);return}
  clearInterval(pollTimer);
  if(d.status==='error'){el.innerHTML=`<p class="warn">실행 실패: ${esc((d.results[0]||{}).error||'')}</p>`;return}
  const byFile={};(d.results||[]).forEach(r=>{(byFile[r.filename]=byFile[r.filename]||[]).push(r)});
  const pct=d.total?Math.round(d.matched/d.total*100):0;
  el.innerHTML=`<h2>검사 #${d.id} 결과
    <span class="badge ${d.matched===d.total?'b-pass':'b-fail'}">${d.matched}/${d.total} 일치 (${pct}%)</span></h2>
    ${Object.entries(byFile).map(([fn,rows])=>`
      <div class="det"><b style="font-size:13px">${esc(fn)}</b>
      <table style="margin-top:6px"><thead><tr><th></th><th>필드</th><th>정답</th><th>이번 탐지</th></tr></thead><tbody>
      ${rows.map(r=>`<tr${r.ok?'':' style="background:#fde8e8"'}>
        <td>${r.ok?'<span style="color:#087443">✓</span>':'<span class="warn">✗</span>'}</td>
        <td><b>${esc(r.field)}</b></td>
        <td style="font-family:ui-monospace,monospace;font-size:12px">${esc(r.expected)}</td>
        <td style="font-family:ui-monospace,monospace;font-size:12px">${r.got.map(esc).join('<br>')||'<span class="hint">(미탐지)</span>'}</td></tr>`).join('')}
      </tbody></table></div>`).join('')}`}
async function startGolden(){const btn=$('#grunbtn');btn.disabled=true;
  try{const r=await api('/api/golden_runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pack_id:1})});
    showRun(r.id);const runs=await api('/api/golden_runs');
    $('#runlist').innerHTML=runs.map(r2=>`<div class="item" onclick="showRun(${r2.id})">
      <div class="row" style="justify-content:space-between"><b>검사 #${r2.id}</b>
      <span class="badge ${r2.status==='running'?'b-processing':(r2.matched===r2.total?'b-pass':'b-fail')}">${r2.status==='done'?`${r2.matched}/${r2.total} 일치`:esc(r2.status)}</span></div>
      <div class="hint">${esc((r2.created_at||'').slice(0,19).replace('T',' '))}</div></div>`).join('')}
  catch(e){alert(e.message)}
  finally{btn.disabled=false}}
async function delRule(id){if(!confirm('규칙을 삭제할까요?'))return;await api('/api/rules/'+id,{method:'DELETE'});viewHome()}
async function delDocType(id){if(!confirm('문서 유형을 삭제할까요?'))return;await api('/api/doc_types/'+id,{method:'DELETE'});viewHome()}
async function registerDocType(){const hints=prompt('파일명 힌트 (쉼표 구분, 선택)','')??'';
  await api('/api/doc_types',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pack_id:FD.pack_id,name:FD.doc_type,required:false,filename_hints:hints,description:FD.doc_type_evidence})});
  alert('등록됐어요. 재탐지하면 등록 유형으로 판별됩니다.');viewFile(FD.id)}
let SUG=[];
async function suggestRules(){const btn=$('#sugbtn');btn.disabled=true;btn.textContent='VLM 이 문서를 읽는 중…';
  try{const r=await api('/api/files/'+FD.id+'/suggest_rules',{method:'POST'});SUG=r.suggestions;
    $('#suggestions').innerHTML=SUG.length?SUG.map((s,i)=>`<div class="det" id="sug${i}">
      <div class="row" style="justify-content:space-between"><span><span class="badge ${s.rule_type==='verify'?'b-uncertain':'b-skipped'}">${esc(s.rule_type)}</span> <b>${esc(s.field)}</b></span>
      <button class="sm" onclick="addSuggested(${i})">추가</button></div>
      <p class="hint" style="margin:6px 0 0">${esc(s.instruction)}<br><i>왜: ${esc(s.why)}</i></p></div>`).join('')
      :'<p class="hint">제안할 규칙이 없대요.</p>'}
  finally{btn.disabled=false;btn.textContent='이 문서에서 점검할 규칙 제안 받기'}}
async function addSuggested(i){const s=SUG[i];
  await api('/api/rules',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pack_id:FD.pack_id,rule_type:s.rule_type,doc_type:s.doc_type,field:s.field,instruction:s.instruction})});
  document.getElementById('sug'+i).style.opacity=.4;document.querySelector('#sug'+i+' button').textContent='추가됨'}
route();
</script></body></html>"""


def parse_multipart(headers, body: bytes) -> dict:
    msg = BytesParser(policy=default).parsebytes(
        f"Content-Type: {headers.get('Content-Type', '')}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    parts: dict[str, tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            parts[name] = (part.get_filename() or "", part.get_payload(decode=True) or b"")
    return parts


def pack_rules(conn, pack_id: int) -> list[dict]:
    return conn.execute(
        "SELECT id, doc_type, field, rule_type, instruction FROM rules WHERE pack_id = %s ORDER BY id", (pack_id,)
    ).fetchall()


def pack_doc_types(conn, pack_id: int) -> list[dict]:
    return conn.execute(
        "SELECT id, name, required, filename_hints, description FROM doc_types WHERE pack_id = %s ORDER BY id",
        (pack_id,),
    ).fetchall()


def detect_file(conn, file_id: int, pdf: Path, rules: list[dict], doc_types: list[dict],
                cache: VLMCache, api_key: str) -> None:
    """파일 하나: 페이지 렌더(없으면) + 탐지 + DB 기록."""
    page_dir = DATA / f"file_{file_id}"
    have = conn.execute("SELECT count(*) AS n FROM pages WHERE file_id = %s", (file_id,)).fetchone()["n"]
    if not have:
        pages = render_pages(pdf, page_dir)
        for p in pages:
            conn.execute(
                "INSERT INTO pages (file_id, page_no, image_path, width, height) VALUES (%s, %s, %s, %s, %s)",
                (file_id, p["page_no"], p["image_path"], p["width"], p["height"]),
            )
        conn.execute("UPDATE files SET page_count = %s WHERE id = %s", (len(pages), file_id))
    result, _ = detect(pdf, rules, doc_types, cache, api_key)
    conn.execute("DELETE FROM detections WHERE file_id = %s AND feedback = ''", (file_id,))
    # 피드백 있는 탐지는 보존되므로, 같은 항목이 다시 나오면 중복 삽입하지 않는다
    kept = {(r["field"], r["page_no"], r["value"]) for r in conn.execute(
        "SELECT field, page_no, value FROM detections WHERE file_id = %s", (file_id,)).fetchall()}
    result["detections"] = [
        d for d in result["detections"]
        if (d["field"], int(d.get("page") or 1), str(d.get("value") or "")) not in kept
    ]
    for d in result["detections"]:
        conn.execute(
            "INSERT INTO detections (file_id, page_no, rule_id, field, value, verdict, box, confidence, model) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (file_id, int(d.get("page") or 1), d.get("rule_id"), d["field"], str(d.get("value") or ""),
             d.get("verdict") or "", [int(v) for v in d["box_2d"]], float(d.get("confidence") or 0),
             "gemini-flash-latest"),
        )
    conn.execute(
        "UPDATE files SET doc_type = %s, doc_type_registered = %s, doc_type_evidence = %s, "
        "status = 'detected', error = '' WHERE id = %s",
        (result.get("doc_type") or "", bool(result.get("doc_type_registered")),
         result.get("doc_type_evidence") or "", file_id),
    )


def _norm(s) -> str:
    return "".join(str(s or "").split())


def run_golden(run_id: int, pack_id: int) -> None:
    """골든 러너(#2): 골든셋이 있는 파일을 현재 규칙으로 재탐지해 정답과 대조."""
    api_key = load_api_key()
    cache = VLMCache(BASE / "cache/vlm", prompt_version=PROMPT_VERSION)
    try:
        with db.connect() as conn:
            rules = pack_rules(conn, pack_id)
            doc_types = pack_doc_types(conn, pack_id)
            rows = conn.execute(
                "SELECT g.file_id, g.field, g.expected_value, f.filename, f.pdf_path "
                "FROM golden_verdicts g JOIN files f ON f.id = g.file_id "
                "WHERE g.pack_id = %s AND g.field <> '_file' ORDER BY g.file_id, g.field", (pack_id,),
            ).fetchall()
        by_file: dict[int, list[dict]] = {}
        for r in rows:
            by_file.setdefault(r["file_id"], []).append(r)
        results, matched, total = [], 0, 0
        for file_id, items in by_file.items():
            filename = items[0]["filename"]
            try:
                det, _ = detect(Path(items[0]["pdf_path"]), rules, doc_types, cache, api_key, filename=filename)
                dets = det["detections"]
            except Exception as e:
                for it in items:
                    total += 1
                    results.append({"file_id": file_id, "filename": filename, "field": it["field"],
                                    "expected": it["expected_value"], "got": [f"탐지 실패: {e}"], "ok": False})
                continue
            for it in items:
                total += 1
                got = [d for d in dets if d["field"] == it["field"]]
                if any(d.get("verdict") for d in got):  # verify 골든은 verdict 대조
                    got_vals = [d.get("verdict") or "" for d in got]
                else:                                   # extract 골든은 값 대조 (공백 무시)
                    got_vals = [str(d.get("value") or "") for d in got]
                ok = any(_norm(v) == _norm(it["expected_value"]) for v in got_vals)
                matched += ok
                results.append({"file_id": file_id, "filename": filename, "field": it["field"],
                                "expected": it["expected_value"], "got": got_vals, "ok": ok})
        with db.connect() as conn:
            conn.execute(
                "UPDATE golden_runs SET status = 'done', total = %s, matched = %s, results = %s::jsonb WHERE id = %s",
                (total, matched, json.dumps(results, ensure_ascii=False), run_id),
            )
    except Exception as e:
        with db.connect() as conn:
            conn.execute(
                "UPDATE golden_runs SET status = 'error', results = %s::jsonb WHERE id = %s",
                (json.dumps([{"error": str(e)}], ensure_ascii=False), run_id),
            )


def process_submission(sub_id: int, zip_path: Path, pack_id: int) -> None:
    api_key = load_api_key()
    cache = VLMCache(BASE / "cache/vlm", prompt_version=PROMPT_VERSION)
    run_dir = DATA / f"sub_{sub_id}"
    converted = run_dir / "converted"
    try:
        proc = subprocess.run(
            [sys.executable, "batch_convert.py", str(zip_path), str(converted), "4"],
            cwd=BASE, text=True, capture_output=True,
        )
        if proc.returncode:
            raise RuntimeError(f"변환 실패: {(proc.stdout + proc.stderr)[-500:]}")
        with db.connect() as conn:
            rules = pack_rules(conn, pack_id)
            doc_types = pack_doc_types(conn, pack_id)
        pdfs = sorted(p for p in converted.rglob("*.pdf"))
        others = sorted(p for p in converted.rglob("*") if p.is_file() and p.suffix != ".pdf" and p.name != "summary.json")
        for pdf in pdfs:
            with db.connect() as conn:
                file_id = conn.execute(
                    "INSERT INTO files (submission_id, filename, pdf_path) VALUES (%s, %s, %s) RETURNING id",
                    (sub_id, pdf.name, str(pdf)),
                ).fetchone()["id"]
            try:
                with db.connect() as conn:
                    detect_file(conn, file_id, pdf, rules, doc_types, cache, api_key)
            except Exception as e:  # 파일 하나 실패가 제출건 전체를 막지 않게
                with db.connect() as conn:
                    conn.execute("UPDATE files SET status = 'error', error = %s WHERE id = %s", (str(e)[:300], file_id))
        with db.connect() as conn:
            for f in others:
                conn.execute(
                    "INSERT INTO files (submission_id, filename, pdf_path, status) VALUES (%s, %s, %s, 'skipped')",
                    (sub_id, f.name, str(f)),
                )
            conn.execute("UPDATE submissions SET status = 'ready' WHERE id = %s", (sub_id,))
    except Exception as e:
        with db.connect() as conn:
            conn.execute("UPDATE submissions SET status = 'error', error = %s WHERE id = %s", (str(e)[:500], sub_id))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self.send_bytes(INDEX_HTML.encode(), "text/html; charset=utf-8")
            if path == "/api/bootstrap":
                with db.connect() as conn:
                    packs = conn.execute("SELECT id, slug, name FROM packs ORDER BY id").fetchall()
                    for p in packs:
                        p["rules"] = pack_rules(conn, p["id"])
                        p["doc_types"] = pack_doc_types(conn, p["id"])
                return self.send_json({"packs": packs})
            if path == "/api/submissions":
                with db.connect() as conn:
                    rows = conn.execute(
                        "SELECT s.*, "
                        " (SELECT count(*) FROM files f WHERE f.submission_id = s.id) AS file_count,"
                        " (SELECT count(*) FROM detections d JOIN files f ON f.id = d.file_id WHERE f.submission_id = s.id) AS detection_count,"
                        " (SELECT count(*) FROM golden_verdicts g WHERE g.submission_id = s.id) AS golden_count "
                        "FROM submissions s ORDER BY s.id DESC LIMIT 50"
                    ).fetchall()
                return self.send_json(rows)
            if path == "/api/golden_runs":
                with db.connect() as conn:
                    runs = conn.execute(
                        "SELECT id, pack_id, status, total, matched, created_at "
                        "FROM golden_runs ORDER BY id DESC LIMIT 20"
                    ).fetchall()
                return self.send_json(runs)
            if path.startswith("/api/golden_runs/"):
                with db.connect() as conn:
                    run = conn.execute(
                        "SELECT * FROM golden_runs WHERE id = %s", (int(path.rsplit("/", 1)[-1]),)
                    ).fetchone()
                return self.send_json(run or {"error": "not found"},
                                      HTTPStatus.OK if run else HTTPStatus.NOT_FOUND)
            if path.startswith("/api/submissions/"):
                return self.get_submission(int(path.rsplit("/", 1)[-1]))
            if path.startswith("/api/files/"):
                return self.get_file(int(path.rsplit("/", 1)[-1]))
            if path.startswith("/pageimg/"):
                return self.page_image(int(path.rsplit("/", 1)[-1]))
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as e:
            self.send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def get_submission(self, sub_id: int) -> None:
        with db.connect() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id = %s", (sub_id,)).fetchone()
            if not sub:
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            sub["files"] = conn.execute(
                "SELECT f.id, f.filename, f.doc_type, f.doc_type_registered, f.page_count, f.status, f.error,"
                " (SELECT count(*) FROM detections d WHERE d.file_id = f.id) AS detection_count,"
                " (SELECT count(*) FROM golden_verdicts g WHERE g.file_id = f.id) AS golden_count "
                "FROM files f WHERE f.submission_id = %s ORDER BY f.filename", (sub_id,),
            ).fetchall()
            doc_types = pack_doc_types(conn, sub["pack_id"])
            agg_rows = conn.execute(
                "SELECT d.field, COALESCE(NULLIF(d.corrected_value, ''), d.value) AS val, count(DISTINCT d.file_id) AS n "
                "FROM detections d JOIN files f ON f.id = d.file_id "
                "WHERE f.submission_id = %s AND d.feedback <> 'wrong' AND COALESCE(d.value, '') <> '' "
                "GROUP BY 1, 2 ORDER BY 1, 3 DESC", (sub_id,),
            ).fetchall()
        agg: dict[str, list] = {}
        for r in agg_rows:
            agg.setdefault(r["field"], []).append({"val": r["val"], "n": r["n"]})
        sub["aggregation"] = agg
        # 완비 체크리스트: VLM 판별 유형(주) + 파일명 힌트(보조) 로 유형별 존재 여부 확인
        active = [f for f in sub["files"] if f["status"] != "skipped"]
        matched_ids: set[int] = set()
        checklist = []
        for t in doc_types:
            hits = [f for f in active if f["doc_type"] == t["name"]
                    or any(h and h.lower() in f["filename"].lower() for h in t["filename_hints"])]
            matched_ids.update(f["id"] for f in hits)
            checklist.append({
                "name": t["name"], "required": t["required"], "present": bool(hits),
                "files": [f["filename"] for f in hits],
            })
        sub["checklist"] = checklist
        sub["unmatched_files"] = [f["filename"] for f in active if f["id"] not in matched_ids]
        self.send_json(sub)

    def get_file(self, file_id: int) -> None:
        with db.connect() as conn:
            f = conn.execute(
                "SELECT f.*, s.name AS submission_name, s.pack_id FROM files f "
                "JOIN submissions s ON s.id = f.submission_id WHERE f.id = %s", (file_id,),
            ).fetchone()
            if not f:
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            f["pages"] = conn.execute(
                "SELECT id, page_no, width, height FROM pages WHERE file_id = %s ORDER BY page_no", (file_id,)
            ).fetchall()
            f["detections"] = conn.execute(
                "SELECT id, page_no, rule_id, field, value, verdict, box, confidence, feedback, corrected_value "
                "FROM detections WHERE file_id = %s ORDER BY page_no, field", (file_id,)
            ).fetchall()
            f["golden"] = conn.execute(
                "SELECT field, expected_value, verdict, note FROM golden_verdicts WHERE file_id = %s", (file_id,)
            ).fetchall()
        self.send_json(f)

    def page_image(self, page_id: int) -> None:
        with db.connect() as conn:
            row = conn.execute("SELECT image_path FROM pages WHERE id = %s", (page_id,)).fetchone()
        path = Path(row["image_path"]) if row else None
        if not path or not path.is_file() or not path.is_relative_to(DATA):
            return self.send_error(HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/submissions":
                return self.create_submission()
            if path == "/api/rules":
                body = self.json_body()
                rule_type = body.get("rule_type") if body.get("rule_type") in ("extract", "verify") else "extract"
                with db.connect() as conn:
                    conn.execute(
                        "INSERT INTO rules (pack_id, doc_type, field, rule_type, instruction) VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (pack_id, doc_type, field) DO UPDATE SET "
                        "instruction = EXCLUDED.instruction, rule_type = EXCLUDED.rule_type",
                        (body["pack_id"], body["doc_type"].strip(), body["field"].strip(), rule_type,
                         (body.get("instruction") or "").strip()),
                    )
                return self.send_json({"ok": True})
            if path == "/api/doc_types":
                body = self.json_body()
                hints = [h.strip() for h in (body.get("filename_hints") or "").split(",") if h.strip()]
                with db.connect() as conn:
                    conn.execute(
                        "INSERT INTO doc_types (pack_id, name, required, filename_hints, description) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (pack_id, name) DO UPDATE SET "
                        "required = EXCLUDED.required, filename_hints = EXCLUDED.filename_hints, "
                        "description = EXCLUDED.description",
                        (body["pack_id"], body["name"].strip(), bool(body.get("required")), hints,
                         (body.get("description") or "").strip()),
                    )
                return self.send_json({"ok": True})
            if path.startswith("/api/files/") and path.endswith("/suggest_rules"):
                return self.suggest(int(path.split("/")[3]))
            if path == "/api/golden_runs":
                body = self.json_body()
                pack_id = int(body.get("pack_id") or 1)
                with db.connect() as conn:
                    n = conn.execute(
                        "SELECT count(*) AS n FROM golden_verdicts WHERE pack_id = %s AND field <> '_file'",
                        (pack_id,),
                    ).fetchone()["n"]
                    if not n:
                        return self.send_json({"error": "골든셋이 비어 있습니다. 파일 검토 화면에서 먼저 정답을 확정하세요."},
                                              HTTPStatus.BAD_REQUEST)
                    run_id = conn.execute(
                        "INSERT INTO golden_runs (pack_id) VALUES (%s) RETURNING id", (pack_id,)
                    ).fetchone()["id"]
                threading.Thread(target=run_golden, args=(run_id, pack_id), daemon=True).start()
                return self.send_json({"id": run_id})
            if path.startswith("/api/detections/") and path.endswith("/feedback"):
                det_id = int(path.split("/")[3])
                body = self.json_body()
                with db.connect() as conn:
                    conn.execute(
                        "UPDATE detections SET feedback = %s, corrected_value = %s WHERE id = %s",
                        (body.get("feedback") or "", body.get("corrected_value") or "", det_id),
                    )
                return self.send_json({"ok": True})
            if path.startswith("/api/files/") and path.endswith("/golden"):
                return self.save_golden(int(path.split("/")[3]))
            if path.startswith("/api/files/") and path.endswith("/redetect"):
                return self.redetect(int(path.split("/")[3]))
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/rules/"):
            rule_id = int(path.rsplit("/", 1)[-1])
            with db.connect() as conn:
                conn.execute("UPDATE detections SET rule_id = NULL WHERE rule_id = %s", (rule_id,))
                conn.execute("DELETE FROM rules WHERE id = %s", (rule_id,))
            return self.send_json({"ok": True})
        if path.startswith("/api/doc_types/"):
            dt_id = int(path.rsplit("/", 1)[-1])
            with db.connect() as conn:
                conn.execute("DELETE FROM doc_types WHERE id = %s", (dt_id,))
            return self.send_json({"ok": True})
        self.send_error(HTTPStatus.NOT_FOUND)

    def suggest(self, file_id: int) -> None:
        """이 파일에서 점검할 가치가 있는 규칙을 VLM 이 제안 (능동적 규칙 만들기)."""
        with db.connect() as conn:
            f = conn.execute(
                "SELECT f.pdf_path, f.doc_type, s.pack_id FROM files f "
                "JOIN submissions s ON s.id = f.submission_id WHERE f.id = %s", (file_id,),
            ).fetchone()
            if not f or not f["pdf_path"].endswith(".pdf"):
                return self.send_json({"error": "PDF 파일이 아닙니다"}, HTTPStatus.BAD_REQUEST)
            existing = pack_rules(conn, f["pack_id"])
        suggestions = suggest_rules(Path(f["pdf_path"]), f["doc_type"], existing, load_api_key())
        self.send_json({"pack_id": f["pack_id"], "suggestions": suggestions})

    def create_submission(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        parts = parse_multipart(self.headers, self.rfile.read(length))
        if "zip" not in parts or not parts["zip"][1]:
            raise ValueError("zip 파일이 없습니다")
        pack_id = int(parts.get("pack_id", ("", b"1"))[1] or b"1")
        filename = Path(parts["zip"][0] or "upload.zip").name
        with db.connect() as conn:
            sub_id = conn.execute(
                "INSERT INTO submissions (pack_id, name) VALUES (%s, %s) RETURNING id",
                (pack_id, filename.removesuffix(".zip")),
            ).fetchone()["id"]
        run_dir = DATA / f"sub_{sub_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        zip_path = run_dir / filename
        zip_path.write_bytes(parts["zip"][1])
        threading.Thread(target=process_submission, args=(sub_id, zip_path, pack_id), daemon=True).start()
        self.send_json({"id": sub_id})

    def save_golden(self, file_id: int) -> None:
        body = self.json_body()
        verdict = body.get("verdict") or "pass"
        note = body.get("note") or ""
        with db.connect() as conn:
            f = conn.execute("SELECT submission_id FROM files WHERE id = %s", (file_id,)).fetchone()
            sub = conn.execute("SELECT pack_id FROM submissions WHERE id = %s", (f["submission_id"],)).fetchone()
            items = body.get("items") or [{"field": "_file", "expected_value": "", "source_detection_id": None}]
            for it in items:
                conn.execute(
                    "INSERT INTO golden_verdicts (pack_id, submission_id, file_id, field, expected_value, verdict, source_detection_id, note) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (file_id, field) DO UPDATE SET expected_value = EXCLUDED.expected_value, "
                    "verdict = EXCLUDED.verdict, source_detection_id = EXCLUDED.source_detection_id, note = EXCLUDED.note",
                    (sub["pack_id"], f["submission_id"], file_id, it["field"], it.get("expected_value") or "",
                     verdict, it.get("source_detection_id"), note),
                )
        self.send_json({"ok": True})

    def redetect(self, file_id: int) -> None:
        with db.connect() as conn:
            f = conn.execute(
                "SELECT f.pdf_path, s.pack_id FROM files f JOIN submissions s ON s.id = f.submission_id WHERE f.id = %s",
                (file_id,),
            ).fetchone()
            conn.execute("UPDATE files SET status = 'pending' WHERE id = %s", (file_id,))
            rules = pack_rules(conn, f["pack_id"])
            doc_types = pack_doc_types(conn, f["pack_id"])

        def run() -> None:
            try:
                with db.connect() as conn:
                    detect_file(conn, file_id, Path(f["pdf_path"]), rules, doc_types,
                                VLMCache(BASE / "cache/vlm", prompt_version=PROMPT_VERSION), load_api_key())
            except Exception as e:
                with db.connect() as conn:
                    conn.execute("UPDATE files SET status = 'error', error = %s WHERE id = %s", (str(e)[:300], file_id))

        threading.Thread(target=run, daemon=True).start()
        self.send_json({"ok": True})

    def json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def send_json(self, data, status=HTTPStatus.OK) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8", status)

    def send_bytes(self, data: bytes, content_type: str, status=HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args) -> None:
        print(f"{self.address_string()!s} - {fmt % args!s}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    db.migrate()
    db.seed()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"simsa review: http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
