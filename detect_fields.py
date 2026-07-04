"""PDF 한 건에서 문서 유형 판별 + 팩 규칙 필드 탐지 (Gemini VLM, box_2d 좌표 포함).

- 문서 유형: 팩의 doc_types 레지스트리를 프롬프트에 주입. 내용이 주 판별자,
  파일명은 보조 근거. 레지스트리에 없으면 registered=false 로 능동 보고.
- 규칙 종류: extract(값+위치) / verify(pass·fail·uncertain 판정 + 근거 위치).
- 응답은 vlm_cache 로 캐시 — 같은 파일 + 같은 규칙·유형 세트면 API 호출이 없다.

usage: python detect_fields.py <pdf> [pdf ...]   # koica-cts 팩 규칙으로 탐지
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import fitz
import requests

from vlm_cache import VLMCache
from vlm_screen import load_api_key

BASE = Path(__file__).parent
MODEL = "gemini-flash-latest"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
PROMPT_VERSION = "detect-v2"
RENDER_DPI = 150

PROMPT_TEMPLATE = """당신은 정부 지원사업 제출서류 적격심사 보조원입니다.
첨부된 PDF 를 분석해 (1) 문서 유형을 판별하고 (2) 탐지 규칙을 적용해 JSON 으로만 답하세요.

파일명(보조 근거): {filename}

등록된 문서 유형 (이름 | 파일명 힌트 | 설명):
{doc_types_block}

탐지 규칙 (rule_id | 종류 | 문서 유형 | 필드 | 설명):
{rules_block}

{{
  "doc_type": "판별한 문서 유형",
  "doc_type_registered": true | false,
  "doc_type_evidence": "판별 근거 한 문장",
  "detections": [
    {{
      "rule_id": 규칙의 rule_id 숫자,
      "field": "필드 이름",
      "value": "extract: 문서에 적힌 값 그대로 / verify: 판단 근거 한 문장",
      "verdict": "verify 규칙만 pass | fail | uncertain, extract 는 null",
      "page": 값이 있는 페이지 번호 (1부터),
      "box_2d": [ymin, xmin, ymax, xmax],
      "confidence": 0.0~1.0
    }}
  ]
}}

규칙:
- 문서 유형은 내용을 기준으로 판별하고 파일명은 보조로만 쓰세요. 등록된 유형 중 맞는 것이
  있으면 그 이름을 글자 그대로 쓰고 doc_type_registered=true, 없으면 유형을 자유롭게
  기술하고 false 로 하세요. 파일명과 내용이 다르면 내용을 따르고 evidence 에 언급하세요.
- 규칙 적용 대상: 규칙의 문서 유형이 판별 유형과 같거나 상위 개념이거나('서약서' 규칙은
  모든 종류의 서약서·선언서에 적용) '*' 인 규칙.
- extract 규칙: 같은 필드의 값이 여러 개면(업태·종목, 페이지마다 있는 서명 등) 항목을
  여러 개 만드세요. 값이 여러 줄이면 박스로 전체를 감싸세요.
- verify 규칙: verdict 로 판정하고 value 에 근거 한 문장, box_2d 는 판단 근거 위치
  (서명·도장 표시, 빈 서명란 등). 애매하면 uncertain — 사람 확인으로 넘어갑니다.
- box_2d 는 해당 페이지 안에서 0~1000 으로 정규화된 [ymin, xmin, ymax, xmax] 이며
  라벨이 아니라 값 텍스트(또는 근거)를 최대한 정확히 감쌉니다.
- 문서에 없는 필드는 넣지 마세요. 없는 것을 있다고 하지 마세요. 불확실하면 confidence 를 낮추세요."""

SUGGEST_PROMPT = """당신은 정부 지원사업 제출서류 적격심사 설계자입니다.
첨부된 PDF 는 '{doc_type}' 유형의 제출서류 견본입니다.
이 유형의 서류를 자동 심사할 때 점검할 가치가 있는 항목을 3~6개 제안하세요. JSON 으로만 답하세요.

이미 등록된 규칙 (중복 제안 금지):
{existing_block}

{{
  "suggestions": [
    {{
      "doc_type": "{doc_type}",
      "field": "필드 이름 (간결하게)",
      "rule_type": "extract | verify",
      "instruction": "탐지/판정 방법 설명 한 문장",
      "why": "왜 심사에 중요한지 한 문장"
    }}
  ]
}}

- extract 는 값을 뽑아 교차대조할 항목(번호, 날짜, 성명, 금액 등).
- verify 는 참/거짓 판정 항목(서명 유효성, 빈 양식 여부, 페이지 완비 등).
- 이 문서에서 실제로 확인 가능한 것만 제안하세요."""


def _gemini(parts: list[dict], api_key: str) -> dict:
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    resp = requests.post(
        URL, json=body, timeout=180,
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
    )
    resp.raise_for_status()
    result = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
    if isinstance(result, list):  # Gemini 가 배열로 감싸는 변덕 대응
        result = result[0] if result else {}
    return result


def _pdf_part(pdf: Path) -> dict:
    return {"inline_data": {"mime_type": "application/pdf", "data": base64.b64encode(pdf.read_bytes()).decode()}}


def rules_block(rules: list[dict]) -> str:
    return "\n".join(
        f"- {r['id']} | {r.get('rule_type', 'extract')} | {r['doc_type']} | {r['field']} | {r['instruction'] or '-'}"
        for r in rules
    ) or "- (규칙 없음)"


def doc_types_block(doc_types: list[dict]) -> str:
    return "\n".join(
        f"- {t['name']} | {', '.join(t['filename_hints']) or '-'} | {t['description'] or '-'}"
        for t in doc_types
    ) or "- (등록된 유형 없음 — 자유 기술)"


def fingerprint(rules: list[dict], doc_types: list[dict]) -> str:
    payload = json.dumps([
        [[r["id"], r["doc_type"], r["field"], r.get("rule_type", "extract"), r["instruction"]]
         for r in sorted(rules, key=lambda r: r["id"])],
        [[t["name"], t["filename_hints"], t["description"]] for t in sorted(doc_types, key=lambda t: t["name"])],
    ], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def render_pages(pdf: Path, out_dir: Path) -> list[dict]:
    """PDF 전 페이지를 PNG 로 렌더하고 [{page_no, image_path, width, height}] 를 반환."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    with fitz.open(pdf) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=RENDER_DPI)
            path = out_dir / f"p{i}.png"
            pix.save(path)
            pages.append({"page_no": i, "image_path": str(path), "width": pix.width, "height": pix.height})
    return pages


def detect(pdf: Path, rules: list[dict], doc_types: list[dict], cache: VLMCache, api_key: str,
           filename: str | None = None) -> tuple[dict, bool]:
    """(탐지 결과, 캐시 히트 여부). 결과: {doc_type, doc_type_registered, doc_type_evidence, detections}"""
    key = cache.key_for(pdf, extra=fingerprint(rules, doc_types) + (filename or pdf.name))
    cached = cache.get(key)
    if cached is not None:
        return cached, True

    prompt = PROMPT_TEMPLATE.format(
        filename=filename or pdf.name,
        doc_types_block=doc_types_block(doc_types),
        rules_block=rules_block(rules),
    )
    result = _gemini([_pdf_part(pdf), {"text": prompt}], api_key)
    result.setdefault("doc_type", "")
    result.setdefault("doc_type_registered", False)
    result.setdefault("doc_type_evidence", "")
    result.setdefault("detections", [])
    valid_ids = {r["id"] for r in rules}
    result["detections"] = [
        d for d in result["detections"]
        if isinstance(d.get("box_2d"), list) and len(d["box_2d"]) == 4 and d.get("field")
        and (d.get("rule_id") in valid_ids or d.get("rule_id") is None)
    ]
    for d in result["detections"]:
        if d.get("verdict") not in ("pass", "fail", "uncertain"):
            d["verdict"] = ""
    cache.set(key, result, source=pdf.name)
    return result, False


def suggest_rules(pdf: Path, doc_type: str, existing: list[dict], api_key: str) -> list[dict]:
    """이 문서 유형에서 점검할 가치가 있는 규칙을 VLM 이 제안."""
    existing_block = "\n".join(
        f"- {r['doc_type']} | {r['field']}" for r in existing
    ) or "- (없음)"
    prompt = SUGGEST_PROMPT.format(doc_type=doc_type or "미상", existing_block=existing_block)
    result = _gemini([_pdf_part(pdf), {"text": prompt}], api_key)
    out = []
    for s in result.get("suggestions", []):
        if s.get("field") and s.get("rule_type") in ("extract", "verify"):
            out.append({
                "doc_type": s.get("doc_type") or doc_type, "field": s["field"],
                "rule_type": s["rule_type"], "instruction": s.get("instruction") or "",
                "why": s.get("why") or "",
            })
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    import db

    with db.connect() as conn:
        rules = conn.execute(
            "SELECT r.id, r.doc_type, r.field, r.rule_type, r.instruction FROM rules r "
            "JOIN packs p ON p.id = r.pack_id WHERE p.slug = 'koica-cts' ORDER BY r.id"
        ).fetchall()
        doc_types = conn.execute(
            "SELECT t.name, t.filename_hints, t.description FROM doc_types t "
            "JOIN packs p ON p.id = t.pack_id WHERE p.slug = 'koica-cts' ORDER BY t.id"
        ).fetchall()
    api_key = load_api_key()
    cache = VLMCache(BASE / "cache/vlm", prompt_version=PROMPT_VERSION)
    for arg in sys.argv[1:]:
        result, hit = detect(Path(arg), rules, doc_types, cache, api_key)
        reg = "등록" if result["doc_type_registered"] else "미등록"
        print(f"\n=== {Path(arg).name} [{'cache' if hit else 'api'}] {result['doc_type']} ({reg}) ===")
        print(f"    근거: {result['doc_type_evidence']}")
        for d in result["detections"]:
            v = f" verdict={d['verdict']}" if d.get("verdict") else ""
            print(f"  {d['field']}: {d.get('value', '')!r}{v} p{d.get('page')} box={d['box_2d']} conf={d.get('confidence')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
