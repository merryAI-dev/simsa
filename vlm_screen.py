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

PROMPT_VERSION = "screen-v1"
SCREENING_PROMPT = """당신은 정부 지원사업 제출서류 적격심사 보조원입니다.
첨부된 PDF 문서를 보고 아래 JSON 스키마로만 답하세요. 판단이 불확실하면 확신도를 낮추고 uncertain 으로 두세요. 없는 것을 있다고 하지 마세요.

{
  "doc_type": "문서 유형 (예: 서약서, 공문, 사업자등록증, 예산계획서, 이력서, 기타)",
  "signature_or_seal": {
    "present": true | false,
    "kind": "자필서명 | 도장(인감) | 법인직인 | 전자서명 | 없음",
    "signer_name": "서명자/날인자 성명 (없으면 null)",
    "organization": "소속 기관/법인명 (없으면 null)",
    "page": 서명이 있는 페이지 번호 (없으면 null),
    "evidence": "판단 근거를 한 문장으로"
  },
  "date_written": "문서에 기재된 작성일 (YYYY-MM-DD, 없으면 null)",
  "verdict": "pass | fail | uncertain",
  "confidence": 0.0~1.0,
  "notes": "심사위원이 알아야 할 특이사항 (없으면 null)"
}"""


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
