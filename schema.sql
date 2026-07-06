-- simsa 범용 심사 스키마. db.py 의 migrate() 가 서버 시작 시 적용한다.

CREATE TABLE IF NOT EXISTS packs (
  id serial PRIMARY KEY,
  slug text UNIQUE NOT NULL,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- 문서 유형 레지스트리: "이 심사에 어떤 서류가 들어와야 하는가" (완비 체크 기준).
-- filename_hints 는 보조 근거, VLM 내용 판별이 주 판별자.
CREATE TABLE IF NOT EXISTS doc_types (
  id serial PRIMARY KEY,
  pack_id int NOT NULL REFERENCES packs(id),
  name text NOT NULL,
  required boolean NOT NULL DEFAULT false,
  filename_hints text[] NOT NULL DEFAULT '{}',
  description text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (pack_id, name)
);

-- 탐지 규칙: "이 문서 유형에서 이 필드를 찾아라". doc_type '*' 는 모든 문서에 적용.
CREATE TABLE IF NOT EXISTS rules (
  id serial PRIMARY KEY,
  pack_id int NOT NULL REFERENCES packs(id),
  doc_type text NOT NULL,
  field text NOT NULL,
  instruction text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'draft',        -- draft | confirmed
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (pack_id, doc_type, field)
);

CREATE TABLE IF NOT EXISTS submissions (
  id serial PRIMARY KEY,
  pack_id int NOT NULL REFERENCES packs(id),
  name text NOT NULL,
  status text NOT NULL DEFAULT 'processing',   -- processing | ready | error
  error text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS files (
  id serial PRIMARY KEY,
  submission_id int NOT NULL REFERENCES submissions(id),
  filename text NOT NULL,
  pdf_path text NOT NULL DEFAULT '',
  doc_type text NOT NULL DEFAULT '',            -- VLM 이 판별한 문서 유형
  page_count int NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'pending',       -- pending | detected | skipped | error
  error text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pages (
  id serial PRIMARY KEY,
  file_id int NOT NULL REFERENCES files(id),
  page_no int NOT NULL,
  image_path text NOT NULL,
  width int NOT NULL,
  height int NOT NULL,
  UNIQUE (file_id, page_no)
);

-- 탐지 결과: box 는 0~1000 정규화 [ymin, xmin, ymax, xmax]
CREATE TABLE IF NOT EXISTS detections (
  id serial PRIMARY KEY,
  file_id int NOT NULL REFERENCES files(id),
  page_no int NOT NULL DEFAULT 1,
  rule_id int REFERENCES rules(id),
  field text NOT NULL,
  value text NOT NULL DEFAULT '',
  box int[] NOT NULL DEFAULT '{}',
  confidence real NOT NULL DEFAULT 0,
  model text NOT NULL DEFAULT '',
  feedback text NOT NULL DEFAULT '',            -- '' | correct | wrong
  corrected_value text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

-- 골든셋(#2): 사용자가 확정한 정답 판정. 골든 러너가 이 값과 파이프라인 출력을 대조한다.
CREATE TABLE IF NOT EXISTS golden_verdicts (
  id serial PRIMARY KEY,
  pack_id int NOT NULL REFERENCES packs(id),
  submission_id int NOT NULL REFERENCES submissions(id),
  file_id int NOT NULL REFERENCES files(id),
  field text NOT NULL,
  expected_value text NOT NULL DEFAULT '',
  verdict text NOT NULL,                        -- pass | fail | uncertain
  source_detection_id int REFERENCES detections(id),
  note text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (file_id, field)
);

-- 종합 검사 결과: scope='submission' 규칙을 추출값 텍스트 기반으로 평가한 판정 (제출건 단위)
CREATE TABLE IF NOT EXISTS submission_checks (
  id serial PRIMARY KEY,
  submission_id int NOT NULL REFERENCES submissions(id),
  rule_id int REFERENCES rules(id),
  name text NOT NULL,
  verdict text NOT NULL,                        -- pass | fail | uncertain
  evidence text NOT NULL DEFAULT '',
  refs jsonb NOT NULL DEFAULT '[]',             -- [{file, field, value}]
  feedback text NOT NULL DEFAULT '',            -- '' | correct | wrong
  created_at timestamptz NOT NULL DEFAULT now()
);

-- 골든셋 검사 실행 이력 (#2 골든 러너): 현재 규칙으로 골든 파일을 재탐지해 정답과 대조한 결과
CREATE TABLE IF NOT EXISTS golden_runs (
  id serial PRIMARY KEY,
  pack_id int NOT NULL REFERENCES packs(id),
  status text NOT NULL DEFAULT 'running',       -- running | done | error
  total int NOT NULL DEFAULT 0,
  matched int NOT NULL DEFAULT 0,
  results jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now()
);

-- 증분 마이그레이션 (IF NOT EXISTS 로 멱등)
ALTER TABLE rules ADD COLUMN IF NOT EXISTS rule_type text NOT NULL DEFAULT 'extract';     -- extract | verify
ALTER TABLE rules ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'file';             -- file(파일별 VLM 탐지) | submission(추출값 텍스트 종합)
ALTER TABLE detections ADD COLUMN IF NOT EXISTS verdict text NOT NULL DEFAULT '';          -- verify 규칙: pass | fail | uncertain
ALTER TABLE files ADD COLUMN IF NOT EXISTS doc_type_registered boolean NOT NULL DEFAULT false;
ALTER TABLE files ADD COLUMN IF NOT EXISTS doc_type_evidence text NOT NULL DEFAULT '';
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS base_date date NOT NULL DEFAULT CURRENT_DATE;  -- 심사 기준일 (날짜 판정 기준)
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS timings jsonb NOT NULL DEFAULT '{}';           -- {convert:[t0,t1], checks:[t0,t1]}
ALTER TABLE files ADD COLUMN IF NOT EXISTS timings jsonb NOT NULL DEFAULT '{}';                 -- {render:[t0,t1], detect:[t0,t1]}
ALTER TABLE detections ADD COLUMN IF NOT EXISTS crop_path text NOT NULL DEFAULT '';             -- AI 가 실제로 본 영역을 크롭한 증거 이미지
ALTER TABLE detections ADD COLUMN IF NOT EXISTS prompt_version text NOT NULL DEFAULT '';

-- 증거 감사 로그 (append-only): detections 는 "현재 상태 캐시", 이 테이블은 "불변 이력".
-- detections 에 UPDATE 가 일어날 때마다(탐지 생성, 사람 피드백) 스냅샷을 append 한다.
-- redetect 로 detections 행이 지워져도 crop_path·값을 denormalize 해뒀으므로 증거가 남는다.
CREATE TABLE IF NOT EXISTS detection_events (
  id serial PRIMARY KEY,
  submission_id int NOT NULL REFERENCES submissions(id),
  file_id int NOT NULL REFERENCES files(id),
  detection_id int REFERENCES detections(id) ON DELETE SET NULL,
  event_type text NOT NULL,          -- detected | feedback
  page_no int NOT NULL DEFAULT 1,
  rule_id int,
  field text NOT NULL DEFAULT '',
  value text NOT NULL DEFAULT '',
  verdict text NOT NULL DEFAULT '',
  box int[] NOT NULL DEFAULT '{}',
  crop_path text NOT NULL DEFAULT '',
  confidence real NOT NULL DEFAULT 0,
  model text NOT NULL DEFAULT '',
  prompt_version text NOT NULL DEFAULT '',
  feedback text NOT NULL DEFAULT '',
  corrected_value text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS detection_events_submission_created_idx
  ON detection_events (submission_id, created_at, id);
