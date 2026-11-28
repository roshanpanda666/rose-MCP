# rose-phone-app

## What this codebase does

A phone PWA (`frontend/`) and an Android "Rose Voice" widget app (`android/`) talk to a FastAPI
backend (`backend/app/main.py`, 127.0.0.1:8790). The backend serves only the owner's tailnet, through
`tailscale serve`. It runs an LLM agent (Groq, `agent.py`) that calls ROSE MCP tools with ROSE's
"Phone app" static token, so anything this backend does runs as a ROSE client that skips the fingerprint
for everyday tools.

## Auth shape

- `require_token` dependency: `Authorization: Bearer <APP_TOKEN>`, checked with `hmac.compare_digest`.
  It is one shared token for every device. `/api/ping` is the only open route.
- The voice WebSocket's first message must be JSON `{"token": APP_TOKEN}`.
- `/api/approve` relays ROSE approval links. Android `ApproveActivity` opens fingerprint approvals.
- `ensure_app_token` generates `APP_TOKEN` into `backend/.env` (0600).

## Threat model

1. Getting to ROSE through this backend: a route missing `require_token`, an LLM agent tricked by
   prompt injection (notifications, web, screen text) into calling dangerous ROSE tools, or SSRF to
   ROSE or localhost.
2. Leaking `APP_TOKEN` or the ROSE token: in logs, frontend storage, error messages, intents, or
   notification deep links.
3. Android: exported activities or intents that start actions or approvals without the user.

## Project-specific patterns to flag

- An `@app.*` route without `dependencies=[Depends(require_token)]`.
- `/api/media/{name}`, `/api/apps/icon/{app_id}`: path traversal.
- Tool arguments the agent builds from untrusted content (notifications, clipboard, web) and passes on
  to ROSE tools.
- `subprocess` / shell use in `local_tools.py`, `apps.py`, `quick.py`, `browser.py`.
- Android `exported=true` components, and deep links (`notifications.py`) opened without an origin check.

## Known false-positives

- `backend/.env.example` holds placeholder values.
- Tests under `backend/tests/`.
- Binding to 127.0.0.1 is intended. Tailnet-only exposure is handled outside the code.
