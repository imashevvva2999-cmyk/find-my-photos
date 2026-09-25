# Deployment plan — Find My Photos («Мои фото»)

Status: **proposal, nothing deployed.** No photos, face data, database backups or secrets have
been sent to any external service. Nothing below is connected until you approve it.

Prices were checked on 25 Sep 2026 from the providers' public pages; confirm them when you sign up.

---

## 1. Can the whole application run on Vercel as it is? — No

| Part of the app | How it works today | On Vercel | Verdict |
|---|---|---|---|
| Web app (FastAPI + Jinja pages) | Long-running `uvicorn` process | Python Functions can run FastAPI (500 MB bundle limit, 2–4 GB memory) | ✅ possible |
| Face recognition (OpenCV + ONNX models, ~40 MB models) | Loaded once per process | Fits the 500 MB Python bundle; model load on every cold start (~1–2 s) | ⚠️ works, slower first request |
| **Photo uploads (admin)** | 6–19 MB per photo, sent to the server | **Function request limit is 4.5 MB** (413 `FUNCTION_PAYLOAD_TOO_LARGE`) | ❌ must be redesigned (direct-to-storage uploads) |
| Visitor selfie upload | Up to 15 MB | Now reduced in the browser to ≤ ~1.5 MB (done in this update) | ✅ fits |
| **Photo storage** | Files in `data/` on the Mac | **No persistent filesystem** on Functions; every file would be lost | ❌ needs object storage (Vercel Blob / R2 / S3) — code changes |
| Full-size "clean copy" downloads | Streamed by the server (can exceed 4.5 MB) | Function **response** limit is also 4.5 MB | ❌ must redirect to signed storage URLs |
| **Background worker** (finds faces, retries, retention purge) | Separate always-on process with a PostgreSQL queue | No always-on processes; functions stop after 300 s (Hobby) / 800 s (Pro) | ❌ must be rewritten for Vercel Queues (beta) + Cron |
| PostgreSQL | Local cluster | Works with any hosted Postgres (Neon via Vercel Marketplace) | ✅ |
| Migrations | `python -m app.migrate` | Run once per release from a trusted machine / CI, not from functions | ✅ with a manual step |
| Admin sessions, rate limits, signed links | Stored in PostgreSQL | Work unchanged | ✅ |
| Search cache (embeddings in memory) | Kept by the long-running process | Rebuilt per function instance (4 678 faces load in well under a second) | ✅ |
| Hobby plan | — | **Personal, non-commercial use only** | ⚠️ an event/conference service likely needs **Pro ($20/month)** |

**Conclusion:** running everything on Vercel is possible only after a significant rewrite (object
storage everywhere, direct browser-to-storage uploads, the worker rebuilt on Vercel Queues, which is
still in beta). Estimated 1–2 weeks of work plus retesting. It is not what I recommend for now.

---

## 2. Recommended setup — Vercel in front, a container backend behind it

```
 visitors ──► Vercel (your domain, HTTPS, static files, fonts)
                 │  rewrites: pages + search + gallery API
                 ▼
          Backend container (Render, Frankfurt)
          ├─ web: uvicorn (FastAPI, face search)
          ├─ worker: face processing + daily retention purge
          └─ persistent disk: photos, previews, thumbnails (20 GB)
                 │ private network
                 ▼
          Managed PostgreSQL (Render Postgres, Frankfurt)

 organiser ──► admin.<your-domain>  → backend directly (large uploads never pass through Vercel)

 backups (optional, needs your approval) ──► Cloudflare R2 (encrypted DB dump + photo copy)
```

Why this shape:
* The app keeps working exactly as tested here — the same code, the same worker, the same storage
  layout on a persistent disk. No risky rewrite.
* Vercel gives the domain, HTTPS and fast static files; it forwards (rewrites) dynamic requests to
  the backend. Visitor requests are small (selfies are now reduced in the browser).
* **Admin uploads go straight to the backend** (`admin.<domain>`), because Vercel does not document
  whether its 4.5 MB limit also applies to proxied requests.
* Everything in one EU region (Frankfurt) to keep latency low and data in one place.

Things to verify on a *private preview* before going public:
1. Photo previews and full-size downloads through the Vercel rewrite (responses above 4.5 MB).
   Fallback: download links point to the backend domain.
2. Vercel must not cache private responses (the app sends `no-store` / `private`).
3. Visitor rate limiting sees the real visitor IP (backend started with
   `--proxy-headers --forwarded-allow-ips=…`).

### Expected monthly cost (approximate, USD)

| Item | Plan | Cost |
|---|---|---|
| Vercel | Pro (commercial use) · Hobby only if personal/non-commercial | $20 · $0 |
| Backend (web + worker in one service, 2 GB RAM) | Render "Standard" | ~$25 |
| Persistent disk 20 GB (photos 7.5 GB today + growth) | Render disk (daily snapshots included) | ~$5 |
| PostgreSQL | Render Postgres smallest paid plan · or Neon Free (0.5 GB, enough for face data) | ~$6 · $0 |
| Off-site backups (optional) | Cloudflare R2 (10 GB free, no egress fees; $0.015/GB after) | ~$0–1 |
| **Total** | | **≈ $50–60 with Vercel Pro · ≈ $30–40 on Hobby** |

Render prices are from memory of their public price list and must be confirmed at sign-up (their
pricing page could not be read automatically). If 2 GB RAM is not enough during large uploads,
set `WORKER_THREADS=1` or move to the 4 GB plan (~$85).

### Alternatives

| Option | Monthly | Pros | Cons |
|---|---|---|---|
| **A. Recommended (above)** | ≈ $30–60 | Same tested code; managed backups/snapshots; little server admin | Two providers + Vercel |
| B. Everything on one backend host, no Vercel | ≈ $30–40 | Simplest; one bill; no proxy questions | Doesn't use Vercel |
| C. Budget VPS (e.g. Hetzner, EU) with Docker + Caddy HTTPS | ≈ €5–10 | Cheapest; plenty of RAM | You maintain OS updates, security, backups |
| D. All on Vercel (Functions + Queues + Blob + Neon) | ≈ $20–30 + usage | One platform, scales to zero | 1–2 weeks rewrite; depends on beta features; cold starts |

---

## 3. What I prepared in this update (no external services touched)

* Russian interface everywhere (visitor, camera/upload, consent, results, errors, admin).
* Event 7 renamed to «Технокадр» (same ID, same private link, 942 photos unchanged).
* Visitor selfies are reduced in the browser to ≤ 1600 px before sending (fits any request limit).
* `.gitignore`, `.dockerignore`, `.vercelignore` exclude `.env`, `data/`, `photos/`, `photo 2/`,
  database dumps and backups, so they cannot be committed or uploaded by accident.

Still to build **after you approve an option** (for A/B): a `Dockerfile` and start script (web +
worker, proxy headers), `vercel.json` with rewrites (A only), a data-migration script that copies
the database and the photo files to the new host, and a private preview test run.

---

## 4. Deployment checklist

**Before anything is uploaded (your decisions)**
- [ ] Choose option A, B, C or D.
- [ ] Personal or commercial use? (decides Vercel Hobby vs Pro)
- [ ] Domain name to use, and access to its DNS settings.
- [ ] Region (recommended: Frankfurt, EU).
- [ ] Explicit approval to upload **real event photos and face data** to the chosen providers —
      or start empty and upload the event again through the admin page.
- [ ] Backups: off-site copies to Cloudflare R2 yes/no; how long to keep them.
- [ ] Legal check: face data is biometric personal data. Confirm the consent text and hosting
      region meet the rules that apply to you and your guests (I can't give legal advice).
- [ ] Still open from earlier: may visitors search with an uploaded photo of someone else?
      (Recommended: camera-only by default with a per-event switch.)

**Accounts you would create (I never need your passwords)**
- [ ] Vercel (option A/D) — sign in with GitHub.
- [ ] GitHub, a **private** repository (or deploy from your Mac with the provider's CLI).
- [ ] Render (option A/B) — payment card for paid plans.
- [ ] Neon (only if you choose Neon instead of Render Postgres).
- [ ] Cloudflare (only for R2 backups or DNS).

**Environment variables for the backend** (values are set by you in the provider's dashboard;
never in the repository)

| Variable | Value |
|---|---|
| `DATABASE_URL` | Connection string of the hosted PostgreSQL (with `sslmode=require` if external) |
| `SECRET_KEY` | New random 64-character secret (not the one on this Mac) |
| `ADMIN_PASSWORD_HASH` | Created with `scripts/set_admin_password.py` — new password for production |
| `DATA_DIR` | Mount path of the persistent disk, e.g. `/var/data` |
| `COOKIE_SECURE` | `true` (HTTPS only) |
| `WORKER_THREADS` | `1` on a 2 GB instance |
| `EVENT_RETENTION_DAYS`, `RESULT_LINK_MINUTES`, `MAX_UPLOAD_MB`, `MAX_SELFIE_MB` | Keep current defaults unless you decide otherwise |
| `LOG_LEVEL` | `INFO` |

On Vercel (option A) only one setting is needed: the backend URL used by the rewrites
(`BACKEND_URL`). No secrets go to Vercel.

**Go-live steps (after approval)**
1. Create the database; run `python -m app.migrate` against it.
2. Deploy the backend privately; check `/healthz` and `/readyz` (database, migrations, models, worker).
3. Copy data (approved method) or re-upload the event; verify 942 photos / 4 678 faces.
4. Deploy Vercel preview with rewrites; run the preview checks listed in section 2.
5. Run the browser tests against the preview (gallery, viewer, camera, upload, search, admin).
6. Only then connect the domain and make it public.
7. Change the local database password on this Mac (exposed in a log earlier).
