# simsa — CTS 사전적격심사 자동화

KOICA CTS 지원사업 제출서류(ZIP)를 업로드하면 문서 변환 → Gemini VLM 심사 → 교차검증 → 심사표 엑셀 생성까지 자동으로 수행하는 로컬 웹 도구입니다.

제출 ZIP 안의 HWP/HWPX/PDF/XLSX 문서를 PDF로 변환하고, 문서별로 서명·날인·빈 양식·필수서류 누락 등을 판별해 제출건별 사전적격심사표를 만듭니다.

## 요구 환경

**macOS 전용**입니다 (`converter.py`의 LibreOffice/Chrome 경로가 맥 기준).

| 필수 | 용도 | 설치 |
|---|---|---|
| Python 3.13+ | 실행 런타임 | `brew install python@3.14` |
| LibreOffice | HWP/HWPX/XLSX → PDF 변환 | `brew install --cask libreoffice` |
| **H2Orestart 확장** | LibreOffice의 HWP 읽기 지원 (핵심) | 아래 참고 |
| Google Chrome | 일부 문서의 PDF 변환 경로 | 이미 있으면 됨 |
| Gemini API 키 | VLM 심사 | https://aistudio.google.com/apikey |

### H2Orestart 설치 (중요)

HWP 계열 변환이 전부 이 확장에 의존합니다. 없으면 라오스 건처럼 HWP 문서가 통째로 스킵됩니다.

1. https://github.com/ebandal/H2Orestart/releases 에서 `H2Orestart.oxt` 다운로드
2. LibreOffice 실행 → 도구 → 확장 관리자 → 추가 → oxt 선택 → LibreOffice 재시작
3. 설치 확인: `~/Library/Application Support/LibreOffice/4/user/uno_packages/` 아래에 확장이 보여야 함

> 변환기는 병렬 실행을 위해 격리 프로필을 쓰는데, **격리 프로필은 기본 프로필 사본으로 시드**해야 H2Orestart가 로드됩니다 (`converter.py`가 자동 처리 — 기본 프로필에 확장이 설치돼 있기만 하면 됨).

## 설치

```bash
git clone https://github.com/merryAI-dev/simsa.git
cd simsa
python3.14 -m pip install -r requirements.txt
echo "GEMINI_API_KEY=여기에_키" > .env
chmod 600 .env
```

`.env`는 gitignore에 포함돼 있습니다. **절대 커밋하지 마세요.** 키가 노출됐다면 즉시 재발급하세요.

## 실행

```bash
python3.14 web_app.py --host 127.0.0.1 --port 8765
```

브라우저에서 http://127.0.0.1:8765 접속:

1. 상단 힌트 줄에서 환경 체크 확인 — `Gemini OK · soffice OK · H2Orestart OK · py-deps OK`가 아니면 위 설치부터 해결
2. 제출 ZIP 업로드 (제출건별 폴더가 들어있는 묶음 ZIP, 폴더명 예: `국가_기관명_Seed1`)
3. 심사 시작 → 단계별 진행 표시 → 완료 후 XLSX / JSON / ZIP 다운로드

결과 파일은 `runs/<job_id>/results/`에도 남습니다.

### 파일명 정규화 (완료 폴더)

제출물 파일명이 제각각일 때, PDF 변환 + 규칙/VLM 분류로 표준 파일명을 붙여 `완료/` 폴더에 저장합니다.

```bash
python3.14 normalize_files.py 제출서류.zip                     # → 완료/<제출건>/<코드>. <유형>.pdf
python3.14 normalize_files.py 제출서류.zip --rules my_rules.json --out 결과폴더
```

- 파일명 규칙은 [naming_rules.json](naming_rules.json)에서 정의 (문서 유형·파일명 힌트·템플릿) — 프로그램마다 규칙 파일만 바꾸면 됨
- 원본 파일명 힌트로 먼저 매칭(API 비용 0), 못 잡는 파일만 VLM이 내용 보고 분류
- 분류 실패 파일은 `미분류_<원본명>.pdf`로 보존 (버리지 않음), 전체 매핑은 `완료/rename_map.json`에 기록

### CLI로 단계별 실행

```bash
python3.14 batch_convert.py 제출서류.zip converted/ 4     # 변환 (워커 4)
python3.14 screen_batch.py converted/ 3                   # VLM 심사 (워커 3)
python3.14 cross_check.py converted/screening_results_*.json  # 교차검증
python3.14 excel_report.py <screening.json> <xcheck.json> 심사표.xlsx
```

## 알아둘 것

- **첫 실행은 문서 전량을 Gemini에 호출**합니다 (제출 7건·77파일 기준 15~30분, API 비용 발생). 응답은 `cache/`에 캐시되어 재실행 시 재사용됩니다. 캐시는 지원서 데이터를 포함하므로 repo에 올라가지 않습니다.
- **판정 변동성**: borderline 항목 1~2건은 실행 간 pass↔uncertain이 흔들릴 수 있습니다. 빈 양식·날인 누락 같은 핵심 적발은 일관됩니다.
- **최종 판정은 참고용**입니다. `review`/`uncertain`/`외부 확인` 항목은 반드시 사람이 원본을 확인하세요.
- 지원기업 개인정보가 담기는 `converted/`, `runs/`, `cache/`, `*.xlsx`는 gitignore로 제외되어 있습니다. 커밋 전 `git status`로 확인하는 습관을 권장합니다.

## 구조

```
web_app.py        # 웹 UI + 잡 오케스트레이션 (아래 4개를 subprocess로 실행)
batch_convert.py  # ZIP 해제 + 문서→PDF 일괄 변환 (cp949 파일명 처리)
screen_batch.py   # 문서별 Gemini VLM 심사 (flash 1차 → pro 에스컬레이션, 재시도·캐시)
cross_check.py    # 제출건 단위 교차검증 (서명자↔대표자, 기관명, 필수서류 누락 등)
excel_report.py   # 사전적격심사표 XLSX 생성 (criteria_seed12 형식)
converter.py      # soffice+H2Orestart 변환 체인 (격리 프로필 시드)
format_detect.py  # 매직바이트 포맷 판별 (.hwpx 위장 구형 HWP 자동 보정)
vlm_screen.py     # Gemini 호출·프롬프트 (screen-v3)
vlm_cache.py      # VLM 응답 캐시 (프롬프트 버전 키, 오염 응답 차단)
criteria/         # 심사 기준 (Seed1/2, CTS-TIPS)
data/             # 기준일 등 참조 데이터
```
