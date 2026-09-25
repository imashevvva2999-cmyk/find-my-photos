-- One copy per image per event: the SHA-256 of each uploaded file is stored, and the same
-- file (byte for byte, whatever its name) cannot be added to the same event twice.
-- Photos uploaded before this migration keep NULL and are not affected.
ALTER TABLE photos ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_photos_event_content
    ON photos (event_id, content_sha256) WHERE content_sha256 IS NOT NULL;
