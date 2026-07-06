# simsa — 규칙 기반 범용 심사 팩 플랫폼

## Repo 경계 (중요)

- 이 repo 는 **범용 플랫폼(review_app)** 전용이다. KOICA CTS 전용 코드는
  **https://github.com/merryAI-dev/cts-screening** (로컬: `/Users/boram/Hwp/cts-screening`)에 있다.
- **이 repo 에 CTS 전용 로직을 추가하지 말 것**: web_app.py, excel_report.py, screen_batch.py,
  cross_check.py, normalize_files.py, criteria/, naming_rules.json, reference_data.json 은
  전부 cts-screening 소속이며 여기서는 삭제됐다.
- CTS 특화 요구가 들어오면 cts-screening 에서 작업하거나, 이 repo 에서는 "팩(packs/rules/doc_types)"
  데이터로만 표현한다 — 코드에 특정 프로그램 이름·기준을 하드코딩하지 않는다.
- `converter.py`/`format_detect.py`/`batch_convert.py`/`vlm_cache.py` 는 두 repo 가 각자 사본을
  유지한다 (의도된 분기, 동기화 도구 없음). 버그 수정 시 필요하면 양쪽에 따로 반영.

## 실행

- `./start_review.sh` — postgres 컨테이너(simsa-postgres, 포트 5544) 자동 기동 + 서버(포트 8766).
- 스키마는 schema.sql, 서버 시작 시 자동 migrate + koica-cts 팩 시드 (db.py).
- UI 는 review_ui.html 하나 (Tailwind CDN, 매 요청 로드 — 수정 즉시 반영, 빌드 없음).

## 규칙

- stdlib http.server 유지. 새 웹 프레임워크/빌드 도구 도입 금지.
- detections 는 "현재 상태 캐시", detection_events 는 "append-only 감사 이력" — 이력을 UPDATE/DELETE 하지 않는다.
- 지원기업 개인정보가 담기는 data/review/, cache/, *.xlsx 는 gitignore — 커밋 전 git status 확인.
