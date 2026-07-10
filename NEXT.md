# NEXT — simsa 이어받기

## 지금 상태

- repo: `/Users/boram/Hwp/cts_screening`
- remote: `https://github.com/merryAI-dev/simsa`
- 역할: 범용 심사 팩 플랫폼. CTS 전용 파이프라인은 `/Users/boram/Hwp/cts-screening` / `merryAI-dev/cts-screening`.
- 최신 주요 기능:
  - 새 심사 온보딩: 팩 생성 -> 샘플 ZIP -> 서류 유형 집합 파악 -> 규칙 제안 -> 사람 확정.
  - 민감 규칙: `rules.sensitive=true`면 값 마스킹 + crop 미저장.
  - 개표 참관 스트리밍: `/api/submissions/:id/progress?after=<file_id>` 커서 증분 폴링 + UI FIFO 재생 큐.
  - 증거 감사 로그: `detection_events` append-only, `detections`는 현재 상태 캐시.

## 절대 경계

- CTS 관련 요구는 `cts-screening` repo에서 처리한다.
- 이 repo에 CTS 파일/기준/기업명/국가표를 다시 넣지 않는다.
- 여기서는 프로그램 특화 내용도 `packs/doc_types/rules` 데이터로만 표현한다.

## 실행

```bash
cd /Users/boram/Hwp/cts_screening
./start_review.sh
# http://127.0.0.1:8766
```

필요 조건:
- Docker Desktop 실행 중 (`simsa-postgres`, 포트 5544)
- `.env`에 `GEMINI_API_KEY` (커밋 금지)

## 빠른 검증

서버 상태:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8766/api/bootstrap
```

스트리밍 증분 API:

```bash
curl -s "http://127.0.0.1:8766/api/submissions/8/progress?after=0" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], len(d["new_files"]), bool(d["current"]))'
```

완료 뷰 회귀:

```bash
curl -s http://127.0.0.1:8766/api/submissions/8 \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], len(d["feed"]), len(d["checks"]), len(d["checklist"]))'
```

## 다음으로 하면 좋은 것

1. **취소 버튼**: 처리 중 제출건을 서버 중단 없이 graceful cancel.
   - 현재 긴급 취소는 서버 stop 또는 DB status 변경.
   - 최소 구현: `submissions.cancel_requested boolean` + `process_submission()` 파일 사이 체크.
2. **온보딩 규칙 제안 프롬프트 정리**:
   - 샘플의 특정 금액/환율 같은 값이 규칙 설명에 들어오는 것을 더 강하게 금지.
3. **스트리밍 UI 실사용 검증**:
   - 캐시 없는 작은 샘플 ZIP으로 실제 VLM 호출 중 `current` 화면이 충분히 보이는지 확인.
   - 빠른 캐시 히트에서는 재생 큐가 350~1400ms로 따라잡는 것이 정상.

## 주요 파일

- `review_app.py`
  - `/api/submissions/:id/progress`: 경량 커서 증분 스트리밍.
  - `detect_file()`: 페이지 렌더 commit -> VLM 탐지 -> crop 저장 -> 이벤트 append.
  - `process_onboarding()`: 샘플 ZIP 기반 유형 집합/규칙 제안.
- `review_ui.html`
  - `startStream()`, `pollProgress()`, `playNext()`: FIFO 재생 큐.
  - `currentScreen()`, `feedScreen()`, `feedLogRow()`: 개표 참관 UI.
- `detect_fields.py`
  - `PROMPT_VERSION = "detect-v3"`.
  - `[민감]` 규칙 마스킹 지시.
- `schema.sql`
  - `detection_events`, `rules.sensitive`, `packs.status`, `submissions.kind`.

