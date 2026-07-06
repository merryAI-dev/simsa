"""PDF 문서를 Gemini VLM 으로 적격심사 항목 판별 (서명/날인 확인 포함).

키는 .env 의 GEMINI_API_KEY 에서 읽는다. 코드에 키를 넣지 않는다.
응답은 vlm_cache 로 캐시되어 같은 파일 + 같은 프롬프트 버전이면 API 호출이 없다.

usage: python vlm_screen.py <pdf> [pdf ...]
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests

from vlm_cache import VLMCache

BASE = Path(__file__).parent
MODEL = "gemini-flash-latest"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"


def url_for(model: str = MODEL) -> str:
    return f"{API_ROOT}/{model}:generateContent"


def cache_version(model: str = MODEL) -> str:
    """캐시 키에 들어가는 버전 문자열. 기본 모델은 기존 키와 호환 유지,
    다른 모델은 모델명을 붙여 캐시를 분리한다 (flash 응답이 pro 로 둔갑 방지)."""
    return PROMPT_VERSION if model == MODEL else f"{PROMPT_VERSION}@{model}"


URL = url_for(MODEL)

PROMPT_VERSION = "screen-v4.1"  # v4: 사업분야·대상국가·자산총계·설립일 추출. v4.1: founding_date 출처 제한
SCREENING_PROMPT = """당신은 정부 지원사업 제출서류 적격심사 보조원입니다.
첨부된 PDF 를 보고 아래 JSON 스키마로만 답하세요.

하나의 PDF 에 독립된 문서가 여러 개 있으면(예: 첨부 5-1 서약서와 첨부 5-2 서약서) documents 배열에 문서마다 항목을 하나씩 만드세요. 문서가 하나면 항목도 하나입니다.

{
  "documents": [
    {
      "doc_type": "문서 유형 (예: 서약서, 공문, 사업자등록증, 예산계획서, 이력서, 기타)",
      "signature_or_seal": {
        "present": true | false,
        "kind": "자필서명 | 도장(인감) | 법인직인 | 전자서명 | 불명확 | 없음",
        "seal_owner": "제출자 | 발급기관 | 불명",
        "signer_name": "서명자/날인자 성명 (없으면 null)",
        "organization": "소속 기관/법인명 (없으면 null)",
        "page": 서명이 있는 페이지 번호 (없으면 null),
        "evidence": "판단 근거를 한 문장으로"
      },
      "date_written": "문서에 기재된 작성일 (YYYY-MM-DD, 없으면 null)",
      "extracted": {
        "company_name": "문서에 기재된 기관/법인명 (없으면 null)",
        "representative_name": "문서에 기재된 대표자/대표이사 성명 (없으면 null)",
        "business_reg_no": "사업자등록번호 (없으면 null)",
        "corp_reg_no": "법인등록번호 (없으면 null)",
        "issue_date": "증명서류의 발급일 YYYY-MM-DD (없으면 null)",
        "founding_date": "개업연월일 또는 법인성립연월일 YYYY-MM-DD — 사업자등록증·법인등기사항전부증명서에서만 기록, 다른 문서 유형에서는 반드시 null",
        "business_field": "사업주제/사업분야 — 사업개요서·제안서 표지나 개요 표에 기재된 것 그대로 (없으면 null)",
        "tech_field": "세부 기술분야 (없으면 null)",
        "target_country": "사업 대상 국가명 (없으면 null)",
        "target_region": "사업 대상 지역/도시 — 국가보다 세부 단위로 기재된 경우 (없으면 null)",
        "asset_total_krw": "재무제표(요약손익계산서·재무상태표)에 기재된 최근연도 자산총계 — 원 단위 숫자, 단위 표기가 천원/백만원이면 원으로 환산 (없으면 null)"
      },
      "completeness": {
        "complete": true | false,
        "evidence": "판단 근거 한 문장 (예: '2페이지 중 2페이지 모두 포함됨')"
      },
      "verdict": "pass | fail | uncertain",
      "confidence": 0.0~1.0,
      "notes": "심사위원이 알아야 할 특이사항 (없으면 null)"
    }
  ]
}

판정 규칙:
- 없는 것을 있다고 하지 마세요. 불확실하면 verdict="uncertain" 으로 두고 confidence 를 낮추세요.
- 서명인지 도장인지조차 불분명한 표시(낙서 같은 표시, X자, 흐릿한 자국)는 present 로 단정하지 말고 kind="불명확", verdict="uncertain", confidence 0.6 이하로 하세요. 서명 확인은 심사에서 중요하므로 애매하면 반드시 사람 확인으로 넘겨야 합니다.
- kind 와 seal_owner 는 반드시 보기 중 정확히 하나만 고르세요. 여러 형태가 함께 있으면 대표적인 것 하나를 고르고 나머지는 notes 에 쓰세요.
- seal_owner: 제출 기관(신청자/대표자)의 서명·날인이면 "제출자", 세무서장·법원 등 증명서 발급기관의 관인이면 "발급기관".
- 원래 서명·날인이 필요 없는 문서 유형(사업개요서, 사업제안서, 예산계획서, 체크리스트, 증빙자료 모음 등)은 서명이 없다는 이유로 fail 하지 마세요. 내용이 실제로 작성되어 있으면 pass 입니다.
- 내용이 채워지지 않은 빈 양식(템플릿)이 제출된 경우는 fail 입니다.
- signer_name 은 서명·날인 바로 옆에 적힌 이름만 쓰세요. 문서의 다른 곳에 나온 이름을 추측해서 넣지 마세요.
- extracted 필드는 문서에 명시적으로 적힌 값만 옮기세요. 추측하거나 다른 문서에서 가져오지 마세요.
- 증명서류(사업자등록증, 법인등기사항전부증명서, 건강보험자격득실확인서 등)에 총 페이지 수나 장수가 표기되어 있는데 실제 포함된 페이지가 부족하면(예: '2페이지 중 1페이지') completeness.complete=false 로 하고 verdict="uncertain" 으로 하세요.
- 법인등기사항전부증명서는 법인명, 대표이사(사내이사 등 대표권자) 성명, 법인등록번호, 발급일을 extracted 에 기록하세요. 해산·청산·말소 등기, 임원 임기 만료, 폐쇄사항 표시가 보이면 notes 에 쓰고 verdict="uncertain" 으로 하세요.
- 사업개요서·사업제안서에서는 사업주제(business_field), 세부 기술분야(tech_field), 사업 대상 국가(target_country)와 세부 지역(target_region)을 찾아 extracted 에 기록하세요. 표지, 사업 개요 표, 사업명 문장 어디에 있든 기재된 표현 그대로 옮기고, 여러 국가면 쉼표로 나열하세요.
- 사업제안서에 재무제표(요약손익계산서, 재무상태표 등)가 있으면 최근연도 자산총계를 asset_total_krw 에 원 단위 숫자로 기록하세요. 표의 단위(천원, 백만원 등)를 반드시 확인해 원으로 환산하고, 환산 근거를 notes 에 쓰세요.
- 개인정보 수집·이용 동의서는 '동의함' 체크 여부와 서명·날인을 함께 확인하세요. 동의 체크가 없으면 notes 에 쓰고 verdict="uncertain" 으로 하세요.
- 예산계획서는 빈 양식이 아닌지(금액이 실제로 기입됐는지) 확인하세요."""


def load_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        env = BASE / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("GEMINI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("GEMINI_API_KEY 가 없습니다. .env 또는 환경변수로 설정하세요.")
    return key


def screen_pdf(pdf: Path, cache: VLMCache, api_key: str) -> tuple[dict, bool]:
    """(판별 결과, 캐시 히트 여부) 를 반환."""
    key = cache.key_for(pdf)
    cached = cache.get(key)
    if cached is not None:
        return cached, True

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
    resp = requests.post(
        URL, json=body, timeout=120,
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
    )
    resp.raise_for_status()
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    result = json.loads(text)
    cache.set(key, result, source=pdf.name)
    return result, False


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    api_key = load_api_key()
    cache = VLMCache(BASE / "cache/vlm", prompt_version=PROMPT_VERSION)

    for arg in sys.argv[1:]:
        pdf = Path(arg)
        result, hit = screen_pdf(pdf, cache, api_key)
        tag = "cache" if hit else "api"
        print(f"\n=== {pdf.name} [{tag}] ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
