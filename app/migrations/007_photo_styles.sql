-- Styled versions of each photo (B&W Editorial, Editorial Film), made by the worker in the background.
-- One photo stays one row: these columns only record whether its styled files are ready.
-- styles_version = the app.styles.STYLE_VERSION the files were made with (0 = not made yet).
ALTER TABLE photos ADD COLUMN IF NOT EXISTS styles_version   INT NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS styles_attempts  INT NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS styles_error     TEXT;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS styles_locked_at TIMESTAMPTZ;
ALTER TABLE photos ADD COLUMN IF NOT EXISTS styles_next_at   TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS photos_styles_todo ON photos (id) WHERE status = 'done';
