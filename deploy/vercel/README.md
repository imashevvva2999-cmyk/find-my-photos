Vercel serves only this folder: `vercel.json` forwards every request to the backend on Render
and never caches responses (private photos). The organiser area (`/admin`) is sent straight to
the backend, because large photo uploads must not pass through Vercel (4.5 MB request limit).
If Render gives the backend a different address, replace `findmyphotos-backend.onrender.com`.
