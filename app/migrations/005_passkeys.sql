-- Passkeys (WebAuthn) for the organiser: sign in with Face ID / Touch ID / Windows Hello or a
-- security key instead of typing the password. Only public keys are stored; the private key
-- never leaves the organiser's device. rp_id is the site address the passkey belongs to.
CREATE TABLE IF NOT EXISTS admin_passkeys (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    credential_id  BYTEA NOT NULL UNIQUE,
    public_key     BYTEA NOT NULL,
    sign_count     BIGINT NOT NULL DEFAULT 0,
    rp_id          TEXT NOT NULL,
    name           TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ
);

-- Every sign-in / add-passkey challenge is also recorded here and deleted when it is used, so an
-- answer can never be accepted twice (even if a session cookie and an answer were both copied).
CREATE TABLE IF NOT EXISTS passkey_challenges (
    challenge   BYTEA PRIMARY KEY,
    kind        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
