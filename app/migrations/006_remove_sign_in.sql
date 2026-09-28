-- The site has no sign-in any more (a public one-time site): the tables that only served the
-- organiser sign-in are removed. Events, photos, faces and search statistics are not touched.
DROP TABLE IF EXISTS passkey_challenges;
DROP TABLE IF EXISTS admin_passkeys;
DROP TABLE IF EXISTS admin_state;
DELETE FROM rate_limits WHERE key LIKE 'login:%';
