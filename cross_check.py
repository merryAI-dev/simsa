"""제출건 단위 교차문서 검증 — 문서별 심사 결과를 모아 문서 간 정합성을 판정.

단일 문서 판별로는 못 잡는 유형을 잡는다:
  - 서약서/공문 서명·날인 주체가 사업자등록증·법인등기부의 대표자와 일치하는가
  - 기관/법인명이 문서 간 일관적인가 ((주)/주식회사 표기 변형은 허용)
  - 사업자등록번호·법인등록번호가 문서 간 일치하는가
  - 필수 서류가 빠지지 않았는가 (코드 레벨 + VLM 종합)

PDF 를 다시 보내지 않는다 — screen_batch 가 추출해둔 구조화 데이터(텍스트)만
제출건당 1회 gemini-pro-latest 에 보낸다. 응답은 캐시된다.

usage: python cross_check.py <screening_results.json> [workers=4]
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

from vlm_cache import VLMCache
from vlm_screen import load_api_key, url_for

BASE = Path(__file__).parent
XCHECK_MODEL = "gemini-pro-latest"
XCHECK_VERSION = "xcheck-v3.1"  # v3: 공식 서류구분(fail/보완)·여행경보·자격규칙을 reference_data 에서 주입. v3.1: 3개월 발급기준을 건강보험확인서로 한정(사업자등록증 오탐 수정) + 타임아웃 재시도

REFS = json.loads((BASE / "data/reference_data.json").read_text(encoding="utf-8"))

XCHECK_PROMPT = """당신은 정부 지원사업 적격심사 보조원입니다. 오늘(심사 기준일)은 {today} 입니다. 날짜의 과거/미래, 경과 기간은 반드시 이 기준일로 계산하세요.
아래는 한 지원기관이 제출한 서류 묶음을 문서별로 분석한 결과(JSON)입니다.
문서 간 정합성을 검토하여 아래 스키마의 JSON 으로만 답하세요.

검토 항목:
1. representative_match — 서약서·공문에 서명/날인한 사람이 사업자등록증 또는 법인등기부의 대표자와 일치하는가. 대표자가 아닌 사람(예: 연구소장, 이사)이 서약서에 서명했다면 mismatch 로 하고 누가 서명했어야 하는지 detail 에 쓰세요.
2. company_name_consistency — 기관/법인명이 문서 간 일관적인가. (주)↔주식회사, 띄어쓰기 차이는 같은 것으로 간주.
3. reg_no_consistency — 사업자등록번호·법인등록번호가 문서 간 서로 일치하는가. 비교할 값이 한 문서에만 있으면 not_applicable.
4. date_anomalies — 작성일·발급일의 이상. **3개월 이내 발급 기준은 건강보험자격득실확인서에만 적용됩니다** — 사업자등록증·법인등기부 등 다른 증명서는 발급일이 오래됐다고 이상이 아닙니다(폐업 후 재등록처럼 실제 문제가 있는 경우만 예외). 그 외 확인할 것: 작성일 누락, 미래 날짜, 발급일이 작성일보다 늦는 등 순서가 맞지 않는 경우.
5. document_set — 서류 묶음 관점의 특이사항 (빈 양식 제출, 페이지 누락 문서, 서명 누락 문서 종합).
6. country_eligibility — 사업 대상 국가/지역이 여행경보 기준을 통과하는가. 아래 '여행경보 판정 기준'과 '대상 국가 여행경보 정보'만 근거로 판단하세요. 제안 지역이 3단계(출국권고)·4단계(여행금지)·특별여행주의보 지역에 해당하면 mismatch, 대상 지역이 특정되지 않았는데 그 국가에 3단계 이상 지역이 존재하면 uncertain (어느 지역인지 확인 필요하다고 detail 에 쓰기), 경보 정보가 없는 국가면 uncertain (외교부 0404 확인 필요). 대상 국가 자체가 추출 안 됐으면 uncertain.
7. eligibility_rules — 아래 '지원자격 규칙'을 추출값에 적용할 수 있는 항목만 판정하세요 (자산총계 120억 기준 성숙기업, 국내 법인 여부, 업력 10년). 판정에 필요한 값이 추출 안 됐거나 규칙에 '확인필요' 조건이 걸리면 uncertain. 어떤 값으로 어떻게 계산했는지 detail 에 쓰세요.

규칙:
- 제공된 데이터에 있는 값만 근거로 판단하세요. 데이터에 없는 것을 추측하지 마세요.
- 판단 근거가 부족하면 status="uncertain". 지어내는 것보다 uncertain 이 항상 낫습니다.

여행경보 판정 기준:
{advisory_rule}

대상 국가 여행경보 정보 (외교부 {advisory_date} 기준):
{advisory_block}

지원자격 규칙:
{eligibility_block}
- 공모 공고일: {announcement_date} (미입력이면 업력 판정은 uncertain)

{
  "checks": [
    {"id": "representative_match", "status": "ok | mismatch | uncertain | not_applicable", "detail": "근거 한두 문장"},
    {"id": "company_name_consistency", "status": "...", "detail": "..."},
    {"id": "reg_no_consistency", "status": "...", "detail": "..."},
    {"id": "date_anomalies", "status": "...", "detail": "..."},
    {"id": "document_set", "status": "...", "detail": "..."},
    {"id": "country_eligibility", "status": "...", "detail": "..."},
    {"id": "eligibility_rules", "status": "...", "detail": "..."}
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


def missing_required(records: list[dict]) -> tuple[list[str], list[str]]:
    """코드 레벨 서류 존재 체크 (파일명 + doc_type 동시 탐색).
    reference_data 의 공식 구분표 기반 — (적격탈락급 누락, 보완요청급 누락) 반환."""
    haystack = []
    for r in records:
        haystack.append(r["file"])
        for d in (r.get("result") or {}).get("documents", []):
            if isinstance(d, dict) and d.get("doc_type"):
                haystack.append(str(d["doc_type"]))
    blob = " ".join(haystack)
    fail_miss, supp_miss = [], []
    for doc in REFS["required_documents"]:
        if any(h in blob for h in doc["hints"]):
            continue
        label = doc["name"] + (f" ({doc['condition']})" if doc.get("condition") else "")
        (fail_miss if doc["severity"] == "fail" else supp_miss).append(label)
    return fail_miss, supp_miss


def advisory_block_for(docs: list[dict]) -> str:
    """문서에서 추출된 대상 국가의 여행경보 정보만 프롬프트에 주입 (토큰 절약 + 정확도)."""
    countries: set[str] = set()
    for d in docs:
        tc = (d.get("extracted") or {}).get("target_country") or ""
        for part in str(tc).replace("/", ",").split(","):
            p = part.strip()
            if p and p.lower() not in ("null", "none"):
                countries.add(p)
    if not countries:
        return "- (문서에서 사업 대상 국가가 추출되지 않음 — country_eligibility 는 uncertain)"
    lines = []
    for c in sorted(countries):
        hit = next((k for k in REFS["travel_advisory"] if k in c or c in k), None)
        if hit:
            lines.append(f"- {hit}: {REFS['travel_advisory'][hit]}")
        else:
            lines.append(f"- {c}: (여행경보 데이터에 없는 국가 — 외교부 0404.go.kr 확인 필요, uncertain 처리)")
    return "\n".join(lines)


def eligibility_block() -> str:
    return "\n".join(f"- {v}" for v in REFS["eligibility_rules"].values())


def cross_check(submission: str, records: list[dict], results_path: Path,
                cache: VLMCache, api_key: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    docs = compact_summary(records)
    # .format() 은 스키마의 중괄호와 충돌하므로 replace 사용
    payload = (XCHECK_PROMPT
               .replace("{today}", today)
               .replace("{advisory_rule}", REFS["advisory_exclusion_rule"])
               .replace("{advisory_date}", REFS["travel_advisory_date"])
               .replace("{advisory_block}", advisory_block_for(docs))
               .replace("{eligibility_block}", eligibility_block())
               .replace("{announcement_date}", REFS.get("announcement_date") or "미입력")
               + json.dumps(docs, ensure_ascii=False, indent=1))

    key = cache.key_for(results_path,
                        extra=f"xcheck:{submission}:{today}:{REFS.get('version', '')}")
    cached = cache.get(key)
    if cached is not None:
        return {"submission": submission, "cache_hit": True, **cached}

    body = {
        "contents": [{"parts": [{"text": payload}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    for attempt in range(3):
        try:
            resp = requests.post(url_for(XCHECK_MODEL), json=body, timeout=600,
                                 headers={"X-goog-api-key": api_key})
        except requests.exceptions.RequestException as e:
            # 타임아웃·연결끊김 등 네트워크 레벨 오류도 재시도 (기존엔 HTTP 상태코드만 재시도했음)
            print(f"  [재시도 {attempt + 1}/3] 네트워크 오류: {e}")
            time.sleep(5 * (attempt + 1))
            continue
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
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    all_records = json.loads(results_path.read_text(encoding="utf-8"))

    by_sub: dict[str, list[dict]] = {}
    for r in all_records:
        by_sub.setdefault(r["submission"], []).append(r)

    api_key = load_api_key()
    cache = VLMCache(BASE / "cache/vlm",
                     prompt_version=f"{XCHECK_VERSION}@{XCHECK_MODEL}")

    def one(item: tuple[str, list[dict]]) -> dict:
        sub, records = item
        fail_miss, supp_miss = missing_required(records)
        xc = cross_check(sub, records, results_path, cache, api_key)
        xc["missing_required_docs"] = fail_miss + supp_miss  # 하위 호환 (excel_report 등)
        xc["missing_fail_docs"] = fail_miss          # 미제출 시 적격탈락
        xc["missing_supplement_docs"] = supp_miss    # 대면심사 시 보완요청
        if (fail_miss or supp_miss) and xc.get("overall") == "ok":
            xc["overall"] = "issues_found"
        return xc

    # 제출건 간 의존성이 없으므로 병렬 처리 (300건 규모: 순차 3h+ → 워커 4 기준 ~50분)
    items = sorted(by_sub.items())
    print(f"제출 {len(items)}건 / workers={workers}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(one, items))

    for xc in out:
        sub = xc["submission"]
        fail_miss = xc["missing_fail_docs"]
        supp_miss = xc["missing_supplement_docs"]
        print(f"\n=== {sub} [{'cache' if xc.get('cache_hit') else 'api'}] → {xc.get('overall')}")
        if fail_miss:
            print(f"  [적격탈락급 누락] {', '.join(fail_miss)}")
        if supp_miss:
            print(f"  [보완요청급 누락] {', '.join(supp_miss)}")
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
