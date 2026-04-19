CREATE TABLE IF NOT EXISTS events (
  project_id              VARCHAR NOT NULL,
  trace_id                VARCHAR NOT NULL,
  observation_id          VARCHAR NOT NULL,
  parent_observation_id   VARCHAR,
  session_id              VARCHAR,
  user_id                 VARCHAR,
  environment             VARCHAR DEFAULT 'production',
  release                 VARCHAR,
  name                    VARCHAR NOT NULL,
  type                    VARCHAR NOT NULL,
  level                   VARCHAR DEFAULT 'DEFAULT',
  start_time              TIMESTAMP NOT NULL,
  end_time                TIMESTAMP,
  duration_ms             INTEGER,
  status                  VARCHAR DEFAULT 'unset',
  status_message          VARCHAR,
  input                   VARCHAR,
  output                  VARCHAR,
  input_ref               VARCHAR,
  output_ref              VARCHAR,
  model                   VARCHAR,
  model_parameters        MAP(VARCHAR, VARCHAR),
  prompt_tokens           INTEGER DEFAULT 0,
  completion_tokens       INTEGER DEFAULT 0,
  cache_read_tokens       INTEGER DEFAULT 0,
  cache_creation_tokens   INTEGER DEFAULT 0,
  total_tokens            INTEGER DEFAULT 0,
  input_cost_usd          DOUBLE DEFAULT 0,
  output_cost_usd         DOUBLE DEFAULT 0,
  total_cost_usd          DOUBLE DEFAULT 0,
  tool_definitions        MAP(VARCHAR, VARCHAR),
  tool_calls              VARCHAR[],
  tool_call_names         VARCHAR[],
  agent_id                VARCHAR,
  agent_role              VARCHAR,
  handoff_target_agent_id VARCHAR,
  prompt_name             VARCHAR,
  prompt_version          INTEGER,
  prompt_label            VARCHAR,
  metadata                MAP(VARCHAR, VARCHAR),
  tags                    VARCHAR[],
  event_version           BIGINT NOT NULL,
  ingested_at             TIMESTAMP NOT NULL,
  PRIMARY KEY (project_id, trace_id, observation_id, event_version)
);

CREATE INDEX IF NOT EXISTS events_by_time     ON events(project_id, start_time);
CREATE INDEX IF NOT EXISTS events_by_session  ON events(project_id, session_id);
CREATE INDEX IF NOT EXISTS events_by_user     ON events(project_id, user_id);
CREATE INDEX IF NOT EXISTS events_by_trace    ON events(project_id, trace_id);
CREATE INDEX IF NOT EXISTS events_by_status   ON events(project_id, status, start_time);
CREATE INDEX IF NOT EXISTS events_by_model    ON events(project_id, model);

CREATE OR REPLACE VIEW events_current AS
SELECT * FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, trace_id, observation_id ORDER BY event_version DESC) AS rn
  FROM events
) WHERE rn = 1;

CREATE TABLE IF NOT EXISTS scores (
  project_id        VARCHAR NOT NULL,
  trace_id          VARCHAR NOT NULL,
  observation_id    VARCHAR NOT NULL DEFAULT '',
  name              VARCHAR NOT NULL,
  value             DOUBLE,
  string_value      VARCHAR,
  source            VARCHAR NOT NULL,
  comment           VARCHAR,
  judge_model       VARCHAR,
  calibration_bias  DOUBLE DEFAULT 0,
  timestamp         TIMESTAMP NOT NULL,
  annotator_id      VARCHAR,
  eval_run_id       VARCHAR,
  PRIMARY KEY (project_id, trace_id, observation_id, name, timestamp)
);

CREATE TABLE IF NOT EXISTS causal_edges (
  project_id              VARCHAR NOT NULL,
  trace_id                VARCHAR NOT NULL,
  source_observation_id   VARCHAR NOT NULL,
  target_observation_id   VARCHAR NOT NULL,
  edge_type               VARCHAR NOT NULL,
  confidence              DOUBLE NOT NULL,
  method                  VARCHAR NOT NULL,
  evidence                VARCHAR,
  replay_supported        BOOLEAN NOT NULL,
  judge_model             VARCHAR,
  computed_at             TIMESTAMP NOT NULL,
  PRIMARY KEY (project_id, trace_id, source_observation_id, target_observation_id, method)
);

CREATE TABLE IF NOT EXISTS cost_daily (
  project_id  VARCHAR NOT NULL,
  date        DATE NOT NULL,
  user_id     VARCHAR NOT NULL DEFAULT '',
  agent_id    VARCHAR NOT NULL DEFAULT '',
  model       VARCHAR NOT NULL DEFAULT '',
  prompt_name VARCHAR NOT NULL DEFAULT '',
  total_cost_usd DOUBLE,
  total_tokens   BIGINT,
  call_count     BIGINT,
  PRIMARY KEY (project_id, date, user_id, agent_id, model, prompt_name)
);
