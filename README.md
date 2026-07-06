# simsa — 규칙 기반 범용 심사 팩 플랫폼

업로드 → 문서 유형 판별 → 규칙 기반 필드 탐지(box_2d) → 검토 → 골든셋 확정까지, 팩(규칙 세트)만
갈아끼우면 어떤 서류 심사에도 쓸 수 있는 플랫폼입니다. 규칙·탐지·정답은 PostgreSQL에 저장됩니다.

> **KOICA CTS 전용 파이프라인(문서 변환·VLM 심사·엑셀 리포트)은
> [merryAI-dev/cts-screening](https://github.com/merryAI-dev/cts-screening)으로 분리됐습니다.**
> `web_app.py`/`excel_report.py`/`screen_batch.py`/`cross_check.py`/`normalize_files.py`와
> 공유 모듈(`converter.py`/`format_detect.py`/`vlm_cache.py`/`vlm_screen.py`/`batch_convert.py`)의
> 사본이 그 repo로 옮겨졌습니다. 이 repo에는 당분간 원본이 그대로 남아있지만
> **앞으로 CTS 관련 작업은 새 repo에서** 하고, 이 repo의 사본은 정리될 예정입니다
> ([이슈 #4](https://github.com/merryAI-dev/simsa/issues/4) 참고). 아래는 simsa 플랫폼(review_app) 설명입니다.

## 요구 환경

**macOS 전용**입니다. Docker Desktop(PostgreSQL 컨테이너)과 Gemini API 키가 필요합니다.
https://aistudio.google.com/apikey

```bash
python3.14 -m pip install -r requirements.txt   # psycopg 포함
echo "GEMINI_API_KEY=여기에_키" > .env && chmod 600 .env
./start_review.sh                               # postgres 자동 기동 → http://127.0.0.1:8766
```

`.env`는 gitignore에 포함돼 있습니다. **절대 커밋하지 마세요.** 키가 노출됐다면 즉시 재발급하세요.

- **DB**: 컨테이너 `simsa-postgres` (호스트 포트 5544, 데이터는 `simsa_pgdata` 볼륨에 영속,
  부팅 시 자동 시작). 접속 문자열은 `.env` 의 `DATABASE_URL` (없으면 로컬 기본값 사용).
  스키마([schema.sql](schema.sql))와 koica-cts 팩 시드는 서버 시작 시 자동 적용됩니다.
- **문서 유형 레지스트리**: "어떤 서류가 들어와야 하는가"의 기준. VLM 내용 판별이 주,
  파일명 힌트는 보조. 필수 유형이 빠지면 제출건 화면에 누락 경고가 뜨고,
  레지스트리에 없는 문서는 미등록 배너 → 클릭 한 번으로 등록됩니다.
- **규칙 종류**: `extract` 는 값+위치 추출(업태·종목처럼 값이 여러 개면 항목별 박스),
  `verify` 는 pass·fail·uncertain 판정 + 근거 위치(서명 유효성 등). 파일 화면의
  "규칙 제안 받기"를 누르면 VLM 이 그 문서에서 점검할 규칙을 제안합니다.
- **골든셋**: 검토 화면에서 확정한 판정이 `golden_verdicts` 에 쌓입니다 (#2 골든 러너의 정답 데이터).

```
review_app.py     # 검토 서버: 업로드·검토 UI + API (포트 8766)
detect_fields.py  # 유형 판별 + 필드 탐지 (Gemini box_2d, detect-v2 프롬프트)
db.py             # PostgreSQL 연결·마이그레이션·시드 (koica-cts 팩)
schema.sql        # packs/doc_types/rules/submissions/files/pages/detections/golden_verdicts
start_review.sh   # 로컬 원커맨드 실행 (postgres 컨테이너 자동 기동)
```
