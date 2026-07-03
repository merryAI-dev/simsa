from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from converter import CHROME_CANDIDATES, SOFFICE

BASE = Path(__file__).parent
RUNS = BASE / "runs"
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()

INDEX_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CTS 사전적격심사</title><style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f6f7f9;color:#17202a}
.wrap{max-width:1120px;margin:0 auto;padding:20px}header{background:white;border-bottom:1px solid #d9dee7}
main{display:grid;grid-template-columns:420px 1fr;gap:18px}.card{background:white;border:1px solid #d9dee7;border-radius:8px;padding:18px}
h1{font-size:24px;margin:0}h2{font-size:16px;margin:0 0 14px}label{display:block;margin:14px 0 7px;font-weight:700;font-size:13px}
input,select,button{width:100%;min-height:40px;border-radius:6px;font-size:14px}input,select{border:1px solid #d9dee7;padding:8px}
button{margin-top:16px;border:0;background:#146c5f;color:white;font-weight:700;cursor:pointer}button:disabled{background:#98a2b3}
.hint{color:#667085;font-size:12px;line-height:1.5}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}
.metric{border:1px solid #d9dee7;border-radius:8px;padding:12px;background:#fbfcfd}.metric span{display:block;color:#667085;font-size:12px}.metric strong{display:block;margin-top:6px;font-size:20px}
.phases{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}.phase{border:1px solid #d9dee7;border-radius:8px;padding:11px;background:#fbfcfd;font-size:13px}
.done{color:#087443}.running{color:#9a3412}.error{color:#b42318}.downloads{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.downloads a{border:1px solid #d9dee7;border-radius:6px;padding:8px 10px;color:#17202a;text-decoration:none;background:white;font-size:13px}
pre{min-height:320px;max-height:520px;overflow:auto;background:#111827;color:#e5e7eb;border-radius:8px;padding:14px;font-size:12px;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}td,th{border-bottom:1px solid #d9dee7;padding:8px;text-align:left}
@media(max-width:860px){main{grid-template-columns:1fr}.metrics,.phases{grid-template-columns:1fr}}
</style></head><body><header><div class="wrap"><h1>CTS 사전적격심사</h1></div></header>
<main class="wrap"><section class="card"><h2>업로드</h2><form id="form">
<label>제출 ZIP</label><input name="zip" type="file" accept=".zip" required>
<label>심사표</label><select name="sheet"><option value="SEED1_2">Seed 1/2</option><option value="CTS_TIPS">CTS-TIPS</option></select>
<p class="hint">서버 재시작 복구용 최소 UI입니다. 기존 배치 스크립트와 캐시를 그대로 씁니다.</p><button id="submit">심사 시작</button></form>
</section><section class="card"><h2>결과</h2><div class="phases" id="phases"></div>
<div class="metrics"><div class="metric"><span>1차 결과</span><strong id="final">-</strong></div><div class="metric"><span>가산점</span><strong id="bonus">-</strong></div><div class="metric"><span>외부 확인</span><strong id="pending">-</strong></div></div>
<div class="downloads" id="downloads"></div><p class="hint" id="health">환경 확인 중...</p><pre id="logs">작업 로그가 여기에 표시됩니다.</pre>
<table id="manifest" hidden><thead><tr><th>제출건</th><th>상태</th><th>메모</th></tr></thead><tbody></tbody></table></section></main>
<script>
const form=document.getElementById('form'),submit=document.getElementById('submit'),logs=document.getElementById('logs');
const finalEl=document.getElementById('final'),bonusEl=document.getElementById('bonus'),pendingEl=document.getElementById('pending');
const downloads=document.getElementById('downloads'),phasesEl=document.getElementById('phases'),healthEl=document.getElementById('health');
const table=document.getElementById('manifest'),tbody=table.querySelector('tbody');let timer=null;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(job){logs.textContent=(job.logs||[]).join('\\n')||'-';finalEl.textContent=job.final_result||'-';bonusEl.textContent=job.bonus_points_total??'-';pendingEl.textContent=(job.pending_external_checks||[]).length||'-';
phasesEl.innerHTML=(job.phases||[]).map(p=>`<div class="phase ${esc(p.status)}"><b>${esc(p.label)}</b><br>${esc(p.status)}</div>`).join('');
downloads.innerHTML='';Object.entries(job.downloads||{}).forEach(([k,v])=>{const a=document.createElement('a');a.href=v;a.textContent=k.toUpperCase();downloads.appendChild(a)});
tbody.innerHTML='';const rows=job.files||job.batch_results||[];table.hidden=!rows.length;rows.forEach(r=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${esc(r.submission||r.file||r.name)}</td><td>${esc(r.status||r.final_result||'-')}</td><td>${esc(r.error||r.detail||r.pdf||'')}</td>`;tbody.appendChild(tr)});
if(job.status==='done'||job.status==='error'){clearInterval(timer);submit.disabled=false}}
async function poll(id){const r=await fetch(`/api/jobs/${id}`);render(await r.json())}
form.addEventListener('submit',async e=>{e.preventDefault();submit.disabled=true;logs.textContent='업로드 중...';downloads.innerHTML='';const r=await fetch('/api/jobs',{method:'POST',body:new FormData(form)});if(!r.ok){logs.textContent=await r.text();submit.disabled=false;return}const j=await r.json();await poll(j.id);timer=setInterval(()=>poll(j.id),2000)});
fetch('/api/health').then(r=>r.json()).then(d=>{healthEl.textContent=`Gemini ${d.gemini_api_key?'OK':'키 없음'} · soffice ${d.soffice||'없음'} · H2Orestart ${d.h2orestart?'OK':'확인필요'} · hwp5 ${d.hwp5_pdf_conversion?'OK':'제한'} · 기준DB ${d.reference_data?'OK':'없음'} · py-deps ${(d.python_deps_missing||[]).length?('누락: '+d.python_deps_missing.join(',')):'OK'}`});
</script></body></html>"""


def job_update(job_id: str, **fields) -> None:
    with LOCK:
        JOBS[job_id].update(fields)


def job_log(job_id: str, line: str) -> None:
    with LOCK:
        logs = JOBS[job_id].setdefault("logs", [])
        logs.append(line)
        del logs[:-300]


def run_cmd(job_id: str, args: list[str], cwd: Path = BASE) -> str:
    job_log(job_id, "$ " + " ".join(args))
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    output = (proc.stdout + proc.stderr).strip()
    if output:
        for line in output.splitlines()[-80:]:
            job_log(job_id, line)
    if proc.returncode:
        raise RuntimeError(f"{args[1]} 실패(rc={proc.returncode})")
    return output


def phase(job_id: str, label: str, status: str) -> None:
    with LOCK:
        phases = JOBS[job_id].setdefault("phases", [])
        for p in phases:
            if p["label"] == label:
                p["status"] = status
                break
        else:
            phases.append({"label": label, "status": status})


def run_job(job_id: str) -> None:
    job = JOBS[job_id]
    run_dir = Path(job["run_dir"])
    results = run_dir / "results"
    converted = run_dir / "converted"
    results.mkdir(exist_ok=True)
    try:
        job_update(job_id, status="running")
        phase(job_id, "업로드·압축 해제", "done")

        phase(job_id, "문서 변환", "running")
        run_cmd(job_id, [sys.executable, "batch_convert.py", job["zip_path"], str(converted), "4"])
        phase(job_id, "문서 변환", "done")

        summary_path = converted / "summary.json"
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            batch = [dict(r, submission=sub) for sub, rows in summary.items() for r in rows]
            job_update(job_id, batch_results=batch)

        phase(job_id, "VLM 분석", "running")
        run_cmd(job_id, [sys.executable, "screen_batch.py", str(converted), "3"])
        screening = newest(converted, "screening_results_*.json")
        phase(job_id, "VLM 분석", "done")

        phase(job_id, "교차검증·엑셀", "running")
        run_cmd(job_id, [sys.executable, "cross_check.py", str(screening)])
        xcheck = newest(converted, "cross_check_*.json")
        xlsx = results / f"CTS_사전적격심사표_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        run_cmd(job_id, [sys.executable, "excel_report.py", str(screening), str(xcheck), str(xlsx), str(summary_path)])
        shutil.copy2(screening, results / screening.name)
        shutil.copy2(xcheck, results / xcheck.name)
        if summary_path.exists():
            shutil.copy2(summary_path, results / "conversion_summary.json")
        archive = shutil.make_archive(str(results / "all_results"), "zip", results)
        phase(job_id, "교차검증·엑셀", "done")

        files, pending = summarize(screening, xcheck)
        downloads = {
            "xlsx": f"/downloads/{job_id}/{xlsx.name}",
            "json": f"/downloads/{job_id}/{screening.name}",
            "zip": f"/downloads/{job_id}/{Path(archive).name}",
        }
        job_update(
            job_id,
            status="done",
            final_result=final_status(files, pending),
            bonus_points_total=0,
            pending_external_checks=pending,
            files=files,
            downloads=downloads,
        )
        job_log(job_id, f"완료: {xlsx.name}")
    except Exception as e:
        phase(job_id, "오류", "error")
        (run_dir / "error.txt").write_text(str(e), encoding="utf-8")
        job_update(job_id, status="error", error=str(e))
        job_log(job_id, "ERROR: " + str(e))


def newest(root: Path, pattern: str) -> Path:
    hits = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not hits:
        raise FileNotFoundError(f"{pattern} 없음")
    return hits[-1]


def summarize(screening: Path, xcheck: Path) -> tuple[list[dict], list[dict]]:
    rows = json.loads(screening.read_text(encoding="utf-8"))
    files = [
        {
            "submission": r.get("submission"),
            "file": r.get("file"),
            "status": "review" if r.get("needs_review") else (r.get("overall") or {}).get("verdict", "done"),
            "error": r.get("error", ""),
        }
        for r in rows
    ]
    pending = []
    for r in json.loads(xcheck.read_text(encoding="utf-8")):
        if r.get("missing_required_docs"):
            pending.append({"submission": r.get("submission"), "type": "missing_required_docs"})
        if r.get("overall") in ("issues_found", "uncertain"):
            pending.append({"submission": r.get("submission"), "type": "review"})
    return files, pending


def final_status(files: list[dict], pending: list[dict]) -> str:
    if not files:
        return "보완중"
    if any(f.get("status") == "fail" for f in files):
        return "Fail"
    if pending or any(f.get("status") in ("review", "uncertain") for f in files):
        return "보완중"
    return "Pass"


def parse_upload(headers, body: bytes) -> tuple[str, bytes]:
    content_type = headers.get("Content-Type", "")
    msg = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "zip":
            name = part.get_filename() or "upload.zip"
            return Path(name).name, part.get_payload(decode=True)
    raise ValueError("zip 파일이 없습니다")


def health() -> dict:
    env = (BASE / ".env").read_text(encoding="utf-8") if (BASE / ".env").exists() else ""
    missing = [m for m in ("requests", "openpyxl") if importlib.util.find_spec(m) is None]
    return {
        "gemini_api_key": bool(os.environ.get("GEMINI_API_KEY") or "GEMINI_API_KEY=" in env),
        "soffice": SOFFICE if Path(SOFFICE).exists() else None,
        "chrome": next((c for c in CHROME_CANDIDATES if Path(c).exists()), None),
        "h2orestart": (Path.home() / "Library/Application Support/LibreOffice/4").exists(),
        "hwpx_pdf_conversion": Path(SOFFICE).exists(),
        "hwp5_pdf_conversion": shutil.which("hwp5html") is not None,
        "reference_data": str(BASE / "data/reference_data.json") if (BASE / "data/reference_data.json").exists() else None,
        "python_deps_missing": missing,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_bytes(INDEX_HTML.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self.send_json(health())
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            self.send_json(job or {"error": "not found"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            return
        if path.startswith("/downloads/"):
            self.download(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/jobs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            filename, data = parse_upload(self.headers, self.rfile.read(length))
            job_id = uuid.uuid4().hex[:12]
            run_dir = RUNS / job_id
            run_dir.mkdir(parents=True, exist_ok=True)
            zip_path = run_dir / filename
            zip_path.write_bytes(data)
            with LOCK:
                JOBS[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "run_dir": str(run_dir),
                    "zip_path": str(zip_path),
                    "logs": ["업로드 완료: " + filename],
                    "phases": [
                        {"label": "업로드·압축 해제", "status": "running"},
                        {"label": "문서 변환", "status": "pending"},
                        {"label": "VLM 분석", "status": "pending"},
                        {"label": "교차검증·엑셀", "status": "pending"},
                    ],
                }
            threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
            self.send_json({"id": job_id})
        except Exception as e:
            self.send_bytes(str(e).encode(), "text/plain; charset=utf-8", HTTPStatus.BAD_REQUEST)

    def download(self, path: str) -> None:
        _, _, job_id, name = path.split("/", 3)
        file_path = RUNS / job_id / "results" / unquote(name)
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{html.escape(file_path.name)}"')
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()
        with open(file_path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def send_json(self, data, status=HTTPStatus.OK) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

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
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    RUNS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CTS screening web: http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
