"""converted/ 전체 PDF 를 Gemini 로 일괄 적격심사 — 단계별 로그 + 스키마 검증 + 재시도.

단계(stage)마다 logs/screening_<시각>.jsonl 에 한 줄씩 기록한다:
  route → cache → api(시도별) → parse → validate → done
어느 단계에서 실패해도 침묵하지 않는다: 결과에 needs_review=True + error 로 남는다.

usage: python screen_batch.py <converted_dir> [workers=3] [--probe]
  --probe : 캐시를 우회한 2차 호출로 판정 일관성(결정성)을 표본 측정
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

from vlm_cache import VLMCache
from vlm_screen import PROMPT_VERSION, SCREENING_PROMPT, URL, load_api_key

BASE = Path(__file__).parent
MAX_ATTEMPTS = 3
VERDICTS = {"pass", "fail", "uncertain"}
SEAL_KINDS = {"자필서명", "도장(인감)", "법인직인", "전자서명", "없음"}


class StageLogger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.path = path

    def log(self, **fields) -> None:
        fields["ts"] = datetime.now().isoformat(timespec="milliseconds")
        with self._lock:
            self._f.write(json.dumps(fields, ensure_ascii=False) + "\n")
            self._f.flush()


def validate_result(r) -> list[str]:
    """스키마 위반 목록. 비어 있으면 유효."""
    problems = []
    if not isinstance(r, dict):
        return ["응답이 JSON 객체가 아님"]
    if r.get("verdict") not in VERDICTS:
        problems.append(f"verdict 이상: {r.get('verdict')!r}")
    c = r.get("confidence")
    if not isinstance(c, (int, float)) or not 0 <= c <= 1:
        problems.append(f"confidence 이상: {c!r}")
    s = r.get("signature_or_seal")
    if not isinstance(s, dict):
        problems.append("signature_or_seal 누락")
    else:
        if not isinstance(s.get("present"), bool):
            problems.append(f"present 가 bool 아님: {s.get('present')!r}")
        if s.get("kind") not in SEAL_KINDS:
            problems.append(f"kind 이상: {s.get('kind')!r}")
        if s.get("present") and not s.get("evidence"):
            problems.append("서명 있음인데 evidence 없음")
    if not r.get("doc_type"):
        problems.append("doc_type 누락")
    return problems


def call_gemini(pdf: Path, api_key: str, log, ctx: dict) -> dict:
    """재시도 포함 API 호출 + 파싱. 실패 시 예외."""
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {
                    "mime_type": "application/pdf",
                    "data": base64.b64encode(pdf.read_bytes()).decode(),
                }},
                {"text": SCREENING_PROMPT},
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            resp = requests.post(
                URL, json=body, timeout=180,
                headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
            )
            ms = int((time.monotonic() - t0) * 1000)
            log(stage="api", attempt=attempt, http=resp.status_code, ms=ms, **ctx)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 5 * attempt))
                last_err = f"HTTP {resp.status_code}"
                time.sleep(wait)
                continue
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            try:
                parsed = json.loads(text)
                # Gemini JSON 모드가 단일 객체를 배열로 감싸 반환하는 경우가 있음
                if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                    log(stage="parse", attempt=attempt, ok=True, unwrapped_list=True, **ctx)
                    parsed = parsed[0]
                return parsed
            except json.JSONDecodeError as e:
                log(stage="parse", attempt=attempt, ok=False, error=str(e)[:120], **ctx)
                last_err = f"JSON 파싱 실패: {e}"
                continue  # 재호출
        except requests.RequestException as e:
            ms = int((time.monotonic() - t0) * 1000)
            log(stage="api", attempt=attempt, http=None, ms=ms, error=str(e)[:120], **ctx)
            last_err = str(e)
            time.sleep(5 * attempt)
    raise RuntimeError(f"{MAX_ATTEMPTS}회 시도 모두 실패: {last_err}")


def screen_one(pdf: Path, submission: str, cache: VLMCache, api_key: str,
               logger: StageLogger, extra: str = "") -> dict:
    ctx = {"submission": submission, "file": pdf.name}
    if extra:
        ctx["probe"] = extra
    log = logger.log
    record = {"submission": submission, "file": pdf.name, "needs_review": False,
              "cache_hit": False, "error": ""}
    t0 = time.monotonic()

    key = cache.key_for(pdf, extra=extra)
    cached = cache.get(key)
    if cached is not None:
        # 캐시된 응답도 검증을 통과해야 사용 (과거 실행이 남긴 불량 응답 차단)
        poisoned = validate_result(cached)
        if poisoned:
            log(stage="cache", hit=True, poisoned=poisoned, **ctx)
            cached = None
        else:
            log(stage="cache", hit=True, **ctx)
    else:
        log(stage="cache", hit=False, **ctx)
    if cached is not None:
        record.update(result=cached, cache_hit=True)
    else:
        try:
            result = call_gemini(pdf, api_key, log, ctx)
        except RuntimeError as e:
            record.update(needs_review=True, error=str(e), result=None)
            log(stage="done", ok=False, error=str(e)[:200], **ctx)
            return record
        problems = validate_result(result)
        log(stage="validate", ok=not problems, problems=problems, **ctx)
        if problems:
            # 불량 응답은 캐시하지 않는다 — 다음 실행에서 재시도되도록
            record.update(needs_review=True, error="스키마 위반: " + "; ".join(problems))
        else:
            cache.set(key, result, source=pdf.name)
        record["result"] = result

    r = record.get("result")
    r = r if isinstance(r, dict) else {}
    log(stage="done", ok=not record["needs_review"],
        verdict=r.get("verdict"), confidence=r.get("confidence"),
        doc_type=r.get("doc_type"),
        signed=(r.get("signature_or_seal") or {}).get("present"),
        total_ms=int((time.monotonic() - t0) * 1000), **ctx)
    return record


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    probe = "--probe" in sys.argv
    if not args:
        print(__doc__)
        return 1
    root = Path(args[0])
    workers = int(args[1]) if len(args) > 1 else 3

    api_key = load_api_key()
    cache = VLMCache(BASE / "cache/vlm", prompt_version=PROMPT_VERSION)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logger = StageLogger(BASE / "logs" / f"screening_{stamp}.jsonl")

    jobs = []  # (pdf, submission)
    for sub_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in sorted(sub_dir.iterdir()):
            if f.suffix.lower() == ".pdf":
                jobs.append((f, sub_dir.name))
            else:
                logger.log(stage="route", submission=sub_dir.name, file=f.name,
                           skipped="pdf 아님 (원본 보존용)")
    print(f"대상 PDF {len(jobs)}개 / workers={workers} / log={logger.path.name}")

    def safe_one(j):
        pdf, sub = j
        try:
            return screen_one(pdf, sub, cache, api_key, logger)
        except Exception as e:  # 한 파일의 예외가 배치 전체를 죽이지 않게
            logger.log(stage="done", ok=False, submission=sub, file=pdf.name,
                       error=f"예외: {type(e).__name__}: {e}"[:200])
            return {"submission": sub, "file": pdf.name, "needs_review": True,
                    "cache_hit": False, "error": f"{type(e).__name__}: {e}", "result": None}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(safe_one, jobs))

    out = root / f"screening_results_{stamp}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in results if not r["needs_review"]]
    fails = [r for r in results if r["needs_review"]]
    verdicts = {}
    for r in ok:
        res = r.get("result")
        v = res.get("verdict") if isinstance(res, dict) else "invalid"
        verdicts[v] = verdicts.get(v, 0) + 1
    print(f"\n완료: {len(ok)}개 정상 / {len(fails)}개 확인필요(needs_review)")
    print(f"판정 분포: {verdicts}")
    print(f"캐시 히트: {sum(1 for r in results if r['cache_hit'])}개")
    for r in fails:
        print(f"  [REVIEW] {r['submission']} / {r['file']}: {r['error'][:150]}")
    print(f"결과: {out}")

    if probe:
        print("\n=== 일관성 프로브 (캐시 우회 재호출) ===")
        import random

        random.seed(42)
        sample = random.sample([r for r in ok], min(5, len(ok)))
        agree = 0
        for r in sample:
            pdf = root / r["submission"] / r["file"]
            second = screen_one(pdf, r["submission"], cache, api_key, logger, extra="probe2")
            r1, r2 = r["result"], second.get("result") or {}
            same = (r1.get("verdict") == r2.get("verdict")
                    and (r1.get("signature_or_seal") or {}).get("present")
                    == (r2.get("signature_or_seal") or {}).get("present"))
            agree += same
            mark = "일치" if same else "불일치!"
            print(f"  [{mark}] {r['file']}: {r1.get('verdict')}/{r2.get('verdict')}, "
                  f"signed {(r1.get('signature_or_seal') or {}).get('present')}"
                  f"/{(r2.get('signature_or_seal') or {}).get('present')}")
        print(f"일관성: {agree}/{len(sample)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
