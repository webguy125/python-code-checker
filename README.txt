Python Code Checker

This project is a Cloudflare-native split deployment:
- `frontend/` is the static Cloudflare Pages site.
- `worker/` is the Cloudflare Python Worker API for `/api/audit`.

Deployment model:
- Frontend: Cloudflare Pages
- Backend: Cloudflare Workers (Python runtime)

The frontend calls `POST /api/audit`.

Project layout:
- `frontend/index.html`, `style.css`, `script.js`: single-page UI
- `frontend/_redirects`: routes `/api/*` to the Worker hostname
- `worker/src/entry.py`: Worker request handling
- `worker/src/cleaner.py`: Python audit and cleanup logic

Deploy:
1. Deploy the Worker from `worker/` with `wrangler deploy`.
2. Update `frontend/_redirects` if the Worker hostname changes.
3. Deploy `frontend/` to Cloudflare Pages.

Local verification:
- Frontend JS syntax: `node --check frontend/script.js`
- Cleaner smoke test: `python -c "from worker.src.cleaner import clean_python_code; print(clean_python_code('import os\\nimport os\\nprint(\\\"hi\\\")\\n')[0])"`
