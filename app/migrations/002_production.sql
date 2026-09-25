-- Retention: every event has an expiry date; its photos and face data are deleted after it.
ALTER TABLE events ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
-- Existing events get 90 days from NOW (never a date in the past): an old event must not be
-- deleted by the first clean-up run just because it was created long ago. The admin can
-- then shorten or extend it.
UPDATE events SET expires_at = GREATEST(created_at, now()) + interval '90 days' WHERE expires_at IS NULL;
ALTER TABLE events ALTER COLUMN expires_at SET NOT NULL;
-- Bumped whenever an event's faces change, so search caches know when to reload.
ALTER TABLE events ADD COLUMN IF NOT EXISTS index_version BIGINT NOT NULL DEFAULT 0;

-- Durable job queue for the worker process.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE photos ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- True once the metadata-free copy that visitors download exists.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS has_display BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_photos_queue ON photos (next_attempt_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_photos_processing ON photos (locked_at) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_photos_event_updated ON photos (event_id, updated_at, id);
CREATE INDEX IF NOT EXISTS idx_events_expires ON events (expires_at);

-- Photos processed before this migration have no metadata-free download copy yet:
-- queue them once more (faces are recomputed identically).
UPDATE photos SET status = 'pending', next_attempt_at = now(), updated_at = now()
WHERE status IN ('done', 'processing') AND NOT has_display;

-- Shared rate limiting (login attempts, searches) for every web process.
CREATE TABLE IF NOT EXISTS rate_limits (
    key           TEXT NOT NULL,
    window_start  TIMESTAMPTZ NOT NULL,
    count         INTEGER NOT NULL,
    PRIMARY KEY (key, window_start)
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_window ON rate_limits (window_start);

-- Worker liveness, shown by /readyz.
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    name        TEXT PRIMARY KEY,
    pid         INTEGER NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    beat_at     TIMESTAMPTZ NOT NULL,
    processed   BIGINT NOT NULL DEFAULT 0
);

-- Search statistics: outcome, number of results and duration only.
-- No IP address, no image, no face data.
CREATE TABLE IF NOT EXISTS search_log (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id     BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome      TEXT NOT NULL,
    results      INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_log_at ON search_log (at);
CREATE INDEX IF NOT EXISTS idx_search_log_event ON search_log (event_id, at);
