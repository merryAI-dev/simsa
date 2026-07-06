"""PostgreSQL 연결 + 스키마 적용 + 기본 팩 시드.

DATABASE_URL 은 환경변수 또는 .env 에서 읽는다. 로컬 기본값은 docker 컨테이너
simsa-postgres (포트 5544) 를 가리킨다.

usage: python db.py   # 스키마 적용 + koica-cts 팩 시드
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

BASE = Path(__file__).parent
DEFAULT_URL = "postgresql://simsa:simsa_local_dev@127.0.0.1:5544/simsa"

SEED_RULES = [
    # (doc_type, field, rule_type, instruction)
    ("*", "기관/법인명", "extract", "문서에 기재된 제출 기관(법인)명. 발급기관 이름이 아니라 신청 기업명."),
    ("*", "사업자등록번호", "extract", "000-00-00000 형식의 사업자등록번호."),
    ("사업자등록증", "대표자 성명", "extract", ""),
    ("사업자등록증", "개업연월일", "extract", ""),
    ("사업자등록증", "발급일", "extract", "문서 하단의 발급 날짜."),
    ("사업자등록증", "사업장 소재지", "extract", "여러 줄이면 전체를 감싼다."),
    ("사업자등록증", "사업의 종류", "extract", "업태·종목 항목별로 각각 탐지 항목을 만든다."),
    ("서약서", "서명·날인", "extract", "대표자의 자필서명 또는 도장. 값에는 종류(자필서명/도장/전자서명)를 쓰고, 박스는 서명·도장 표시 자체를 감싼다."),
    ("서약서", "서명 유효성", "verify", "(인) 표시나 서명란 위에 유효한 자필서명 또는 도장이 실제로 있는가. 낙서·X표·빈칸이면 fail, 애매한 자국이면 uncertain. 박스는 판단 근거 위치."),
    ("서약서", "작성일", "extract", "서약서에 기재된 작성 날짜."),
    ("서약서", "대표자 성명", "extract", "서명란에 적힌 성명."),
    ("공문", "문서번호", "extract", ""),
    ("공문", "시행일", "extract", ""),
    ("건강보험자격득실확인서", "성명", "extract", "확인서 대상자 성명."),
    ("건강보험자격득실확인서", "발급일", "extract", "발급 날짜. 심사 기준일로부터 3개월 이내여야 한다."),
]

# 종합 규칙(scope=submission): 파일별 추출이 끝난 값들을 텍스트로 놓고 제출건 단위로 평가
SEED_SUBMISSION_RULES = [
    # (name, instruction)
    ("대표자·서명자 일치", "사업자등록증의 대표자 성명과 각 서약서의 서명자/대표자 성명이 모두 동일 인물인지 확인한다."),
    ("기관명 일관성", "모든 문서의 기관/법인명이 같은 법인을 가리키는지 확인한다. 주식회사/(주)/㈜ 등 표기 변형은 같은 것으로 본다."),
    ("사업자등록번호 일치", "여러 문서에서 추출된 사업자등록번호가 모두 동일한지 확인한다."),
    ("증명서 발급일 유효", "건강보험자격득실확인서 등 증명서류의 발급일이 심사 기준일로부터 3개월 이내인지 확인한다."),
]

SEED_DOC_TYPES = [
    # (name, required, filename_hints, description)
    ("공문", True, ["공문"], "제출 공문 (기관 직인 포함)"),
    ("사업개요서", True, ["개요서"], ""),
    ("사업제안서", True, ["제안서"], ""),
    ("사업 예산계획서", True, ["예산"], "예산안·예산계획 엑셀/PDF"),
    ("결격사유 서약서", True, ["결격"], ""),
    ("CTS 공모서약서", True, ["공모서약"], ""),
    ("인권경영 실천서약서", True, ["인권경영"], ""),
    ("ODA 반부패 선언서", True, ["반부패"], ""),
    ("환경·사회·인권영향 스크리닝 체크리스트", True, ["스크리닝", "체크리스트"], ""),
    ("개인정보수집 및 이용동의서", True, ["개인정보"], ""),
    ("실적 증빙 자료", True, ["실적", "증빙"], "매출·투자·수상·특허·인증 등 실적 증빙 일체"),
    ("건강보험자격득실확인서", True, ["건강보험"], "참여인력별 1부, 발급 3개월 이내"),
    ("참여인력 참여 확인서", True, ["참여"], ""),
    ("이력서", False, ["이력서"], "외국인 인력 등 건강보험 확인서 대체 시"),
    ("사업자등록증", True, ["사업자등록증"], ""),
]


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        env = BASE / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("DATABASE_URL="):
                    url = line.split("=", 1)[1].strip()
    return url or DEFAULT_URL


def connect() -> psycopg.Connection:
    return psycopg.connect(database_url(), row_factory=dict_row)


def migrate() -> None:
    with connect() as conn:
        conn.execute((BASE / "schema.sql").read_text(encoding="utf-8"))
        backfill_legacy_feedback(conn)


def backfill_legacy_feedback(conn) -> None:
    """detection_events 도입 전에 쌓인 feedback 을 1회 소급 적재 (감사 로그 완결성).
    멱등: 이미 해당 detection_id 의 feedback 이벤트가 있으면 건너뛴다."""
    conn.execute(
        "INSERT INTO detection_events (submission_id, file_id, detection_id, event_type, "
        " page_no, rule_id, field, value, verdict, box, crop_path, confidence, model, "
        " prompt_version, feedback, corrected_value, created_at) "
        "SELECT f.submission_id, d.file_id, d.id, 'feedback', d.page_no, d.rule_id, d.field, "
        " d.value, d.verdict, d.box, d.crop_path, d.confidence, d.model, d.prompt_version, "
        " d.feedback, d.corrected_value, d.created_at "
        "FROM detections d JOIN files f ON f.id = d.file_id "
        "WHERE d.feedback <> '' "
        "AND NOT EXISTS (SELECT 1 FROM detection_events e "
        "                WHERE e.detection_id = d.id AND e.event_type = 'feedback')"
    )


def seed() -> None:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO packs (slug, name) VALUES ('koica-cts', 'KOICA CTS') "
            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id"
        ).fetchone()
        pack_id = row["id"]
        for doc_type, field, rule_type, instruction in SEED_RULES:
            conn.execute(
                "INSERT INTO rules (pack_id, doc_type, field, rule_type, instruction) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (pack_id, doc_type, field) DO NOTHING",
                (pack_id, doc_type, field, rule_type, instruction),
            )
        for name, instruction in SEED_SUBMISSION_RULES:
            conn.execute(
                "INSERT INTO rules (pack_id, doc_type, field, rule_type, scope, instruction) "
                "VALUES (%s, '종합', %s, 'verify', 'submission', %s) "
                "ON CONFLICT (pack_id, doc_type, field) DO NOTHING",
                (pack_id, name, instruction),
            )
        for name, required, hints, description in SEED_DOC_TYPES:
            conn.execute(
                "INSERT INTO doc_types (pack_id, name, required, filename_hints, description) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (pack_id, name) DO NOTHING",
                (pack_id, name, required, hints, description),
            )


if __name__ == "__main__":
    migrate()
    seed()
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM rules").fetchone()["n"]
    print(f"schema OK, rules={n}")
