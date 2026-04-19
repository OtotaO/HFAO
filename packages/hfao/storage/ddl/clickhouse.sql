CREATE TABLE IF NOT EXISTS events (
  project_id              LowCardinality(String),
  trace_id                String,
  observation_id          String,
  parent_observation_id   String,
  session_id              String,
  user_id                 String,
  environment             LowCardinality(String) DEFAULT 'production',
  release                 LowCardinality(String),
  name                    String,
  type                    LowCardinality(String),
  level                   LowCardinality(String) DEFAULT 'DEFAULT',
  start_time              DateTime64(3, 'UTC'),
  end_time                Nullable(DateTime64(3, 'UTC')),
  duration_ms             UInt32 DEFAULT 0,
  status                  LowCardinality(String) DEFAULT 'unset',
  status_message          String,
  input                   String CODEC(ZSTD(3)),
  output                  String CODEC(ZSTD(3)),
  input_ref               String,
  output_ref              String,
  model                   LowCardinality(String),
  model_parameters        Map(LowCardinality(String), String),
  prompt_tokens           UInt32 DEFAULT 0,
  completion_tokens       UInt32 DEFAULT 0,
  cache_read_tokens       UInt32 DEFAULT 0,
  cache_creation_tokens   UInt32 DEFAULT 0,
  total_tokens            UInt32 DEFAULT 0,
  input_cost_usd          Float64 DEFAULT 0,
  output_cost_usd         Float64 DEFAULT 0,
  total_cost_usd          Float64 DEFAULT 0,
  tool_definitions        Map(LowCardinality(String), String),
  tool_calls              Array(String),
  tool_call_names         Array(LowCardinality(String)),
  agent_id                LowCardinality(String),
  agent_role              LowCardinality(String),
  handoff_target_agent_id LowCardinality(String),
  prompt_name             LowCardinality(String),
  prompt_version          UInt32,
  prompt_label            LowCardinality(String),
  metadata                Map(LowCardinality(String), String),
  tags                    Array(LowCardinality(String)),
  event_version           UInt64,
  ingested_at             DateTime64(3, 'UTC'),
  INDEX idx_session    (project_id, session_id) TYPE bloom_filter GRANULARITY 1,
  INDEX idx_user       (project_id, user_id)    TYPE bloom_filter GRANULARITY 1,
  INDEX idx_status     (status)                 TYPE set(8)       GRANULARITY 1,
  INDEX idx_tools      tool_call_names          TYPE bloom_filter GRANULARITY 1,
  INDEX idx_tags       tags                     TYPE bloom_filter GRANULARITY 1,
  INDEX idx_agent      (project_id, agent_id)   TYPE bloom_filter GRANULARITY 1
)
ENGINE = ReplacingMergeTree(event_version)
PARTITION BY toYYYYMM(start_time)
ORDER BY (project_id, toStartOfHour(start_time), trace_id, observation_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS scores (
  project_id       LowCardinality(String),
  trace_id         String,
  observation_id   String,
  name             LowCardinality(String),
  value            Nullable(Float64),
  string_value     String,
  source           LowCardinality(String),
  comment          String,
  judge_model      LowCardinality(String),
  calibration_bias Float32 DEFAULT 0,
  timestamp        DateTime64(3, 'UTC'),
  annotator_id     String,
  eval_run_id      String,
  event_version    UInt64
)
ENGINE = ReplacingMergeTree(event_version)
PARTITION BY toYYYYMM(timestamp)
ORDER BY (project_id, toStartOfHour(timestamp), trace_id, observation_id, name);

CREATE TABLE IF NOT EXISTS causal_edges (
  project_id            LowCardinality(String),
  trace_id              String,
  source_observation_id String,
  target_observation_id String,
  edge_type             LowCardinality(String),
  confidence            Float32,
  method                LowCardinality(String),
  evidence              String,
  replay_supported      Bool,
  judge_model           LowCardinality(String),
  computed_at           DateTime64(3, 'UTC'),
  event_version         UInt64
)
ENGINE = ReplacingMergeTree(event_version)
PARTITION BY toYYYYMM(computed_at)
ORDER BY (project_id, trace_id, source_observation_id, target_observation_id, method);

CREATE MATERIALIZED VIEW IF NOT EXISTS cost_daily_mv
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(date)
ORDER BY (project_id, date, user_id, agent_id, model, prompt_name)
AS SELECT
  project_id,
  toDate(start_time) AS date,
  user_id,
  agent_id,
  model,
  prompt_name,
  sum(total_cost_usd) AS total_cost_usd,
  sum(total_tokens)   AS total_tokens,
  count()             AS call_count
FROM events
GROUP BY project_id, date, user_id, agent_id, model, prompt_name;
