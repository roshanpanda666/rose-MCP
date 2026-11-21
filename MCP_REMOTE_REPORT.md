# ROSE remote MCP connector and FreeLLMAPI integration: build and verification report

*2026-11-21 · branch `tree-first` · written by Forge (Claude Code)*

## 1. Summary

| Item | State |
| --- | --- |
| ROSE as a remote **Streamable HTTP** MCP connector via **ngrok** | **Working and verified.** 22/22 protocol and auth checks passed over `https://washhouse-serotonin-pennant.ngrok-free.dev/mcp`, and 22/22 locally. |
| FreeLLMAPI installed and running | **Done.** It is its own service on `127.0.0.1:3001`, with its own `.env`, database and key. Its MCP endpoint has 13 tools. |
| FreeLLMAPI as a separate connector for Claude Code | **Done.** `freellmapi` → `http://127.0.0.1:3001/mcp` (user scope) shows ✔ Connected. |
| FreeLLMAPI as extra tools inside ROSE (`freellm_*`, 13 tools) | **Live** (deployed 2026-11-21 at your request). The public URL now serves 86 tools; probe 22/22 again. |
| Caveman (token-saving reply style) | **Live in Claude Code** as plugin `caveman@caveman` 4.1.0. The Claude app needs one manual step (§8). |
| Existing ROSE setup: auth, ports, ngrok, units, config | **Unchanged.** Same URL, same OAuth clients and tokens, same ports and units. The server was redeployed from this branch (tree-first + bridge). All temporary test credentials were revoked. |

Per your instruction, nothing that overlaps the existing setup was changed. Three things need your decision; they're listed in **§6 Flagged for Roshan**.

---

## 2. Architecture

```
 claude.ai / phone ──HTTPS──► ngrok edge (washhouse-serotonin-pennant.ngrok-free.dev)
                                   │  (ngrok agent, run by rose-tunnel.service)
                                   ▼
 Claude Code (Forge) ──HTTP──► 127.0.0.1:8765  rose serve   (rose.service)
                               │  OAuth 2.1 + static bearer tokens, guardrails, 72 tools (live)
                               │
                               └─(after deploy: freellm_* tools)──┐
                                                                  ▼
 Claude Code ──HTTP + unified key──────────────────────────► 127.0.0.1:3001  FreeLLMAPI (freellmapi.service)
   (separate "freellmapi" connector)                          /v1 OpenAI-compatible, /mcp (13 tools)
                                                              └──► free-tier LLM providers (outbound)
```

Every listener is loopback-only (`127.0.0.1:8765`, `127.0.0.1:3001`, ngrok's API on `127.0.0.1:4040`). The ngrok tunnel is the only way in from outside, and it only reaches ROSE, which always requires auth.

## 3. ROSE as a remote Streamable HTTP connector

### 3.1 How it works

- **Server.** `rose serve` (in [rose/server.py](rose/server.py), `run_http`) builds an `MCPServer` from the official MCP Python SDK 2.x and serves `streamable_http_app(json_response=True)` with uvicorn on `127.0.0.1:8765`.
  - Each POST to `/mcp` gets a plain JSON response, which survives any proxy.
  - `GET /mcp` opens the optional server-to-client SSE stream.
  - Sessions use `Mcp-Session-Id` and end with `DELETE`.
- **Auth.** [rose/auth.py](rose/auth.py) is the OAuth 2.1 authorization server and the resource server in one:
  - **Discovery.** `/.well-known/oauth-protected-resource/mcp` (RFC 9728) and `/.well-known/oauth-authorization-server` (RFC 8414).
  - **401 challenge.** `WWW-Authenticate: Bearer … resource_metadata=…`, which is how claude.ai finds the auth server.
  - **Client registration.** Dynamic client registration at `/register` is limited to redirect hosts in `oauth_redirect_hosts` (claude.ai, claude.com, localhost).
  - **Authorization.** `/authorize` uses PKCE (S256). It parks the request and sends the browser to ROSE's own `/rose/login` page, where you approve with your passphrase (scrypt-hashed; 5 failures lock the page for 15 min).
  - **Tokens.** Access tokens last 1 h. Refresh tokens last 30 days and rotate on every use. Static bearer tokens (`rose auth new-token`) serve Claude Code and scripts. All tokens are stored SHA-256-hashed in `~/rose/auth/state.json` (0600).
  - **Request checks.** Host and Origin are validated by the SDK's `TransportSecuritySettings` to block DNS rebinding and foreign origins.
- **Guardrails.** [rose/guard.py](rose/guard.py) wraps every tool with:
  - lockdown;
  - a 150 calls/min limit per remote client;
  - out-of-band passphrase approval links for remote `confirm=true`, GUI input and webcam;
  - untrusted-content labels plus an injection tripwire;
  - logging to `~/rose/logs/`.
- **Tunnel.** [rose/tunnel.py](rose/tunnel.py) (`rose tunnel`, run by `rose-tunnel.service`) starts `ngrok http 127.0.0.1:8765 --url https://<static-domain>`.
  - It waits for ngrok's local API to list the tunnel and writes the public URL to `~/rose/runtime.json`. The server uses that URL as its OAuth issuer.
  - It **fails closed**: the tunnel is torn down unless an unauthenticated `POST /mcp` through the public URL returns **401**.
- **Services** (systemd user units): `rose.service` (server), `rose-tunnel.service` (ngrok, `Wants=rose.service` so reloads don't bounce the tunnel), `rose-watch.service` (watchers and schedules).

### 3.2 Verification (live, over ngrok)

The probe ([scripts/mcp_probe.py](scripts/mcp_probe.py); usage: `.venv/bin/python scripts/mcp_probe.py <base-url> [bearer-token]`; registers a `rose-probe` OAuth client, so revoke it afterwards) replays what claude.ai does. All 22 checks passed against the public URL and again against `127.0.0.1:8765`:

| # | Check | Result |
| --- | --- | --- |
| 1 | Protected-resource metadata at `/.well-known/oauth-protected-resource/mcp` | 200, resource = public `/mcp` |
| 2 | Auth-server metadata, PKCE S256 advertised | 200 |
| 3–4 | Unauthenticated `POST /mcp` → 401 with `resource_metadata` in `WWW-Authenticate` | ✔ |
| 5–6 | Dynamic client registration: claude.ai redirect accepted (201); foreign redirect refused (400) | ✔ |
| 7–8 | `/authorize` with PKCE → 302 to `/rose/login`; login page renders | ✔ |
| 9–11 | `initialize` (protocol `2025-06-18`), `Mcp-Session-Id` issued, `notifications/initialized` → 202 | ✔ |
| 12–13 | `tools/list` → 72 tools, each with a description and input schema | ✔ |
| 14–16 | `tools/call system_status` OK; unknown tool → `isError` result; `ping` | ✔ |
| 17 | Foreign `Origin` → 403 | ✔ |
| 18 | Unknown session → 404 | ✔ |
| 19 | `GET /mcp` → 200 `text/event-stream` | ✔ |
| 20–21 | `DELETE` session → 200; session then gone → 404 | ✔ |
| 22 | Wrong bearer token → 401 | ✔ |

The probe used a temporary token (`probe-report`) and registered test OAuth clients (`rose-probe`). **All were revoked afterwards.** `rose auth list` now shows only the passphrase, the `forge` token and the `Claude (personal)` client, as before.

### 3.3 Connecting clients (unchanged, for reference)

- **claude.ai:** Settings → Connectors → Add custom connector → `https://washhouse-serotonin-pennant.ngrok-free.dev/mcp` → approve on ROSE's login page with your passphrase. On ngrok's free plan the browser shows ngrok's "Visit Site" page once. API calls never see it.
- **Claude Code:** `claude mcp add --scope user --transport http rose http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token from rose auth new-token forge>"` (already set up).

## 4. FreeLLMAPI

### 4.1 What it is

[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (MIT, Node/TypeScript) is a local router that puts the free tiers of many LLM providers behind one OpenAI-compatible `/v1` API. It handles routing, fallback, caching and quota tracking, and has a built-in **MCP server** at `/mcp`: stateless JSON-RPC, protocol `2025-06-18`, authenticated with its *unified key* as a Bearer token. Its MCP tools:

`ask_freellmapi`, `healthcheck`, `list_models`, `provider_health`, `provider_list`, `usage_summary`, `quota_outlook`, `routing_info`, `set_routing_strategy`, `cache_stats`, `compression_stats`, `web_search`, `clear_cache`.

### 4.2 What was set up (all separate from ROSE)

| Piece | Location |
| --- | --- |
| Source checkout (commit `7ad9097`) and build (`npm ci`, server and dashboard) | `~/0x/projects/freellmapi` |
| Its own config: `NODE_ENV=production`, **`HOST=127.0.0.1`**, `PORT=3001`, fresh random `ENCRYPTION_KEY` (0600, git-ignored) | `~/0x/projects/freellmapi/.env` |
| Its own database (encrypted provider keys, settings, unified key) | `~/0x/projects/freellmapi/server/data/freeapi.db` |
| Service: starts at login, restarts on failure | `~/.config/systemd/user/freellmapi.service` (copy in [deploy/freellmapi.service](deploy/freellmapi.service)) |
| MCP surface turned on (it ships off) | `settings.enable_mcp = 1` in its DB |
| Copy of the unified key for clients (0600, outside ROSE's auth folder) | `~/.config/freellmapi-bridge/unified.key` |
| Claude Code connector | `freellmapi` → `http://127.0.0.1:3001/mcp` (user scope, Bearer header) |

> **Note.** FreeLLMAPI's code defaults to listening on `::` (every interface). It's pinned to `127.0.0.1` in both `.env` and the unit, and `ss` confirms it listens on `127.0.0.1:3001` only.

Current status (`rose freellm status`): **ready**. 48 usable models are already served by FreeLLMAPI's keyless providers, and web search is ready. A live test, `ask` → answered by `Qwen3.5-9B` via `ovh` after an automatic fallback, worked end to end.

### 4.3 The ROSE bridge (`freellm_*`, written but not deployed)

> **Update 2026-11-21 (token-saving refactor):** the nine read-only diagnostics below were merged into one `freellm_info(kind)` tool. ROSE now offers 5 `freellm_*` tools: `freellm_ask`, `freellm_web_search`, `freellm_info`, `freellm_set_routing` and `freellm_clear_cache`. See README → FreeLLMAPI bridge.

[rose/tools/freellm.py](rose/tools/freellm.py) forwards FreeLLMAPI's MCP tools as 13 ROSE tools. Once ROSE is redeployed, claude.ai and your phone reach FreeLLMAPI **through ROSE's existing OAuth-protected ngrok URL**. FreeLLMAPI itself is never exposed, and no second tunnel or domain is needed.

| ROSE tool | FreeLLMAPI tool | Annotation |
| --- | --- | --- |
| `freellm_ask(prompt, system, model, temperature, max_tokens, timeout_s)` | `ask_freellmapi` | read-only, open-world, **untrusted output** |
| `freellm_web_search(query, max_results, topic)` | `web_search` | read-only, open-world, **untrusted output** |
| `freellm_health`, `freellm_models`, `freellm_provider_health`, `freellm_providers`, `freellm_usage`, `freellm_quota`, `freellm_routing`, `freellm_cache_stats`, `freellm_compression_stats` | diagnostics | read-only |
| `freellm_set_routing(strategy)` | `set_routing_strategy` | writes |
| `freellm_clear_cache(confirm)` | `clear_cache` | destructive; needs `confirm=true`, which gets a passphrase approval link when called remotely |

Design points:

- **Additive only.** The ROSE diff is a new file, one registration block in `server.py`, two names added to `UNTRUSTED_TOOLS` in `guard.py` (affecting only the new tools), and a `rose freellm set-key|status` subcommand. No existing tool, auth path, port, config key or instruction text changed.
- **Separate config.** The bridge reads `FREELLMAPI_URL` (default `http://127.0.0.1:3001`), `FREELLMAPI_KEY` or `~/.config/freellmapi-bridge/unified.key`, and `FREELLMAPI_DISABLED=1` to turn it off. Nothing goes in ROSE's `config.toml` or `~/rose/auth/`.
- **Clean errors.** Problems come back as clear `ToolError`s: service down, wrong key, MCP disabled, timeout.

## 5. Setup steps from scratch (for reference)

```sh
# FreeLLMAPI
git clone https://github.com/tashfeenahmed/freellmapi ~/0x/projects/freellmapi
cd ~/0x/projects/freellmapi && npm ci && npm run build -w server && npm run build -w client
umask 077; printf 'NODE_ENV=production\nHOST=127.0.0.1\nPORT=3001\nENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" > .env
cp ~/0x/projects/rose-MCP/deploy/freellmapi.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now freellmapi
# open http://127.0.0.1:3001, create the dashboard account, Keys page → copy the unified key,
# turn on Agent compatibility → MCP, add provider API keys

# Clients
rose freellm set-key            # stores the unified key in ~/.config/freellmapi-bridge/unified.key
claude mcp add --scope user --transport http freellmapi http://127.0.0.1:3001/mcp \
  --header "Authorization: Bearer $(cat ~/.config/freellmapi-bridge/unified.key)"
rose freellm status
```

Operations: `systemctl --user status|restart freellmapi` · `journalctl --user -u freellmapi -f` · updating = `git pull && npm ci && npm run build -w server -w client && systemctl --user restart freellmapi`.

## 6. Flagged for Roshan (nothing below was changed)

1. **The live ROSE server runs stale code.** The installed `rose` tool (`~/.local/share/uv/tools/rose`) was built from the *baseline* commit `5521ab2`. The committed tree-first work (`e0e19b4`) is **not live**: no `observation_stats`, old screenshot and typing behavior, 72 tools instead of 73. The `freellm_*` bridge needs the same redeploy, which would also roll out tree-first. To deploy both:
   ```sh
   cd ~/0x/projects/rose-MCP && uv tool install --force --python /usr/bin/python3 . && rose reload
   ```
   **Read item 2 before running it.**

2. **⚠ Restart hazard: `rose reload` can crash VS Code right now.** ROSE launches apps with `start_new_session=True`, which does not move them out of `rose.service`'s systemd cgroup. VS Code's helper processes (zygote, GPU, network service, renderers), started earlier through ROSE, are currently inside `rose.service`. The unit uses `KillMode=control-group`, so any restart (`rose reload`, a `rose.service` restart, the dashboard's restart button) will kill them. Proposed fix, written and reverted per your instruction:
   - (a) launch GUI apps through `systemd-run --user --scope`, so each gets its own scope;
   - (b) add `KillMode=process` to `rose.service`.

   Until then, close any VS Code window ROSE opened, or restart only when losing it is acceptable.

3. **Remote (claude.ai) access to FreeLLMAPI.**
   - **Recommended:** deploy the ROSE bridge (item 1), which reuses the existing authed ngrok URL.
   - **Alternative:** a standalone public FreeLLMAPI connector. It would need a second public hostname (ngrok's free plan has one static domain, already ROSE's) **and** an OAuth layer in front, because FreeLLMAPI only does static-key auth, which claude.ai connectors can't use. That overlaps your ngrok setup, so it wasn't done.

4. **Finish FreeLLMAPI's first-run setup soon.** The dashboard account is unclaimed. Open `http://127.0.0.1:3001` on this laptop and create it. It's reachable only from this machine, but until it's claimed any local process could claim it. Then add your provider API keys to widen the model pool beyond the keyless providers.

## 6a. Update 2026-11-21: deployed

- Ran `uv tool install --force --python /usr/bin/python3 .` and `rose reload`, then restarted `rose-watch`. The tunnel was left running, so the URL didn't change.
- **Item 1 is done.** Tree-first and the `freellm_*` tools are live: 86 tools. `freellm_health` and `freellm_ask` were tested through the live server, and the public probe passed 22/22.
- **Item 2 was mitigated for this restart.** Before reloading, the nine VS Code helper processes were moved from `rose.service`'s cgroup into VS Code's own scope, so VS Code survived. **The root cause is not fixed.** The next time ROSE opens an app, that app lands in `rose.service` again and a later reload kills it. The permanent fix (`systemd-run --user --scope` + `KillMode=process`) is still waiting for your OK.

## 7. Files touched

| File | Change |
| --- | --- |
| `rose/tools/freellm.py` | **new**: FreeLLMAPI bridge and CLI |
| `rose/server.py` | +1 import, +1 registration block |
| `rose/guard.py` | `freellm_ask`, `freellm_web_search` added to `UNTRUSTED_TOOLS` |
| `rose/cli.py` | + `rose freellm` subcommand |
| `deploy/freellmapi.service` | **new**: copy of the installed unit |
| `scripts/mcp_probe.py` | **new**: 22-check remote conformance probe |
| `MCP_REMOTE_REPORT.md` | **new**: this report |
| `~/0x/projects/freellmapi/` (+ `.env`, `server/data/`) | **new**: separate project |
| `~/.config/systemd/user/freellmapi.service` | **new** |
| `~/.config/freellmapi-bridge/unified.key` | **new** (0600) |
| `~/.claude.json` | + `freellmapi` MCP server entry |

The ROSE repo changes are **not committed** (left in the working tree on `tree-first` for your review).

## 8. Caveman

[Caveman](https://github.com/JuliusBrussee/caveman) (Apache-2.0) makes the agent answer in a terse, structured style. Code, commands and errors stay verbatim, and security warnings and irreversible actions still get full sentences. The authors report about 35% fewer output tokens with `/ultracave` and a few percent with `/caveman`, on top of "answer concisely".

**Installed:** the Claude Code plugin, user scope:
`claude plugin marketplace add JuliusBrussee/caveman && claude plugin install caveman@caveman`.
- It adds skills: `/caveman`, `/ultracave`, `/megacave`, `/caveman-stats`, `/caveman-compress`, and the cavecrew subagents.
- Its hooks (SessionStart, SubagentStart, UserPromptSubmit, SessionEnd) are local Node scripts with no network calls. They track the mode and record stats.
- New interactive sessions start in caveman mode. Say `stop caveman` to turn it off, or set `{"defaultMode": "ultracave"}` in `~/.config/caveman/config.json`.
- **Headless runs (`claude -p`, Forge) stay plain by design.** Verified: a `claude -p` test returned normal sentences.

**Not installed, on purpose:**
- **The `@caveman-ai/cli` proxy** (the Read less / Auto / Browse features). It sets `ANTHROPIC_BASE_URL` in `~/.claude/settings.json`, so every Claude Code run, Forge included, would go through a local caveman proxy. It also disables Claude Code Remote Control, and Auto needs a caveman account. That overlaps the existing setup, so it's your call.
- **ROSE server instructions.** These were not changed. ROSE's replies come from whichever Claude uses it, so the caveman style belongs in that Claude, not in ROSE.

**Your step, for the Claude app (web, desktop, phone):** this can't be done from the terminal. In claude.ai go to **Customize → Plugins** and add caveman from `JuliusBrussee/caveman`, following the repo's INSTALL.md section "Claude apps". The authors list this path as not yet tested end to end.

**Uninstall:** `claude plugin uninstall caveman@caveman && claude plugin marketplace remove caveman`.
