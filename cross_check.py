"""제출건 단위 교차문서 검증 — 문서별 심사 결과를 모아 문서 간 정합성을 판정.

단일 문서 판별로는 못 잡는 유형을 잡는다:
  - 서약서/공문 서명·날인 주체가 사업자등록증·법인등기부의 대표자와 일치하는가
  - 기관/법인명이 문서 간 일관적인가 ((주)/주식회사 표기 변형은 허용)
  - 사업자등록번호·법인등록번호가 문서 간 일치하는가
  - 필수 서류가 빠지지 않았는가 (코드 레벨 + VLM 종합)

PDF 를 다시 보내지 않는다 — screen_batch 가 추출해둔 구조화 데이터(텍스트)만
제출건당 1회 gemini-pro-latest 에 보낸다. 응답은 캐시된다.

usage: python cross_check.py <screening_results.json>
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from vlm_cache import VLMCache
from vlm_screen import load_api_key, url_for

BASE = Path(__file__).parent
XCHECK_MODEL = "gemini-pro-latest"
XCHECK_VERSION = "xcheck-v2"  # v2: 심사 기준일 주입 (미래날짜 오탐 방지)

# 파일명/doc_type 기반 필수서류 체크 (VLM 이전, 코드 레벨)
REQUIRED_DOC_TYPES = {
    "공문": ("공문",),
    "서약서": ("서약서",),
    "사업개요서": ("개요서", "사업요약서"),
    "사업제안서": ("제안서",),
    "예산계획서": ("예산",),
    "사업자등록증": ("사업자등록",),
    "법인등기부": ("등기",),
    "건강보험자격득실확인서": ("건강보험",),
}

XCHECK_PROMPT = """당신은 정부 지원사업 적격심사 보조원입니다. 오늘(심사 기준일)은 {today} 입니다. 날짜의 과거/미래, 경과 기간은 반드시 이 기준일로 계산하세요.
아래는 한 지원기관이 제출한 서류 묶음을 문서별로 분석한 결과(JSON)입니다.
문서 간 정합성을 검토하여 아래 스키마의 JSON 으로만 답하세요.

검토 항목:
1. representative_match — 서약서·공문에 서명/날인한 사람이 사업자등록증 또는 법인등기부의 대표자와 일치하는가. 대표자가 아닌 사람(예: 연구소장, 이사)이 서약서에 서명했다면 mismatch 로 하고 누가 서명했어야 하는지 detail 에 쓰세요.
2. company_name_consistency — 기관/법인명이 문서 간 일관적인가. (주)↔주식회사, 띄어쓰기 차이는 같은 것으로 간주.
3. reg_no_consistency — 사업자등록번호·법인등록번호가 문서 간 서로 일치하는가. 비교할 값이 한 문서에만 있으면 not_applicable.
4. date_anomalies — 작성일·발급일의 이상 (예: 증명서 발급일이 제출 시점 기준 3개월 초과 경과, 작성일 누락, 미래 날짜).
5. document_set — 서류 묶음 관점의 특이사항 (빈 양식 제출, 페이지 누락 문서, 서명 누락 문서 종합).

규칙:
- 제공된 데이터에 있는 값만 근거로 판단하세요. 데이터에 없는 것을 추측하지 마세요.
- 판단 근거가 부족하면 status="uncertain".

{
  "checks": [
    {"id": "representative_match", "status": "ok | mismatch | uncertain | not_applicable", "detail": "근거 한두 문장"},
    {"id": "company_name_consistency", "status": "...", "detail": "..."},
    {"id": "reg_no_consistency", "status": "...", "detail": "..."},
    {"id": "date_anomalies", "status": "...", "detail": "..."},
    {"id": "document_set", "status": "...", "detail": "..."}
  ],
  "overall": "ok | issues_found | uncertain",
  "issues_summary": "심사위원용 요약 (문제 없으면 null)"
}

분석 데이터:
"""


def compact_summary(records: list[dict]) -> list[dict]:
    """교차검증에 필요한 필드만 추려 토큰을 아낀다."""
    docs = []
    for r in records:
        for d in (r.get("result") or {}).get("documents", []):
            if not isinstance(d, dict):
                continue
            s = d.get("signature_or_seal") or {}
            docs.append({
                "file": r["file"],
                "doc_type": d.get("doc_type"),
                "verdict": d.get("verdict"),
                "signer": {k: s.get(k) for k in ("present", "kind", "seal_owner",
                                                 "signer_name", "organization")},
                "extracted": d.get("extracted"),
                "completeness": d.get("completeness"),
                "date_written": d.get("date_written"),
                "notes": d.get("notes"),
            })
    return docs


def missing_required(records: list[dict]) -> list[str]:
    """코드 레벨 필수서류 존재 체크 (파일명 + doc_type 동시 탐색)."""
    haystack = []
    for r in records:
        haystack.append(r["file"])
        for d in (r.get("result") or {}).get("documents", []):
            if isinstance(d, dict) and d.get("doc_type"):
                haystack.append(str(d["doc_type"]))
    blob = " ".join(haystack)
    return [need for need, keys in REQUIRED_DOC_TYPES.items()
            if not any(k in blob for k in keys)]


def cross_check(submission: str, records: list[dict], results_path: Path,
                cache: VLMCache, api_key: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    docs = compact_summary(records)
    # .format() 은 스키마의 중괄호와 충돌하므로 replace 사용
    payload = (XCHECK_PROMPT.replace("{today}", today)
               + json.dumps(docs, ensure_ascii=False, indent=1))

    key = cache.key_for(results_path, extra=f"xcheck:{submission}:{today}")
    cached = cache.get(key)
    if cached is not None:
        return {"submission": submission, "cache_hit": True, **cached}

    body = {
        "contents": [{"parts": [{"text": payload}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    for attempt in range(3):
        resp = requests.post(url_for(XCHECK_MODEL), json=body, timeout=180,
                             headers={"X-goog-api-key": api_key})
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        try:
            result = json.loads(text)
            if isinstance(result, list) and len(result) == 1:
                result = result[0]
        except json.JSONDecodeError:
            continue
        cache.set(key, result, source=submission)
        return {"submission": submission, "cache_hit": False, **result}
    return {"submission": submission, "overall": "uncertain",
            "issues_summary": "교차검증 API 3회 실패 — 사람 확인 필요", "checks": []}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    results_path = Path(sys.argv[1])
    all_records = json.loads(results_path.read_text(encoding="utf-8"))

    by_sub: dict[str, list[dict]] = {}
    for r in all_records:
        by_sub.setdefault(r["submission"], []).append(r)

    api_key = load_api_key()
    cache = VLMCache(BASE / "cache/vlm",
                     prompt_version=f"{XCHECK_VERSION}@{XCHECK_MODEL}")

    out = []
    for sub, records in sorted(by_sub.items()):
        miss = missing_required(records)
        xc = cross_check(sub, records, results_path, cache, api_key)
        xc["missing_required_docs"] = miss
        if miss and xc.get("overall") == "ok":
            xc["overall"] = "issues_found"
        out.append(xc)

        print(f"\n=== {sub} [{'cache' if xc.get('cache_hit') else 'api'}] → {xc.get('overall')}")
        if miss:
            print(f"  [필수서류 누락] {', '.join(miss)}")
        for c in xc.get("checks", []):
            if c.get("status") not in ("ok", "not_applicable"):
                print(f"  [{c.get('status')}] {c.get('id')}: {c.get('detail')}")
        if xc.get("issues_summary"):
            print(f"  요약: {xc['issues_summary']}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = results_path.parent / f"cross_check_{stamp}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
