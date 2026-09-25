-- Server-side session revocation: admin sessions started before sessions_valid_after are
-- rejected (set on logout, so a copied session cookie stops working).
CREATE TABLE IF NOT EXISTS admin_state (
    key    TEXT PRIMARY KEY,
    value  TIMESTAMPTZ NOT NULL
);
