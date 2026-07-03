"""HWP/HWPX/Office 문서 → PDF 변환 (타임아웃 + fallback 체인).

체인:
  1. LibreOffice + H2Orestart (soffice --headless --convert-to pdf)
  2. hwp5html + Chrome headless PDF (hwp5html 설치 시에만, HWP 5.x 전용)
  실패 시 → ok=False, needs_review=True 로 반환 (침묵 실패 금지)

특징:
  - 변환 전에 format_detect 로 실제 포맷을 판별하고, 확장자가 틀린 파일은
    올바른 확장자의 임시 사본을 만들어 변환한다. ("File is not a zip file" 방지)
  - soffice 는 -env:UserInstallation 프로필 격리로 병렬 실행이 가능하다.
  - 변환 후 pypdf 로 페이지 수를 검증한다. 0페이지면 실패로 처리.

사용:
    from converter import convert_to_pdf
    result = convert_to_pdf(src, out_dir)
    if result.ok:
        result.pdf_path, result.pages, result.engine
    else:
        result.error  # needs_review 큐로 보낼 것
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from format_detect import detect_format

SOFFICE = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
# H2Orestart 확장은 사용자 프로필(uno_packages)에 설치돼 있으므로,
# 격리 프로필은 빈 디렉터리가 아니라 기본 프로필 사본으로 시드해야 HWP를 읽을 수 있다.
DEFAULT_LO_PROFILE = Path.home() / "Library/Application Support/LibreOffice/4"
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

CONVERTIBLE = {"hwp", "hwpx", "doc", "docx", "xlsx", "pptx"}


@dataclass
class ConversionResult:
    src: Path
    ok: bool
    pdf_path: Path | None = None
    engine: str = ""            # 'passthrough' | 'soffice' | 'hwp5html+chrome'
    pages: int = 0
    format: str = ""            # 판별된 실제 포맷
    mismatch: bool = False      # 확장자와 실제 포맷 불일치 여부
    needs_review: bool = False  # 자동판정 금지, 사람 확인 필요
    error: str = ""
    attempts: list[str] = field(default_factory=list)


def convert_to_pdf(src: str | Path, out_dir: str | Path, timeout: int = 180) -> ConversionResult:
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    info = detect_format(src)
    result = ConversionResult(src=src, ok=False, format=info.format, mismatch=info.mismatch)

    if info.format == "pdf":
        dest = out_dir / (src.stem + ".pdf")
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        result.ok, result.pdf_path, result.engine = True, dest, "passthrough"
        result.pages = _count_pages(dest)
        return result

    if info.format not in CONVERTIBLE:
        result.error = f"변환 대상 아님: {info.format} ({info.detail})"
        result.needs_review = True
        return result

    # 확장자가 실제 포맷과 다르면 올바른 확장자의 임시 사본으로 변환
    work_src = src
    tmp_holder = None
    if info.mismatch:
        tmp_holder = tempfile.TemporaryDirectory(prefix="cts_conv_")
        work_src = Path(tmp_holder.name) / (src.stem + "." + info.format)
        shutil.copy2(src, work_src)
        result.attempts.append(f"확장자 보정: {src.suffix} → .{info.format}")

    try:
        pdf = _try_soffice(work_src, out_dir, timeout, result)
        if pdf is None and info.format == "hwp" and shutil.which("hwp5html"):
            pdf = _try_hwp5html_chrome(work_src, out_dir, timeout, result)

        if pdf is not None:
            # 임시 사본 이름으로 생성됐어도 원본 stem 으로 정리
            final = out_dir / (src.stem + ".pdf")
            if pdf.resolve() != final.resolve():
                shutil.move(pdf, final)
            pages = _count_pages(final)
            if pages > 0:
                result.ok, result.pdf_path, result.pages = True, final, pages
            else:
                result.error = "변환은 됐지만 PDF 페이지가 0쪽 (내용 유실 의심)"
                result.needs_review = True
        else:
            result.needs_review = True
            if not result.error:
                result.error = "모든 변환 엔진 실패"
    finally:
        if tmp_holder:
            tmp_holder.cleanup()

    return result


def _try_soffice(src: Path, out_dir: Path, timeout: int, result: ConversionResult) -> Path | None:
    profile = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex[:8]}"
    if DEFAULT_LO_PROFILE.is_dir():
        shutil.copytree(DEFAULT_LO_PROFILE, profile, ignore=shutil.ignore_patterns(".lock"))
    cmd = [
        SOFFICE, "--headless", "--norestore",
        f"-env:UserInstallation=file://{profile}",
        "--convert-to", "pdf", "--outdir", str(out_dir), str(src),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        expected = out_dir / (src.stem + ".pdf")
        if proc.returncode == 0 and expected.exists() and expected.stat().st_size > 0:
            result.engine = "soffice"
            result.attempts.append("soffice: OK")
            return expected
        result.attempts.append(f"soffice 실패 (rc={proc.returncode}): {(proc.stderr or proc.stdout).strip()[:200]}")
    except subprocess.TimeoutExpired:
        result.attempts.append(f"soffice 타임아웃 ({timeout}s)")
    except OSError as e:
        result.attempts.append(f"soffice 실행 불가: {e}")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    result.error = "; ".join(result.attempts)
    return None


def _try_hwp5html_chrome(src: Path, out_dir: Path, timeout: int, result: ConversionResult) -> Path | None:
    chrome = next((c for c in CHROME_CANDIDATES if Path(c).exists()), None)
    if chrome is None:
        result.attempts.append("Chrome 없음 — hwp5html fallback 생략")
        return None
    with tempfile.TemporaryDirectory(prefix="cts_hwp5_") as td:
        html_dir = Path(td) / "html"
        try:
            subprocess.run(
                ["hwp5html", "--output", str(html_dir), str(src)],
                capture_output=True, text=True, timeout=timeout, check=True,
            )
            index = html_dir / "index.xhtml"
            if not index.exists():
                candidates = list(html_dir.glob("*.xhtml")) + list(html_dir.glob("*.html"))
                if not candidates:
                    result.attempts.append("hwp5html: 출력 HTML 없음")
                    return None
                index = candidates[0]
            pdf = out_dir / (src.stem + ".pdf")
            subprocess.run(
                [chrome, "--headless", "--disable-gpu", f"--print-to-pdf={pdf}",
                 "--no-pdf-header-footer", index.as_uri()],
                capture_output=True, text=True, timeout=timeout, check=True,
            )
            if pdf.exists() and pdf.stat().st_size > 0:
                result.engine = "hwp5html+chrome"
                result.attempts.append("hwp5html+chrome: OK")
                return pdf
            result.attempts.append("hwp5html+chrome: PDF 미생성")
        except subprocess.TimeoutExpired:
            result.attempts.append(f"hwp5html 체인 타임아웃 ({timeout}s)")
        except subprocess.CalledProcessError as e:
            result.attempts.append(f"hwp5html 체인 실패: {(e.stderr or '').strip()[:200]}")
    result.error = "; ".join(result.attempts)
    return None


def _count_pages(pdf: Path) -> int:
    try:
        import logging

        logging.getLogger("pypdf").setLevel(logging.ERROR)
        from pypdf import PdfReader
        return len(PdfReader(str(pdf)).pages)
    except Exception:
        # pypdf 없거나 파싱 실패 — 크기만으로 최소 검증
        return 1 if pdf.stat().st_size > 1024 else 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("usage: python converter.py <input file> <output dir>")
        sys.exit(1)
    r = convert_to_pdf(sys.argv[1], sys.argv[2])
    status = "OK" if r.ok else "FAIL"
    print(f"[{status}] {r.src.name} → {r.pdf_path} ({r.engine}, {r.pages}p, format={r.format}"
          f"{', ext mismatch' if r.mismatch else ''})")
    if not r.ok:
        print("  error:", r.error)
    for a in r.attempts:
        print("  -", a)
