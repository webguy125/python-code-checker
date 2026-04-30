Python Code Checker

This project is a Cloudflare-native split deployment:
- `frontend/` is the static Cloudflare Pages site.
- `worker/` is the Cloudflare Python Worker API for `/audit`.

Deployment model:
- Frontend: Cloudflare Pages
- Backend: Cloudflare Workers (Python runtime)

The frontend calls the deployed Worker URL directly with `POST /audit`.
It can also request an optional mock test run of the repaired script by sending `run_mock: true`.
The mock test auto-calls discovered zero-argument user-defined functions after executing the repaired script.

Project layout:
- `frontend/index.html`, `style.css`, `script.js`: single-page UI
- `worker/src/entry.py`: Worker request handling
- `worker/src/cleaner.py`: Python audit and cleanup logic
- `worker/src/runner.py`: restricted mock execution sandbox for repaired code

Deploy:
1. Deploy the Worker from `worker/` with `uv run pywrangler deploy`.
2. Update `frontend/script.js` if the Worker hostname changes.
3. Deploy `frontend/` to Cloudflare Pages.

Local verification:
- Frontend JS syntax: `node --check frontend/script.js`
- Cleaner smoke test: `python -c "from worker.src.cleaner import clean_python_code; print(clean_python_code('import os\\nimport os\\nprint(\\\"hi\\\")\\n')[0])"`
