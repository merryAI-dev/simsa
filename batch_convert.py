"""제출 ZIP 묶음 → PDF 일괄 변환.

입력은 셋 중 하나:
  - 바깥 묶음 ZIP 하나 (안에 제출건 ZIP 들이 든 폴더)
  - 제출건 ZIP 들이 들어있는 폴더
  - 이미 풀린 제출건 폴더들의 상위 폴더

제출건마다:
  - HWP/HWPX/DOC/DOCX → PDF 변환 (converter.py 체인)
  - PDF → 그대로 복사
  - 그 외(XLSX, 이미지 등) → 원본 그대로 복사 (변환 안 함)
결과는 출력폴더/<제출건명>/ 에 저장하고, summary.json 에 건별 성공/실패를 기록한다.
변환 실패 파일은 needs_review 로 표시된다 — 침묵 실패 없음.

usage: python batch_convert.py <input_dir> <output_dir> [workers]
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from converter import CONVERTIBLE, convert_to_pdf
from format_detect import detect_format

SKIP_NAMES = {".DS_Store", "__MACOSX"}


def convert_submission(name: str, files: list[Path], out_dir: Path, workers: int) -> list[dict]:
    dest = out_dir / name
    dest.mkdir(parents=True, exist_ok=True)
    records = []

    def one(f: Path) -> dict:
        info = detect_format(f)
        if info.format in CONVERTIBLE or info.format == "pdf":
            r = convert_to_pdf(f, dest)
            if info.format == "xlsx":
                # 엑셀→PDF 는 열 잘림 위험이 있어 원본도 함께 보존
                shutil.copy2(f, dest / f.name)
            return {
                "file": f.name, "format": r.format, "mismatch": r.mismatch,
                "ok": r.ok, "pdf": r.pdf_path.name if r.pdf_path else None,
                "pages": r.pages, "engine": r.engine,
                "needs_review": r.needs_review, "error": r.error,
            }
        shutil.copy2(f, dest / f.name)
        return {"file": f.name, "format": info.format, "mismatch": info.mismatch,
                "ok": True, "pdf": None, "pages": 0, "engine": "copy",
                "needs_review": False, "error": ""}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(one, files))
    return records


def collect_submissions(input_dir: Path, tmp: Path) -> dict[str, list[Path]]:
    """ZIP 은 임시폴더에 풀고, 폴더는 그대로. {제출건명: 파일목록}"""
    subs: dict[str, list[Path]] = {}
    for entry in sorted(input_dir.iterdir()):
        if entry.name in SKIP_NAMES or entry.name.startswith("."):
            continue
        if entry.is_dir():
            subs[entry.name] = _walk(entry)
        elif entry.suffix.lower() == ".zip":
            target = tmp / entry.stem
            try:
                # cp949: Windows 제작 ZIP 의 한글 파일명 보호 (UTF-8 플래그 항목엔 무영향)
                with zipfile.ZipFile(entry, metadata_encoding="cp949") as zf:
                    zf.extractall(target)
            except (zipfile.BadZipFile, UnicodeDecodeError) as e:
                print(f"[SKIP] {entry.name}: ZIP 해제 실패 — {e}")
                continue
            subs[entry.stem] = _walk(target)
    if not subs:
        # 하위 폴더/ZIP 없이 파일만 있으면 폴더 자체를 제출건 하나로 취급
        files = _walk(input_dir)
        if files:
            subs[input_dir.name] = files
    return subs


def _walk(root: Path) -> list[Path]:
    return [f for f in sorted(root.rglob("*"))
            if f.is_file() and f.name not in SKIP_NAMES
            and "__MACOSX" not in f.parts and not f.name.startswith("._")]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    input_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {}
    with tempfile.TemporaryDirectory(prefix="cts_batch_") as td:
        input_dir = input_path
        if input_path.is_file() and input_path.suffix.lower() == ".zip":
            # 바깥 묶음 ZIP: 풀고, 단일 최상위 폴더면 그 안으로 진입
            outer = Path(td) / "_outer"
            # 한국 Windows ZIP 은 cp949, macOS/리눅스 ZIP 은 UTF-8 파일명
            try:
                with zipfile.ZipFile(input_path, metadata_encoding="cp949") as zf:
                    zf.extractall(outer)
            except UnicodeDecodeError:
                with zipfile.ZipFile(input_path, metadata_encoding="utf-8") as zf:
                    zf.extractall(outer)
            tops = [p for p in outer.iterdir() if p.name not in SKIP_NAMES]
            input_dir = tops[0] if len(tops) == 1 and tops[0].is_dir() else outer
        subs = collect_submissions(input_dir, Path(td))
        for name, files in subs.items():
            print(f"\n=== {name} ({len(files)} files) ===")
            records = convert_submission(name, files, out_dir, workers)
            summary[name] = records
            for r in records:
                mark = "OK " if r["ok"] else "FAIL"
                mm = " [ext mismatch]" if r["mismatch"] else ""
                extra = f"→ {r['pdf']} ({r['pages']}p, {r['engine']})" if r["pdf"] else f"({r['engine']})"
                print(f"  [{mark}] {r['file']}{mm} {extra}")
                if r["error"]:
                    print(f"         {r['error']}")

    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(len(v) for v in summary.values())
    fails = [(s, r) for s, v in summary.items() for r in v if not r["ok"]]
    print(f"\n총 {len(summary)}건 제출, {total}개 파일 / 실패(확인필요) {len(fails)}개")
    for s, r in fails:
        print(f"  - {s} / {r['file']}: {r['error']}")
    print(f"summary: {out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
