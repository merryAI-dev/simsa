"""제출물 파일명 정규화: PDF 변환 + VLM 분류 → 규칙 파일명으로 '완료' 폴더에 저장.

제출 ZIP(또는 폴더)을 받아 제출건별로:
  1. batch_convert 체인으로 PDF 변환 (매직바이트 판별, 위장 HWP 보정 포함)
  2. 원본 파일명에서 규칙 힌트 매칭 → 실패 시 변환된 PDF 내용을 VLM 으로 분류
  3. naming_rules.json 의 template 대로 파일명을 바꿔 출력폴더에 최종 저장
  4. rename_map.json 에 원본명→저장명 매핑 기록 (침묵 없음)

분류 실패 파일은 버리지 않고 '미분류_<원본명>' 으로 보존한다.
VLM 응답은 cache/rename 에 캐시된다 (같은 파일 + 같은 유형 목록이면 API 호출 없음).

usage: python normalize_files.py <zip|folder> [--rules naming_rules.json] [--out 완료] [--workers 4]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

from batch_convert import SKIP_NAMES, collect_submissions, convert_submission
from vlm_cache import VLMCache
from vlm_screen import MODEL, load_api_key, url_for

BASE = Path(__file__).parent
CLASSIFY_VERSION = "rename-v1"
CLASSIFY_PROMPT = """이 PDF 문서가 아래 유형 목록 중 어떤 것인지 판별하세요. JSON 으로만 답하세요.

유형 목록: {types}

{{"doc_type": "목록 중 정확히 하나 또는 '기타'", "confidence": 0.0~1.0}}

표지·제목·본문 서식을 근거로 판단하세요. 목록에 없는 유형이거나 확신이 없으면 '기타'와 낮은 confidence 를 반환하세요. 목록에 없는 이름을 지어내지 마세요."""


def load_rules(path: Path) -> dict:
    rules = json.loads(path.read_text(encoding="utf-8"))
    for field in ("template", "doc_types"):
        if field not in rules:
            sys.exit(f"규칙 파일에 '{field}' 가 없습니다: {path}")
    return rules


def match_hint(original: str, rules: dict) -> dict | None:
    for dt in rules["doc_types"]:
        if any(h in original for h in dt.get("hints", [])):
            return dt
    return None


def classify_pdf(pdf: Path, rules: dict, cache: VLMCache, api_key: str) -> dict:
    """VLM 분류 결과 {doc_type, confidence} 를 반환. 실패 시 doc_type='기타'."""
    types = [d["doc_type"] for d in rules["doc_types"]]
    key = cache.key_for(pdf, extra=hashlib.sha256("|".join(types).encode()).hexdigest())
    cached = cache.get(key)
    if cached is not None:
        return cached
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {
                    "mime_type": "application/pdf",
                    "data": base64.b64encode(pdf.read_bytes()).decode(),
                }},
                {"text": CLASSIFY_PROMPT.format(types=json.dumps(types, ensure_ascii=False))},
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0},
    }
    last_err = None
    for _ in range(2):
        try:
            resp = requests.post(
                url_for(MODEL), json=body, timeout=120,
                headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
            )
            resp.raise_for_status()
            result = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
            if isinstance(result, list):
                result = result[0] if result else {}
            if not isinstance(result.get("doc_type"), str):
                raise ValueError(f"응답 스키마 불일치: {result}")
            cache.set(key, result, source=pdf.name)
            return result
        except Exception as e:  # noqa: BLE001 — 재시도 후 미분류로 보존, 침묵 실패 아님
            last_err = e
    print(f"  [WARN] VLM 분류 실패({pdf.name}): {last_err}")
    return {"doc_type": "기타", "confidence": 0.0, "error": str(last_err)}


def unique_name(stem: str, suffix: str, used: set[str]) -> str:
    name, n = f"{stem}{suffix}", 2
    while name in used:
        name = f"{stem} ({n}){suffix}"
        n += 1
    used.add(name)
    return name


def normalize_submission(name: str, records: list[dict], dest: Path, rules: dict,
                         cache: VLMCache, api_key: str | None) -> list[dict]:
    used: set[str] = set()
    mapping = []
    min_conf = rules.get("min_confidence", 0.5)
    unknown_prefix = rules.get("unknown_prefix", "미분류_")
    for r in records:
        original = r["file"]
        pdf = dest / r["pdf"] if r.get("pdf") else None
        dt, method, conf = match_hint(original, rules), "hint", 1.0
        if dt is None and pdf and pdf.exists():
            if api_key:
                v = classify_pdf(pdf, rules, cache, api_key)
                conf = float(v.get("confidence") or 0.0)
                found = [d for d in rules["doc_types"] if d["doc_type"] == v.get("doc_type")]
                if found and conf >= min_conf:
                    dt, method = found[0], "vlm"
                else:
                    method = "vlm_unknown"
            else:
                method = "no_api_key"

        if dt is not None:
            new_stem = rules["template"].format(code=dt.get("code", ""), doc_type=dt["doc_type"])
        else:
            new_stem = unknown_prefix + Path(original).stem

        entry = {"original": original, "doc_type": dt["doc_type"] if dt else None,
                 "method": method, "confidence": round(conf, 2),
                 "ok": r["ok"], "error": r.get("error", "")}
        if pdf and pdf.exists():
            new_name = unique_name(new_stem, ".pdf", used)
            pdf.rename(dest / new_name)
            entry["saved_as"] = new_name
            # 원본 보존 사본(엑셀 등)도 같은 이름으로 정렬
            companion = dest / original
            if companion.exists() and companion != dest / new_name:
                comp_name = unique_name(new_stem, companion.suffix.lower(), used)
                companion.rename(dest / comp_name)
                entry["companion"] = comp_name
        else:
            copied = dest / original
            if dt is not None and copied.exists():
                new_name = unique_name(new_stem, copied.suffix.lower(), used)
                copied.rename(dest / new_name)
                entry["saved_as"] = new_name
            else:
                entry["saved_as"] = original if copied.exists() else None
        mapping.append(entry)
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="제출 ZIP 또는 폴더")
    parser.add_argument("--rules", default=str(BASE / "naming_rules.json"))
    parser.add_argument("--out", default=str(BASE / "완료"))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    rules = load_rules(Path(args.rules))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        api_key: str | None = load_api_key()
    except SystemExit:
        api_key = None
        print("[WARN] GEMINI_API_KEY 없음 — 힌트 매칭만 사용, 나머지는 미분류로 보존")
    cache = VLMCache(BASE / "cache/rename", prompt_version=CLASSIFY_VERSION)

    input_path = Path(args.input)
    all_map: dict[str, list[dict]] = {}
    with tempfile.TemporaryDirectory(prefix="cts_norm_") as td:
        input_dir = input_path
        if input_path.is_file() and input_path.suffix.lower() == ".zip":
            outer = Path(td) / "_outer"
            with zipfile.ZipFile(input_path, metadata_encoding="cp949") as zf:
                zf.extractall(outer)
            tops = [p for p in outer.iterdir() if p.name not in SKIP_NAMES]
            input_dir = tops[0] if len(tops) == 1 and tops[0].is_dir() else outer
        subs = collect_submissions(input_dir, Path(td))
        for name, files in subs.items():
            print(f"\n=== {name} ({len(files)} files) ===")
            if (out_dir / name).exists():  # 재실행 시 이전 결과와 섞이지 않게
                shutil.rmtree(out_dir / name)
            records = convert_submission(name, files, out_dir, args.workers)
            mapping = normalize_submission(name, records, out_dir / name, rules, cache, api_key)
            all_map[name] = mapping
            for m in mapping:
                tag = {"hint": "규칙", "vlm": "VLM", "vlm_unknown": "미분류",
                       "no_api_key": "키없음"}.get(m["method"], m["method"])
                print(f"  [{tag}] {m['original']} → {m['saved_as']}")

    (out_dir / "rename_map.json").write_text(
        json.dumps(all_map, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(len(v) for v in all_map.values())
    unknown = [(s, m) for s, v in all_map.items() for m in v
               if m["saved_as"] and m["saved_as"].startswith(rules.get("unknown_prefix", "미분류_"))]
    fails = [(s, m) for s, v in all_map.items() for m in v if not m["ok"]]
    print(f"\n총 {len(all_map)}건 제출, {total}개 파일 / 미분류 {len(unknown)}개 / 변환실패 {len(fails)}개")
    for s, m in unknown + fails:
        print(f"  - {s} / {m['original']} ({m['method']}) {m['error']}")
    print(f"완료 폴더: {out_dir}")
    print(f"매핑: {out_dir / 'rename_map.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
