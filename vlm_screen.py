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
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

PROMPT_VERSION = "screen-v2"
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
- signer_name 은 서명·날인 바로 옆에 적힌 이름만 쓰세요. 문서의 다른 곳에 나온 이름을 추측해서 넣지 마세요."""


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
