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
from vlm_screen import (MODEL, PROMPT_VERSION, SCREENING_PROMPT, cache_version,
                        load_api_key, url_for)

BASE = Path(__file__).parent
ESCALATION_MODEL = "gemini-pro-latest"  # uncertain/불명확/저확신 건만 상위 모델 재심사
ESCALATION_CONF = 0.7
MAX_ATTEMPTS = 3
VERDICTS = {"pass", "fail", "uncertain"}
SEAL_KINDS = {"자필서명", "도장(인감)", "법인직인", "전자서명", "불명확", "없음"}
SEAL_OWNERS = {"제출자", "발급기관", "불명"}
VERDICT_RANK = {"fail": 0, "uncertain": 1, "pass": 2}  # 종합판정 = 최악값


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
    """스키마 위반 목록. 비어 있으면 유효. (v2: documents 배열)"""
    if not isinstance(r, dict):
        return ["응답이 JSON 객체가 아님"]
    docs = r.get("documents")
    if not isinstance(docs, list) or not docs:
        return ["documents 배열 누락 또는 빈 배열"]
    problems = []
    for i, d in enumerate(docs):
        tag = f"documents[{i}]"
        if not isinstance(d, dict):
            problems.append(f"{tag} 가 객체 아님")
            continue
        if d.get("verdict") not in VERDICTS:
            problems.append(f"{tag}.verdict 이상: {d.get('verdict')!r}")
        c = d.get("confidence")
        if not isinstance(c, (int, float)) or not 0 <= c <= 1:
            problems.append(f"{tag}.confidence 이상: {c!r}")
        s = d.get("signature_or_seal")
        if not isinstance(s, dict):
            problems.append(f"{tag}.signature_or_seal 누락")
        else:
            if not isinstance(s.get("present"), bool):
                problems.append(f"{tag}.present 가 bool 아님: {s.get('present')!r}")
            if s.get("kind") not in SEAL_KINDS:
                problems.append(f"{tag}.kind 이상: {s.get('kind')!r}")
            if s.get("kind") not in ("없음", None) and s.get("seal_owner") not in SEAL_OWNERS:
                problems.append(f"{tag}.seal_owner 이상: {s.get('seal_owner')!r}")
            if s.get("present") and not s.get("evidence"):
                problems.append(f"{tag} 서명 있음인데 evidence 없음")
        if not d.get("doc_type"):
            problems.append(f"{tag}.doc_type 누락")
        comp = d.get("completeness")
        if comp is not None:
            if not isinstance(comp, dict) or not isinstance(comp.get("complete"), bool):
                problems.append(f"{tag}.completeness 형식 이상")
        ext = d.get("extracted")
        if ext is not None and not isinstance(ext, dict):
            problems.append(f"{tag}.extracted 형식 이상")
    return problems


def overall(r: dict) -> dict:
    """documents 배열의 종합 뷰 (판정은 최악값, 서명은 제출자 서명 존재 여부)."""
    docs = r.get("documents") or []
    docs = [d for d in docs if isinstance(d, dict)]
    if not docs:
        return {}
    worst = min(docs, key=lambda d: VERDICT_RANK.get(d.get("verdict"), 1))
    signed = any(
        (d.get("signature_or_seal") or {}).get("present")
        and (d.get("signature_or_seal") or {}).get("seal_owner") == "제출자"
        for d in docs)
    return {"verdict": worst.get("verdict"),
            "confidence": min((d.get("confidence") or 0) for d in docs),
            "doc_type": " + ".join(str(d.get("doc_type")) for d in docs),
            "submitter_signed": signed, "n_docs": len(docs)}


def call_gemini(pdf: Path, api_key: str, log, ctx: dict, model: str = MODEL) -> dict:
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
                url_for(model), json=body, timeout=300,  # 대형 PDF(30p 제안서) 대비
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
               logger: StageLogger, extra: str = "", model: str = MODEL) -> dict:
    ctx = {"submission": submission, "file": pdf.name}
    if model != MODEL:
        ctx["model"] = model
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
            result = call_gemini(pdf, api_key, log, ctx, model=model)
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
    ov = overall(r) if isinstance(r, dict) else {}
    record["overall"] = ov
    log(stage="done", ok=not record["needs_review"],
        verdict=ov.get("verdict"), confidence=ov.get("confidence"),
        doc_type=ov.get("doc_type"), signed=ov.get("submitter_signed"),
        n_docs=ov.get("n_docs"),
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
    cache = VLMCache(BASE / "cache/vlm", prompt_version=cache_version())
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

    # ── 에스컬레이션: uncertain / 불명확 / 저확신 / 스키마실패 건만 상위 모델 재심사 ──
    def wants_escalation(r: dict) -> bool:
        if r["needs_review"]:
            return True
        ov = r.get("overall") or {}
        if ov.get("verdict") == "uncertain":
            return True
        if (ov.get("confidence") or 1.0) < ESCALATION_CONF:
            return True
        docs = (r.get("result") or {}).get("documents") or []
        return any(isinstance(d, dict)
                   and (d.get("signature_or_seal") or {}).get("kind") == "불명확"
                   for d in docs)

    cands = [(i, r) for i, r in enumerate(results) if wants_escalation(r)]
    if cands:
        print(f"\n에스컬레이션 {len(cands)}건 → {ESCALATION_MODEL}")
        pro_cache = VLMCache(BASE / "cache/vlm",
                             prompt_version=cache_version(ESCALATION_MODEL))

        def escalate(item):
            i, r = item
            pdf = root / r["submission"] / r["file"]
            try:
                return i, screen_one(pdf, r["submission"], pro_cache, api_key,
                                     logger, model=ESCALATION_MODEL)
            except Exception as e:
                logger.log(stage="done", ok=False, submission=r["submission"],
                           file=r["file"], model=ESCALATION_MODEL,
                           error=f"에스컬레이션 예외: {e}"[:200])
                return i, None

        with ThreadPoolExecutor(max_workers=min(workers, 2)) as pool:
            for i, esc in pool.map(escalate, cands):
                first = results[i]
                if esc is None or esc["needs_review"]:
                    first["tier"] = "flash (에스컬레이션 실패)"
                    continue
                v1 = (first.get("overall") or {}).get("verdict")
                v2 = (esc.get("overall") or {}).get("verdict")
                results[i] = {**esc, "tier": ESCALATION_MODEL,
                              "first_pass": {"verdict": v1,
                                             "needs_review": first["needs_review"],
                                             "error": first["error"]}}
                mark = "변경" if v1 != v2 else "유지"
                print(f"  [{mark}] {first['file']}: {v1} → {v2}")

    out = root / f"screening_results_{stamp}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in results if not r["needs_review"]]
    fails = [r for r in results if r["needs_review"]]
    verdicts = {}
    for r in ok:
        v = (r.get("overall") or {}).get("verdict") or "invalid"
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
            o1, o2 = r.get("overall") or {}, second.get("overall") or {}
            same = (o1.get("verdict") == o2.get("verdict")
                    and o1.get("submitter_signed") == o2.get("submitter_signed"))
            agree += same
            mark = "일치" if same else "불일치!"
            print(f"  [{mark}] {r['file']}: {o1.get('verdict')}/{o2.get('verdict')}, "
                  f"signed {o1.get('submitter_signed')}/{o2.get('submitter_signed')}")
        print(f"일관성: {agree}/{len(sample)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
