"""매직 바이트 기반 파일 포맷 판별.

확장자는 신뢰하지 않는다. HWPX 확장자인데 실제로는 구형 HWP(OLE)인 파일,
HWP 확장자인데 실제로는 HWPX(ZIP)인 파일이 실무 제출물에 섞여 있다.

사용:
    from format_detect import detect_format
    info = detect_format(path)
    info.format      # 'hwp' | 'hwpx' | 'pdf' | 'docx' | 'xlsx' | 'pptx' | 'doc' | 'zip' | 'image' | 'unknown'
    info.mismatch    # 확장자와 실제 포맷이 다르면 True
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # HWP 5.x / DOC / XLS 등 OLE 컨테이너
ZIP_MAGIC = b"PK\x03\x04"                          # HWPX / OOXML / ZIP
PDF_MAGIC = b"%PDF"
HWP_STREAM_SIG = b"HWP Document File"              # HWP 5.x FileHeader 스트림 시그니처

IMAGE_MAGICS = {
    b"\xff\xd8\xff": "image",        # JPEG
    b"\x89PNG": "image",             # PNG
    b"GIF8": "image",                # GIF
    b"II*\x00": "image",             # TIFF LE
    b"MM\x00*": "image",             # TIFF BE
}

# 포맷별 정식 확장자 (mismatch 판정용)
CANONICAL_EXT = {
    "hwp": {".hwp"},
    "hwpx": {".hwpx"},
    "pdf": {".pdf"},
    "docx": {".docx"},
    "xlsx": {".xlsx"},
    "pptx": {".pptx"},
    "doc": {".doc", ".xls", ".ppt"},
    "zip": {".zip"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff"},
}


@dataclass
class FormatInfo:
    path: Path
    format: str          # 판별된 실제 포맷
    extension: str       # 파일명의 확장자 (소문자)
    mismatch: bool       # 확장자가 실제 포맷과 다른가
    detail: str = ""     # 판별 근거


def detect_format(path: str | Path) -> FormatInfo:
    path = Path(path)
    ext = path.suffix.lower()

    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        return FormatInfo(path, "unknown", ext, False, f"read error: {e}")

    fmt, detail = _classify(path, head)
    mismatch = fmt != "unknown" and ext not in CANONICAL_EXT.get(fmt, {ext})
    return FormatInfo(path, fmt, ext, mismatch, detail)


def _classify(path: Path, head: bytes) -> tuple[str, str]:
    if head.startswith(PDF_MAGIC):
        return "pdf", "%PDF magic"

    for magic, fmt in IMAGE_MAGICS.items():
        if head.startswith(magic):
            return fmt, "image magic"

    if head.startswith(OLE_MAGIC):
        # OLE 컨테이너: HWP 5.x인지 FileHeader 스트림 시그니처로 확인
        with open(path, "rb") as f:
            chunk = f.read(256 * 1024)
        if HWP_STREAM_SIG in chunk:
            return "hwp", "OLE + HWP Document File signature"
        return "doc", "OLE container (HWP 시그니처 없음 — DOC/XLS 계열 추정)"

    if head.startswith(ZIP_MAGIC):
        return _classify_zip(path)

    return "unknown", f"unrecognized magic: {head[:8].hex()}"


def _classify_zip(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return "unknown", "PK magic이지만 ZIP 파싱 실패 (손상 가능성)"

    if "Contents/content.hpf" in names or any(n.startswith("Contents/section") for n in names):
        return "hwpx", "ZIP + Contents/content.hpf"
    if "mimetype" in names:
        try:
            with zipfile.ZipFile(path) as zf:
                mt = zf.read("mimetype")[:64]
            if b"hwp" in mt:
                return "hwpx", f"ZIP + mimetype {mt.decode(errors='replace')}"
        except Exception:
            pass
    if "[Content_Types].xml" in names:
        if any(n.startswith("word/") for n in names):
            return "docx", "OOXML word/"
        if any(n.startswith("xl/") for n in names):
            return "xlsx", "OOXML xl/"
        if any(n.startswith("ppt/") for n in names):
            return "pptx", "OOXML ppt/"
    return "zip", "일반 ZIP 아카이브"


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        p = Path(arg)
        targets = sorted(p.rglob("*")) if p.is_dir() else [p]
        for t in targets:
            if t.is_file():
                info = detect_format(t)
                flag = "  << MISMATCH" if info.mismatch else ""
                print(f"{info.format:8s} [{info.extension}] {t.name}{flag}  ({info.detail})")
