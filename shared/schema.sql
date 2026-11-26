CREATE SCHEMA IF NOT EXISTS config;
CREATE SCHEMA IF NOT EXISTS live;
CREATE SCHEMA IF NOT EXISTS history;

CREATE TABLE IF NOT EXISTS config.sensor_registry (
    sensor_id TEXT PRIMARY KEY,
    sensor_name TEXT NOT NULL,
    application_id TEXT,
    network_id TEXT,
    gateway_id TEXT,
    gateway_name TEXT,
    last_seen TIMESTAMPTZ NOT NULL,
    battery_level DOUBLE PRECISION,
    signal_strength DOUBLE PRECISION,
    voltage DOUBLE PRECISION,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS config.sensor_measurements (
    sensor_id TEXT NOT NULL REFERENCES config.sensor_registry(sensor_id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    label_index INTEGER NOT NULL,
    unit TEXT,
    last_seen TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(sensor_id,label,label_index)
);

CREATE TABLE IF NOT EXISTS config.users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin','user')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS config.machines (
    machine_id TEXT PRIMARY KEY,
    machine_name TEXT NOT NULL,
    machine_identifier TEXT NOT NULL,
    machine_group_number TEXT,
    machine_group_name TEXT,
    shift_start TIME NOT NULL DEFAULT '07:00',
    shift_end TIME NOT NULL DEFAULT '23:59:59',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    current_config_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE config.machines ADD COLUMN IF NOT EXISTS machine_group_number TEXT;
ALTER TABLE config.machines ADD COLUMN IF NOT EXISTS machine_group_name TEXT;

CREATE TABLE IF NOT EXISTS config.machine_sensors (
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    feature_name TEXT NOT NULL,
    sensor_id TEXT NOT NULL REFERENCES config.sensor_registry(sensor_id),
    measurement_label TEXT NOT NULL,
    measurement_index INTEGER NOT NULL,
    display_name TEXT,
    PRIMARY KEY(machine_id,feature_name)
);

CREATE TABLE IF NOT EXISTS config.machine_states (
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    state_key TEXT NOT NULL,
    state_name TEXT NOT NULL,
    metric_role TEXT NOT NULL DEFAULT 'other',
    display_order INTEGER NOT NULL,
    PRIMARY KEY(machine_id,state_key)
);

CREATE TABLE IF NOT EXISTS config.machine_model (
    machine_id TEXT PRIMARY KEY REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    model_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE config.machine_model DROP CONSTRAINT IF EXISTS machine_model_method_check;
ALTER TABLE config.machine_model ADD CONSTRAINT machine_model_method_check
CHECK (method IN ('logistic','rules','neural_network'));

CREATE TABLE IF NOT EXISTS config.metric_settings (
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    metric_key TEXT NOT NULL,
    selected BOOLEAN NOT NULL DEFAULT FALSE,
    parameters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(machine_id,metric_key)
);

CREATE TABLE IF NOT EXISTS config.configuration_versions (
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    snapshot_json JSONB NOT NULL,
    changed_by TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(machine_id,version)
);

CREATE TABLE IF NOT EXISTS history.sensor_readings (
    id BIGSERIAL PRIMARY KEY,
    timestamp_utc TIMESTAMPTZ NOT NULL,
    timestamp_local TIMESTAMPTZ NOT NULL,
    sensor_id TEXT NOT NULL REFERENCES config.sensor_registry(sensor_id),
    sensor_name TEXT NOT NULL,
    measurement_label TEXT NOT NULL,
    measurement_index INTEGER NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit TEXT,
    message_guid TEXT,
    raw_message JSONB,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_hist_sensor_lookup
ON history.sensor_readings(sensor_id,measurement_label,timestamp_utc DESC);

CREATE TABLE IF NOT EXISTS live.state_intervals (
    id BIGSERIAL PRIMARY KEY,
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    state_key TEXT NOT NULL,
    state_name TEXT NOT NULL,
    metric_role TEXT NOT NULL,
    started_at_utc TIMESTAMPTZ NOT NULL,
    started_at_local TIMESTAMPTZ NOT NULL,
    ended_at_utc TIMESTAMPTZ,
    ended_at_local TIMESTAMPTZ,
    config_version INTEGER NOT NULL,
    model_method TEXT NOT NULL,
    input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    probabilities_json JSONB,
    source_message_guid TEXT
);
CREATE INDEX IF NOT EXISTS ix_live_intervals_machine
ON live.state_intervals(machine_id,started_at_utc DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_one_open_interval
ON live.state_intervals(machine_id) WHERE ended_at_utc IS NULL;

CREATE TABLE IF NOT EXISTS live.neural_windows (
    id BIGSERIAL PRIMARY KEY,
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    window_end_utc TIMESTAMPTZ NOT NULL,
    window_end_local TIMESTAMPTZ NOT NULL,
    interval_seconds DOUBLE PRECISION NOT NULL DEFAULT 600,
    config_version INTEGER NOT NULL,
    input_json JSONB NOT NULL,
    output_json JSONB NOT NULL,
    state_seconds_json JSONB NOT NULL,
    source_message_guid TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(machine_id,window_end_utc)
);
CREATE INDEX IF NOT EXISTS ix_live_neural_windows
ON live.neural_windows(machine_id,window_end_utc DESC);

CREATE TABLE IF NOT EXISTS live.daily_metrics_cache (
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    local_date DATE NOT NULL,
    metric_key TEXT NOT NULL,
    metric_value DOUBLE PRECISION,
    text_value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(machine_id,local_date,metric_key)
);

CREATE TABLE IF NOT EXISTS live.hourly_usage_cache (
    machine_id TEXT NOT NULL REFERENCES config.machines(machine_id) ON DELETE CASCADE,
    local_date DATE NOT NULL,
    hour_of_day INTEGER NOT NULL CHECK (hour_of_day BETWEEN 0 AND 23),
    operational_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
    monitored_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
    utilization_pct DOUBLE PRECISION,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(machine_id,local_date,hour_of_day)
);

CREATE TABLE IF NOT EXISTS history.state_intervals (
    id BIGSERIAL PRIMARY KEY,
    machine_id TEXT NOT NULL,
    state_key TEXT NOT NULL,
    state_name TEXT NOT NULL,
    metric_role TEXT NOT NULL,
    started_at_utc TIMESTAMPTZ NOT NULL,
    started_at_local TIMESTAMPTZ NOT NULL,
    ended_at_utc TIMESTAMPTZ NOT NULL,
    ended_at_local TIMESTAMPTZ NOT NULL,
    duration_seconds DOUBLE PRECISION NOT NULL,
    config_version INTEGER NOT NULL,
    model_method TEXT NOT NULL,
    input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    probabilities_json JSONB
);

CREATE TABLE IF NOT EXISTS history.neural_windows (
    id BIGSERIAL PRIMARY KEY,
    machine_id TEXT NOT NULL,
    window_end_utc TIMESTAMPTZ NOT NULL,
    window_end_local TIMESTAMPTZ NOT NULL,
    interval_seconds DOUBLE PRECISION NOT NULL,
    config_version INTEGER NOT NULL,
    input_json JSONB NOT NULL,
    output_json JSONB NOT NULL,
    state_seconds_json JSONB NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(machine_id,window_end_utc)
);

CREATE TABLE IF NOT EXISTS history.daily_machine_metrics (
    machine_id TEXT NOT NULL,
    local_date DATE NOT NULL,
    machine_name TEXT NOT NULL,
    machine_identifier TEXT NOT NULL,
    machine_group_number TEXT,
    machine_group_name TEXT,
    last_setting_update TIMESTAMPTZ,
    last_setting_user TEXT,
    config_version INTEGER,
    estimation_model TEXT,
    sensor_count INTEGER,
    state_hours_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    availability_pct DOUBLE PRECISION,
    operational_utilization_pct DOUBLE PRECISION,
    active_efficiency_pct DOUBLE PRECISION,
    off_time_share_pct DOUBLE PRECISION,
    operational_time_share_pct DOUBLE PRECISION,
    idle_time_share_pct DOUBLE PRECISION,
    setup_time_share_pct DOUBLE PRECISION,
    processing_time_share_pct DOUBLE PRECISION,
    oee_pct DOUBLE PRECISION,
    operation_session_count INTEGER,
    avg_operation_session_min DOUBLE PRECISION,
    std_operation_session_min DOUBLE PRECISION,
    first_operation_time TIME,
    last_operation_time TIME,
    operating_span_hours DOUBLE PRECISION,
    peak_usage_hour INTEGER,
    peak_hour_utilization_pct DOUBLE PRECISION,
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(machine_id,local_date)
);
ALTER TABLE history.daily_machine_metrics ADD COLUMN IF NOT EXISTS machine_group_number TEXT;
ALTER TABLE history.daily_machine_metrics ADD COLUMN IF NOT EXISTS machine_group_name TEXT;
ALTER TABLE history.daily_machine_metrics ADD COLUMN IF NOT EXISTS last_setting_user TEXT;

CREATE TABLE IF NOT EXISTS history.hourly_machine_usage (
    machine_id TEXT NOT NULL,
    local_date DATE NOT NULL,
    hour_of_day INTEGER NOT NULL,
    operational_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
    monitored_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
    utilization_pct DOUBLE PRECISION,
    PRIMARY KEY(machine_id,local_date,hour_of_day)
);
