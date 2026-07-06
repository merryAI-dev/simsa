"""simsa 심사 검토 서버 — 업로드 → VLM 탐지 → 빨간 박스 검토 → 골든셋 확정.

ZIP 업로드하면 batch_convert 로 PDF 정규화 후 파일마다 팩 규칙 기반 필드 탐지
(detect_fields)를 돌리고, 페이지 이미지 위 박스 오버레이로 검토한다.
사용자가 맞음/틀림 피드백을 주고 파일 판정을 확정하면 golden_verdicts 에 쌓인다.

usage: python review_app.py [--host 127.0.0.1] [--port 8766]
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

import db
from detect_fields import PROMPT_VERSION, check_submission, detect, render_pages, suggest_rules
from vlm_cache import VLMCache
from vlm_screen import load_api_key

BASE = Path(__file__).parent
DATA = BASE / "data/review"

UI_PATH = BASE / "review_ui.html"


def crop_detection(page_image_path: Path, box: list[int], out_path: Path) -> None:
    """AI 가 실제로 본 영역을 페이지 PNG 에서 잘라내 증거로 저장.
    box = [ymin, xmin, ymax, xmax], 0~1000 정규화. 증거가 잘리지 않게 바깥쪽으로 반올림."""
    img = Image.open(page_image_path)
    w, h = img.size
    ymin, xmin, ymax, xmax = box
    x1 = max(0, min(w - 1, math.floor(xmin / 1000 * w)))
    y1 = max(0, min(h - 1, math.floor(ymin / 1000 * h)))
    x2 = max(x1 + 1, min(w, math.ceil(xmax / 1000 * w)))
    y2 = max(y1 + 1, min(h, math.ceil(ymax / 1000 * h)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.crop((x1, y1, x2, y2)).save(out_path)


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


def pack_rules(conn, pack_id: int, scope: str | None = None) -> list[dict]:
    q = "SELECT id, doc_type, field, rule_type, scope, instruction FROM rules WHERE pack_id = %s"
    args: list = [pack_id]
    if scope:
        q += " AND scope = %s"
        args.append(scope)
    return conn.execute(q + " ORDER BY id", args).fetchall()


def pack_doc_types(conn, pack_id: int) -> list[dict]:
    return conn.execute(
        "SELECT id, name, required, filename_hints, description FROM doc_types WHERE pack_id = %s ORDER BY id",
        (pack_id,),
    ).fetchall()


def detect_file(conn, file_id: int, pdf: Path, rules: list[dict], doc_types: list[dict],
                cache: VLMCache, api_key: str) -> None:
    """파일 하나: 페이지 렌더(없으면) + 탐지 + DB 기록."""
    page_dir = DATA / f"file_{file_id}"
    timings: dict[str, list[float]] = {}
    have = conn.execute("SELECT count(*) AS n FROM pages WHERE file_id = %s", (file_id,)).fetchone()["n"]
    if not have:
        t0 = time.time()
        pages = render_pages(pdf, page_dir)
        timings["render"] = [t0, time.time()]
        for p in pages:
            conn.execute(
                "INSERT INTO pages (file_id, page_no, image_path, width, height) VALUES (%s, %s, %s, %s, %s)",
                (file_id, p["page_no"], p["image_path"], p["width"], p["height"]),
            )
        conn.execute("UPDATE files SET page_count = %s WHERE id = %s", (len(pages), file_id))
    t0 = time.time()
    result, _ = detect(pdf, rules, doc_types, cache, api_key)
    timings["detect"] = [t0, time.time()]
    conn.execute("DELETE FROM detections WHERE file_id = %s AND feedback = ''", (file_id,))
    # 피드백 있는 탐지는 보존되므로, 같은 항목이 다시 나오면 중복 삽입하지 않는다
    kept = {(r["field"], r["page_no"], r["value"]) for r in conn.execute(
        "SELECT field, page_no, value FROM detections WHERE file_id = %s", (file_id,)).fetchall()}
    result["detections"] = [
        d for d in result["detections"]
        if (d["field"], int(d.get("page") or 1), str(d.get("value") or "")) not in kept
    ]
    page_paths = {p["page_no"]: p["image_path"] for p in conn.execute(
        "SELECT page_no, image_path FROM pages WHERE file_id = %s", (file_id,)).fetchall()}
    crops_dir = DATA / f"file_{file_id}" / "crops"
    for d in result["detections"]:
        page_no = int(d.get("page") or 1)
        box = [int(v) for v in d["box_2d"]]
        value = str(d.get("value") or "")
        verdict = d.get("verdict") or ""
        confidence = float(d.get("confidence") or 0)
        det_id = conn.execute(
            "INSERT INTO detections (file_id, page_no, rule_id, field, value, verdict, box, confidence, "
            "model, prompt_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (file_id, page_no, d.get("rule_id"), d["field"], value, verdict, box, confidence,
             "gemini-flash-latest", PROMPT_VERSION),
        ).fetchone()["id"]
        crop_path = ""
        img_path = page_paths.get(page_no)
        if img_path:
            out = crops_dir / f"d{det_id}.png"
            try:
                crop_detection(Path(img_path), box, out)  # 크롭 파일을 먼저 쓰고, 트랜잭션은 이 함수 종료 시 커밋됨
                crop_path = str(out)
            except Exception as e:  # 크롭 실패해도 탐지 자체는 유효 — 크롭 없이 진행
                print(f"[file {file_id}] detection {det_id} 크롭 실패: {e}")
        if crop_path:
            conn.execute("UPDATE detections SET crop_path = %s WHERE id = %s", (crop_path, det_id))
        conn.execute(
            "INSERT INTO detection_events (submission_id, file_id, detection_id, event_type, page_no, "
            " rule_id, field, value, verdict, box, crop_path, confidence, model, prompt_version) "
            "SELECT f.submission_id, %s, %s, 'detected', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
            "FROM files f WHERE f.id = %s",
            (file_id, det_id, page_no, d.get("rule_id"), d["field"], value, verdict, box, crop_path,
             confidence, "gemini-flash-latest", PROMPT_VERSION, file_id),
        )
    conn.execute(
        "UPDATE files SET doc_type = %s, doc_type_registered = %s, doc_type_evidence = %s, "
        "status = 'detected', error = '', timings = timings || %s::jsonb WHERE id = %s",
        (result.get("doc_type") or "", bool(result.get("doc_type_registered")),
         result.get("doc_type_evidence") or "", json.dumps(timings), file_id),
    )


def _norm(s) -> str:
    return "".join(str(s or "").split())


def submission_detections(conn, sub_id: int) -> list[dict]:
    """제출건의 유효 추출값 (틀림 제외, 정정 값 우선)."""
    return conn.execute(
        "SELECT f.filename, f.doc_type, d.field, d.verdict, "
        " COALESCE(NULLIF(d.corrected_value, ''), d.value) AS value "
        "FROM detections d JOIN files f ON f.id = d.file_id "
        "WHERE f.submission_id = %s AND d.feedback <> 'wrong' "
        "ORDER BY f.filename, d.field", (sub_id,),
    ).fetchall()


def build_snapshot(conn, sub_id: int, pack_id: int) -> str:
    """종합 검사 입력: 완비 현황 + 파일별 추출값을 텍스트로 요약 (PDF 재호출 없음)."""
    files = conn.execute(
        "SELECT id, filename, doc_type, status FROM files WHERE submission_id = %s ORDER BY filename", (sub_id,)
    ).fetchall()
    doc_types = pack_doc_types(conn, pack_id)
    lines = ["## 서류 완비 현황"]
    for t in doc_types:
        hits = [f for f in files if f["status"] != "skipped" and (f["doc_type"] == t["name"]
                or any(h and h.lower() in f["filename"].lower() for h in t["filename_hints"]))]
        req = "필수" if t["required"] else "선택"
        lines.append(f"- [{req}] {t['name']}: " + (", ".join(f["filename"] for f in hits) if hits else "없음"))
    lines.append("\n## 파일별 추출값")
    by_file: dict[str, list[dict]] = {}
    for d in submission_detections(conn, sub_id):
        by_file.setdefault(f"{d['filename']} ({d['doc_type']})", []).append(d)
    for header, dets in by_file.items():
        lines.append(f"### {header}")
        for d in dets:
            v = f" [판정: {d['verdict']}]" if d["verdict"] else ""
            lines.append(f"- {d['field']}: {d['value']}{v}")
    return "\n".join(lines)


def run_submission_checks(sub_id: int, pack_id: int) -> None:
    """종합 규칙(scope=submission)을 pro 텍스트 평가로 실행하고 결과를 저장."""
    t0 = time.time()
    with db.connect() as conn:
        rules = pack_rules(conn, pack_id, scope="submission")
        if not rules:
            return
        base_date = conn.execute(
            "SELECT base_date FROM submissions WHERE id = %s", (sub_id,)
        ).fetchone()["base_date"]
        snapshot = build_snapshot(conn, sub_id, pack_id)
    checks = check_submission(snapshot, rules, load_api_key(), base_date.isoformat())
    by_id = {r["id"]: r for r in rules}
    with db.connect() as conn:
        conn.execute("DELETE FROM submission_checks WHERE submission_id = %s AND feedback = ''", (sub_id,))
        for c in checks:
            conn.execute(
                "INSERT INTO submission_checks (submission_id, rule_id, name, verdict, evidence, refs) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                (sub_id, c["rule_id"], by_id[c["rule_id"]]["field"], c["verdict"], c["evidence"],
                 json.dumps(c["refs"], ensure_ascii=False)),
            )
        conn.execute("UPDATE submissions SET timings = timings || %s::jsonb WHERE id = %s",
                     (json.dumps({"checks": [t0, time.time()]}), sub_id))


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
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, "batch_convert.py", str(zip_path), str(converted), "4"],
            cwd=BASE, text=True, capture_output=True,
        )
        with db.connect() as conn:
            conn.execute("UPDATE submissions SET timings = timings || %s::jsonb WHERE id = %s",
                         (json.dumps({"convert": [t0, time.time()]}), sub_id))
        if proc.returncode:
            raise RuntimeError(f"변환 실패: {(proc.stdout + proc.stderr)[-500:]}")
        with db.connect() as conn:
            rules = pack_rules(conn, pack_id, scope="file")
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
        try:
            run_submission_checks(sub_id, pack_id)  # 추출 완료 후 종합 검사 자동 실행
        except Exception as e:  # 종합 검사 실패는 UI 에서 재실행 가능 — 제출건 상태는 유지
            print(f"[sub {sub_id}] 종합 검사 자동 실행 실패: {e}")
    except Exception as e:
        with db.connect() as conn:
            conn.execute("UPDATE submissions SET status = 'error', error = %s WHERE id = %s", (str(e)[:500], sub_id))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self.send_bytes(UI_PATH.read_text(encoding="utf-8").encode(), "text/html; charset=utf-8")
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
            if path.startswith("/api/submissions/") and path.endswith("/audit"):
                return self.get_audit(int(path.split("/")[3]))
            if path.startswith("/api/submissions/"):
                return self.get_submission(int(path.rsplit("/", 1)[-1]))
            if path.startswith("/api/files/"):
                return self.get_file(int(path.rsplit("/", 1)[-1]))
            if path.startswith("/pageimg/"):
                with db.connect() as conn:
                    row = conn.execute("SELECT image_path FROM pages WHERE id = %s",
                                       (int(path.rsplit("/", 1)[-1]),)).fetchone()
                return self.serve_image(row and row["image_path"])
            if path.startswith("/cropimg/"):
                with db.connect() as conn:
                    row = conn.execute("SELECT crop_path FROM detections WHERE id = %s",
                                       (int(path.rsplit("/", 1)[-1]),)).fetchone()
                return self.serve_image(row and row["crop_path"])
            if path.startswith("/eventimg/"):
                with db.connect() as conn:
                    row = conn.execute("SELECT crop_path FROM detection_events WHERE id = %s",
                                       (int(path.rsplit("/", 1)[-1]),)).fetchone()
                return self.serve_image(row and row["crop_path"])
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as e:
            self.send_json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def get_submission(self, sub_id: int) -> None:
        with db.connect() as conn:
            sub = conn.execute("SELECT * FROM submissions WHERE id = %s", (sub_id,)).fetchone()
            if not sub:
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            sub["files"] = conn.execute(
                "SELECT f.id, f.filename, f.doc_type, f.doc_type_registered, f.page_count, f.status, f.error, f.timings,"
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
        with db.connect() as conn:
            sub["checks"] = conn.execute(
                "SELECT id, rule_id, name, verdict, evidence, refs, feedback "
                "FROM submission_checks WHERE submission_id = %s ORDER BY id", (sub_id,),
            ).fetchall()
            # 라이브 탐지 피드: DAG 대신 "지금 무엇을 보고 있는가"를 보여준다.
            # 파일은 순차 처리되므로 status='pending' 인 파일이 곧 지금 처리 중인 파일.
            current = next((f for f in sub["files"] if f["status"] == "pending"), None)
            if current:
                sub["current_step"] = f"지금 탐지 중: {current['filename']}"
            elif sub["status"] == "processing":
                # 파일 사이 짧은 틈(방금 끝난 파일 커밋 ~ 다음 파일 INSERT)일 수 있음
                sub["current_step"] = "다음 파일 준비 중…" if sub["timings"].get("convert") else "문서 변환 중…"
            else:
                sub["current_step"] = None
            feed = []
            for f in sub["files"]:
                if f["status"] not in ("detected", "error"):
                    continue
                prow = conn.execute(
                    "SELECT page_no FROM detections WHERE file_id = %s "
                    "GROUP BY page_no ORDER BY count(*) DESC, page_no LIMIT 1", (f["id"],),
                ).fetchone()
                page_no = prow["page_no"] if prow else 1
                page = conn.execute(
                    "SELECT id, page_no, width, height FROM pages WHERE file_id = %s AND page_no = %s",
                    (f["id"], page_no),
                ).fetchone()
                dets = conn.execute(
                    "SELECT id, field, value, verdict, confidence, feedback, corrected_value, box, crop_path "
                    "FROM detections WHERE file_id = %s AND page_no = %s ORDER BY field",
                    (f["id"], page_no),
                ).fetchall() if page else []
                feed.append({"file_id": f["id"], "filename": f["filename"], "doc_type": f["doc_type"],
                            "status": f["status"], "error": f["error"], "page": page, "detections": dets})
            sub["feed"] = feed
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
                "SELECT id, page_no, rule_id, field, value, verdict, box, confidence, feedback, corrected_value, "
                "crop_path FROM detections WHERE file_id = %s ORDER BY page_no, field", (file_id,)
            ).fetchall()
            f["golden"] = conn.execute(
                "SELECT field, expected_value, verdict, note FROM golden_verdicts WHERE file_id = %s", (file_id,)
            ).fetchall()
        self.send_json(f)

    def serve_image(self, path_str: str | None) -> None:
        path = Path(path_str) if path_str else None
        if not path or not path.is_file() or not path.is_relative_to(DATA):
            return self.send_error(HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with open(path, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def get_audit(self, sub_id: int) -> None:
        """시간순 append-only 증거 로그 — AI 가 실제로 본 것 + 사람 확정 이력."""
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT e.*, f.filename FROM detection_events e JOIN files f ON f.id = e.file_id "
                "WHERE e.submission_id = %s ORDER BY e.created_at, e.id", (sub_id,),
            ).fetchall()
        self.send_json(rows)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/submissions":
                return self.create_submission()
            if path == "/api/rules":
                body = self.json_body()
                rule_type = body.get("rule_type") if body.get("rule_type") in ("extract", "verify") else "extract"
                scope = body.get("scope") if body.get("scope") in ("file", "submission") else "file"
                with db.connect() as conn:
                    conn.execute(
                        "INSERT INTO rules (pack_id, doc_type, field, rule_type, scope, instruction) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (pack_id, doc_type, field) DO UPDATE SET "
                        "instruction = EXCLUDED.instruction, rule_type = EXCLUDED.rule_type, scope = EXCLUDED.scope",
                        (body["pack_id"], body["doc_type"].strip(), body["field"].strip(), rule_type, scope,
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
            if path.startswith("/api/submissions/") and path.endswith("/check"):
                sub_id = int(path.split("/")[3])
                with db.connect() as conn:
                    sub = conn.execute("SELECT pack_id FROM submissions WHERE id = %s", (sub_id,)).fetchone()
                threading.Thread(target=run_submission_checks, args=(sub_id, sub["pack_id"]), daemon=True).start()
                return self.send_json({"ok": True})
            if path.startswith("/api/checks/") and path.endswith("/feedback"):
                check_id = int(path.split("/")[3])
                body = self.json_body()
                with db.connect() as conn:
                    conn.execute("UPDATE submission_checks SET feedback = %s WHERE id = %s",
                                 (body.get("feedback") or "", check_id))
                return self.send_json({"ok": True})
            if path.startswith("/api/doc_types/") and path.endswith("/toggle"):
                dt_id = int(path.split("/")[3])
                with db.connect() as conn:
                    conn.execute("UPDATE doc_types SET required = NOT required WHERE id = %s", (dt_id,))
                return self.send_json({"ok": True})
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
                    d = conn.execute("SELECT * FROM detections WHERE id = %s", (det_id,)).fetchone()
                    f = conn.execute("SELECT submission_id FROM files WHERE id = %s", (d["file_id"],)).fetchone()
                    # 감사 로그는 append-only: 맞음→틀림→정정 순서로 여러 번 눌러도 매번 새 이벤트로 쌓인다
                    conn.execute(
                        "INSERT INTO detection_events (submission_id, file_id, detection_id, event_type, "
                        " page_no, rule_id, field, value, verdict, box, crop_path, confidence, model, "
                        " prompt_version, feedback, corrected_value) "
                        "VALUES (%s, %s, %s, 'feedback', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (f["submission_id"], d["file_id"], det_id, d["page_no"], d["rule_id"], d["field"],
                         d["value"], d["verdict"], d["box"], d["crop_path"], d["confidence"], d["model"],
                         d["prompt_version"], d["feedback"], d["corrected_value"]),
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
        base_date = (parts.get("base_date", ("", b""))[1] or b"").decode().strip() or None
        filename = Path(parts["zip"][0] or "upload.zip").name
        with db.connect() as conn:
            sub_id = conn.execute(
                "INSERT INTO submissions (pack_id, name, base_date) "
                "VALUES (%s, %s, COALESCE(%s::date, CURRENT_DATE)) RETURNING id",
                (pack_id, filename.removesuffix(".zip"), base_date),
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
            rules = pack_rules(conn, f["pack_id"], scope="file")
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
