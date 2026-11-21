# ROSE MCP server

A local MCP server that gives Claude simple, reliable tools for controlling this Linux machine.
ROSE has no LLM inside it: Claude does the reasoning and ROSE does the actions.

Built with the official MCP Python SDK (`mcp` 2.x, where `FastMCP` is now called `MCPServer`). It is managed with `uv` and runs over stdio locally, or over streamable HTTP with auth for remote use.

## Tools

| Group | Tools |
| --- | --- |
| Clipboard | `clipboard_read`, `clipboard_write` (`xclip` on X11, `wl-clipboard` on Wayland) |
| Screen (fallback) | `take_screenshot` (full PNG saved to `~/rose/screenshots/`, plus a downscaled preview of the active window's monitor by default; `area=active_window\|monitor\|desktop`, `monitor=` picks one screen), `list_monitors` |
| Notifications | `send_notification` |
| Apps & windows | `list_apps`, `open_app` (installed desktop apps only, no arguments), `open_url` (http/https only), `list_windows`, `focus_window`, `move_window` (to another monitor or x/y, keeps or sets maximized), `close_window` (polite close, needs `confirm`) |
| Mouse & keyboard | `mouse_click`, `scroll`, `type_text`, `press_keys` (both take an optional `window=` to focus-verify first), `mouse_position` |
| Webcam | `list_cameras`, `webcam_capture` (saved to `~/rose/webcam/`; shows a desktop notification on every capture), `webcam_live_frame` / `webcam_live_stop` (live view: one overwritten frame, about 2 frames a second, stops 60 s after the last request; always needs approval from remote clients) |
| GitHub (read-only) | `github_my_prs`, `github_pr_status`, `github_ci_runs`, `github_notifications` (uses `gh`) |
| Files | `list_allowed_dirs`, `list_dir`, `read_file` (`offset=` pages past the output cap), `write_file`, `move_file`, `search_files`, all limited to the allowlist |
| System | `system_status` (CPU, RAM, disk, battery, top processes) |
| Device (by command) | `media_control` (play, pause, next and so on, for any MPRIS player), `audio_devices`, `set_audio_device`, `set_mic_muted`, `bluetooth_devices`, `bluetooth_connect`, `wifi_networks`, `wifi_connect` (saved networks only; needs `confirm`; switches back if there's no internet after 90 s), `speak`, `open_path`, `stop_process` (SIGTERM; needs `confirm`; protected processes are refused) |
| Command presets | `list_commands` and `run_command(name)` run the fixed commands under `[commands]` in `config.toml`. There's no shell and no arguments. |
| Quick settings | `quick_settings`, `set_quick_setting(setting, value)` for Wi-Fi, Bluetooth, Airplane mode, Power mode, Night Light, Dark Style, Do Not Disturb, Caffeine, volume, mute and brightness, plus `lock_screen`. Wi-Fi off and Airplane on need `confirm` and undo themselves automatically (default 2 min) through a systemd timer, because they cut ROSE's own connection. |
| FreeLLMAPI | `freellm_ask`, `freellm_web_search`, `freellm_info(kind)` (health, models, providers, usage, quota, routing, cache), `freellm_set_routing`, `freellm_clear_cache` (needs `confirm`). They forward to the local FreeLLMAPI router on `127.0.0.1:3001`, which stays off the internet; see below. |
| Layouts | `arrange_window` (left/right/top/bottom halves, quarters, center, maximize, restore, minimize), `tile_windows` (two windows side by side), `workspace` (list, switch, move a window; numbered from 1) |
| Live view | `screen_frame`: a JPEG of a monitor with its coordinate mapping. No file is saved and no OCR runs, which makes it cheap enough for a 1-frame-per-second live view (used by the phone app) |
| Power | `power_control` (sleep, restart, shutdown, logout, cancel). Needs `confirm`, runs after a delay (default 15 s) through a systemd timer, and can be cancelled |
| Phone (ADB) | `phone_devices`, `phone_connect` (wireless debugging, with optional pairing), `phone_screenshot`, `phone_open_app`, `phone_tap`, `phone_swipe`, `phone_type`, `phone_key`. From remote clients, every action that changes the phone needs approval, like the laptop's mouse |
| Recorder | `recording_start` (mic, optionally plus system audio; needs `confirm`; shows a desktop notification), `recording_stop`, `recording_status`, `recording_list`. Files go to `~/rose/recordings/` (0700); ffmpeg runs as the `rose-recording` user service, so it survives `rose reload` |

ROSE is **tree-first**: observe through the accessibility tree (`ui_list`, `ui_read`) or the browser DOM (`browser_read`), act by name (`ui_click`, `ui_fill`, `browser_click`, `browser_fill`), then read back to confirm. Screenshots are the fallback for apps with no tree (canvas, games, remote desktops, video), for read-back failures the tree can't explain, and for visual questions. `observation_stats` reports tree calls vs screenshots per caller and per task (bursts of GUI calls), so you can watch the screenshot rate.

Typing is focus-verified and read back. `type_text(window=...)` and `press_keys(window=...)` make that window active first and refuse to type if they can't. After typing, `type_text` checks through AT-SPI that the text is in the focused element. `ui_fill` and `browser_fill` read the field back, and every result says `Read-back OK` or `READ-BACK FAILED`.

In the screenshot fallback, mouse coordinates default to pixels in the latest preview, and ROSE converts them to real screen pixels.

Window and input tools support X11 only for now.

## v3: the `rose` CLI and always-on services

```sh
uv tool install --force .        # installs the `rose` command into ~/.local/bin
rose status                      # services, local and public URL (with an auth check), clients, today's calls
rose serve                       # MCP over HTTP on 127.0.0.1:8765/mcp (auth required)
rose stdio                       # MCP over stdio (fallback)
rose tunnel                      # Cloudflare quick tunnel; fails closed unless the public URL answers 401
rose reload                      # restart the server with new code; the tunnel and URL stay the same
rose logs [-n 50] [-f] [--kind tools|http|tunnel]
rose auth set-passphrase | new-token <name> | label <id> <label> | list | revoke <x>
rose config                      # show the settings in use
```

Files:
- `~/.config/rose/config.toml`: settings you edit. An older `~/rose/config.json` is migrated into it automatically.
- `~/rose/runtime.json`: runtime state ROSE writes (the tunnel URL).
- `~/rose/`: logs, screenshots, webcam photos, bridge, auth.

Always-on services (systemd user units in `deploy/`):

```sh
cp deploy/rose.service deploy/rose-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rose          # local server, starts at login
systemctl --user enable --now rose-tunnel   # public URL for claude.ai and your phone (optional)
```

Claude Code connects over HTTP with its own token. The token is labeled `Forge`, so bridge posts are attributed correctly:

```sh
claude mcp add --scope user --transport http rose http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(rose auth new-token forge | tail -1)"
```

Localhost still needs a token: tunnel traffic also arrives from 127.0.0.1, so ROSE can't trust localhost.

The v2 `rose-remote` setup (script and unit) has been removed. `rose install-services` disables an old `rose-remote` unit if it is still installed.

## Remote access with ngrok (permanent URL)

1. Sign up at ngrok.com. In the ngrok dashboard, copy your **authtoken** and your free **static domain** (`something.ngrok-free.app`).
2. Install the agent and add the token:
   ```sh
   curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
   echo "deb https://ngrok-agent.s3.amazonaws.com bookworm main" | sudo tee /etc/apt/sources.list.d/ngrok.list
   sudo apt update && sudo apt install ngrok
   ngrok config add-authtoken <YOUR_TOKEN>
   ```
3. In the dashboard's **Tunnel setup**, choose **ngrok**, paste the domain and click **Save**. (Or set `tunnel_provider = "ngrok"` and `ngrok_domain` in the config.)
4. Run `rose install-services` once. It switches from the v2 `rose-remote` service to the `rose` and `rose-tunnel` services.
5. Click **Generate URL** in the dashboard. The URL is copied, and **Open Claude connectors** takes you to the page where you paste it, once.

On ngrok's free plan, the browser shows a "You are about to visit…" page once before ROSE's login page. Click **Visit Site**. Claude's own API calls never see that page.

## Proactive ROSE, maintenance and testing

- **`rose watch`** runs as `rose-watch.service`. Every 30 s it handles due schedules and new security events. Every 5 min it checks disk, RAM and battery, and every 10 min GitHub: failing CI on your PRs and new review requests. Once a day it cleans up. Alerts go to the desktop and your phone.
- **Phone push** uses ntfy. Run `rose push setup` and subscribe to the printed topic in the ntfy app, then check with `rose push test`. Pushes carry only short alerts and approval links, never file or email contents.
- **Schedules**: `schedule_add(kind, time_hhmm, text, repeat)`. A **remind** pushes the text to your phone. A **report** posts a system and GitHub summary to the bridge and pushes a one-line version. A **forge** runs a Forge task (needs `confirm`) and posts the result to the bridge. Use `schedule_list` and `schedule_remove` to manage them.
- **Cleanup** deletes screenshots and webcam photos after `keep_screenshots_days` (7) and logs after `keep_logs_days` (30). Approvals are removed after 2 days and Forge jobs after 14. Run it now with `rose cleanup`. The dashboard shows ROSE's disk use.
- **`rose selftest`** calls every tool through the running server. Add `--full` to include GUI, webcam, speech, accessibility, the browser and one Forge job.
- **Dashboard approvals**: pending approval links show up in an "Approvals waiting" card, with Approve (asks for your passphrase) and Deny buttons.

## Accessibility and browser control

- **`ui_list`, `ui_read`, `ui_click`, `ui_fill`** work through AT-SPI, via `rose/atspi_helper.py` running under the system Python. They read and operate an app's buttons and fields **by name**, with no screenshots. `ui_list` shows the current value of text fields. `ui_read(window, element=...)` reads a single control (value, focused or checked) for read-back. GNOME and GTK apps work out of the box. Chromium and Electron apps need `ui_enable_app_accessibility` and then an app restart. When their tree comes back empty, `ui_list` says so in a `hint`.
- **`browser_*`** control **ROSE's own browser**: Brave with a separate profile in `~/.config/rose-browser`, driven over DevTools on 127.0.0.1. Chromium refuses DevTools control of your main profile, and keeping ROSE's logins separate is safer anyway. Log in there once to the sites ROSE should use. There's deliberately no "run JavaScript" tool, and Enter needs `confirm`.
- From remote clients, clicks, fills and key presses through `ui_*` and `browser_*` are approval-gated like the mouse (see the approval window below).

## Security model (prompt injection)

No filter can reliably detect prompt injection. So ROSE assumes some will get through and makes sure it can't do damage without you (`rose/guard.py`):

1. **Approval links for risky actions.** When a remote client calls any tool with `confirm=true`, ROSE does nothing yet. It saves the exact call and returns a one-time link to `/rose/approve`. The action runs only after you open the link and enter your passphrase. Links expire after 10 minutes and work once. The model never knows the passphrase, so it can't approve itself.
2. **Mouse and keyboard need approval** from remote clients, because a click can press "Send" with no confirm step.
3. **Webcam needs approval** from a remote client (`approval_required_tools`).
   **One approval covers an hour.** Approving any action opens a window (`approval_grant_minutes`, default 60; 0 = ask every time) in which *that same client* runs everything above without another link. Other clients still need their own approval. The model is still told to ask you in chat before any `confirm=true` action. `rose grant` shows the window, `rose grant end` closes it, and lockdown closes it too.
4. **Outside content is labeled untrusted.** Results containing outside text (files, clipboard, bridge, Forge output with emails and documents, GitHub, window and media titles, the screen) carry a notice that the content is data, not instructions.
5. **Injection tripwire.** Those results are also scanned for typical injection phrasing. A hit adds `_injection_warning` to the result, logs the event, and shows a desktop notification and a dashboard alert. It flags; it doesn't block.
6. **Lockdown.** `rose lockdown on`, or the dashboard button, makes ROSE read-only for every client.
7. **Rate limit.** Remote clients are limited to 150 calls a minute, which stops runaway loops.
8. **Rules for Rose.** ROSE's instructions tell Rose what counts as good and bad commands, and that text inside tool results is never an instruction.

Local Claude Code (Forge) uses its own token, and Claude Code's own permission prompts protect it, so `confirm=true` acts directly there. Set `approval_mode = "chat"` to go back to chat-only confirmation for remote clients (not recommended).

**What ROSE can't control:** Rose's own claude.ai tools, such as her Gmail. An injection that reaches Rose could still misuse those. ROSE limits what such an injection can do *on your laptop*.

## Headless Forge (`forge_run`)

Rose (the personal Claude) can't reach work Gmail or Drive. `forge_run(task)` runs Claude Code headless on this machine (`claude -p`), under the work account, with the task described by intent. It returns Forge's answer.

| Tool | What it does |
| --- | --- |
| `forge_run(task, wait=45)` | Starts a job and waits up to 55 s. If it's still running, it returns a `job_id`. |
| `forge_status(job_id, wait=45)` | Gets the result of a running job. |
| `forge_send(draft_id, confirm)` | Sends a draft that `forge_run` created, exactly as drafted. Needs `confirm=true`. |
| `forge_jobs()` | Lists recent jobs. |

Safety is enforced by Claude Code's permission flags, not by prompting:
- **Locked-down task jobs.** They run with `--tools ToolSearch --permission-mode dontAsk`, plus an allowlist of read-only tools for Gmail, Drive, Docs, Sheets, Slides, Calendar, Notion and Meetings, and Gmail **draft creation**. Send, reply, forward, sharing, deleting, Slack, ROSE itself and shell access are all denied. A test call to `send_message` came back with "No such tool available".
- **Narrow send jobs.** A send job may only call `send_message`, only for a draft ID that a `forge_run` job produced, only with `confirm=true`, and only once.
- **Limits.** Each job has a timeout (`forge_timeout`, default 600 s). At most 2 jobs run at once and 20 per hour. Output is capped at 15k characters.
- **Logs.** Every job is logged to `~/rose/logs/forge.jsonl`, and the job files are kept in `~/rose/forge/jobs/`.
- **Settings:** `forge_enabled`, `forge_model` (for example `"sonnet"`, to use less work-account quota), `forge_timeout`, `forge_max_running`, `forge_max_per_hour`.

Headless runs set `MCP_CONNECTION_NONBLOCKING=false`. Without it, the first turn can start before the claude.ai connectors finish connecting, and Forge then has no work tools.

## Dashboard (GUI)

`rose gui` opens a CustomTkinter dashboard. It shows:
- **Should I worry?**: one verdict plus the reasons behind it.
- **Remote access**: the tunnel URL with a Copy button, and whether that URL requires auth (checked every 30 s). Buttons start and stop remote access or revoke every client.
- **Local server**: status and a restart button.
- **Who can connect**: each client and token.
- **This machine**: CPU, RAM, disk and battery.
- **Recent activity**: who called which tool.
- **Bridge**: the latest messages, with a box to post as Roshan.

```sh
uv tool install --force --python /usr/bin/python3 .   # system Python: its Tk renders fonts properly
rose install-desktop                                   # app-menu entry + open at login (~/.config/autostart)
rose install-desktop --no-autostart                    # keep the app-menu entry, stop opening at login
```

Only one dashboard opens at a time. Launching it again while it's open does nothing.

## Setup

```sh
sudo apt install xclip wl-clipboard libnotify-bin xdotool ffmpeg gh
gh auth login          # for the GitHub tools
uv sync
```

### Local (Claude Code, stdio)

```sh
claude mcp add -s user rose -- uv --directory /home/roshan/0x/projects/rose-MCP run rose/server.py
```

### Remote (HTTP; auth required)

1. **Set up auth first.** HTTP mode refuses to start until at least one of these exists:
   ```sh
   uv run python -m rose.auth set-passphrase    # OAuth login, for claude.ai and your phone
   uv run python -m rose.auth new-token laptop  # bearer token, for Claude Code or scripts
   uv run python -m rose.auth list
   uv run python -m rose.auth revoke <name | client_id | all>
   ```
2. Run `rose serve` (or the `rose` service). It listens on `127.0.0.1:8765` only, so the tunnel is the only way in.
3. Run `rose tunnel` (or the `rose-tunnel` service): ngrok static domain, or a Cloudflare quick tunnel whose URL changes on every restart. It records the public URL in `~/rose/runtime.json` and `~/rose/public_url.txt`.
4. Add `<public_url>/mcp` as a custom connector in Claude.

`rose tunnel` refuses to start without auth. It shuts the tunnel down unless the public URL answers 401 to an unauthenticated request. `scripts/mcp_probe.py <base-url> [token]` runs a 22-check conformance test (discovery, 401 challenge, client registration, PKCE, session lifecycle, Origin check); it registers a `rose-probe` client, so revoke that afterwards.

How auth works:

- **OAuth 2.1** with PKCE and dynamic client registration. Clients may only register redirect URIs on `oauth_redirect_hosts` (https only, apart from localhost).
- **Owner approval.** Every connection must be approved on ROSE's own login page with the passphrase. Five wrong attempts lock the page for 15 minutes.
- **Token lifetimes.** Access tokens last 1 hour. Refresh tokens last 30 days and are single-use: each refresh issues a new one.
- **Storage.** The passphrase is stored scrypt-hashed and tokens SHA-256-hashed, in `~/rose/auth/state.json` (mode 0600).
- **Request checks.** Host and Origin headers are checked to block DNS rebinding.
- **Logging.** Logins, failed logins and client registrations are logged.

## Bridge (messages between accounts)

`bridge_read` and `bridge_post` share messages through one markdown file, `~/rose/bridge/BRIDGE.md` (you can change the location with `bridge_path`). Every Claude account connected to ROSE uses it, and so does Forge (Claude Code, over stdio).

The sender name is not something the model chooses. ROSE sets it from the login:
- Local stdio calls are **Forge**.
- An OAuth client or token gets the label you give it:
  ```sh
  uv run python -m rose.auth list                                  # find the client id
  uv run python -m rose.auth label <client_id> "Claude (Alphalake)"
  ```

**Email copies.** Every post, whoever makes it, is also emailed to `bridge_email_to` in the config.

- **Sending account:** a Gmail account using an app password. Set it up with `rose email setup` (it's stored in `~/rose/auth/email.json`, mode 0600) and send a test with `rose email test`.
- **Never blocks:** emails send in the background, so a slow mail server never holds up a post.
- **Rate limit:** at most 40 emails an hour.
- **Failures:** they show up as a warning in the dashboard.
- **Turning it off:** set `bridge_email_enabled = false`, or run `rose email off`.

After changing code, run `uv tool install --force --python /usr/bin/python3 . && rose reload`. That restarts only the HTTP server, so the tunnel and its URL stay the same.

## Configuration and safety

`~/.config/rose/config.toml` is created with commented defaults on first run (`ROSE_CONFIG` overrides the path; `ROSE_HOME` moves the data folder). It's re-read whenever the file changes, so edits apply without a restart (except the port). The main keys:

| Key | Meaning |
| --- | --- |
| `allowed_dirs` | Folders the file tools may use |
| `max_read_bytes` | Size limit for `read_file` and content search |
| `input_enabled` | Set `false` to turn off mouse and keyboard control |
| `webcam_device` | Default camera, e.g. `/dev/video0` |
| `http_port` | Port for `rose serve` (always on 127.0.0.1); the public URL lives in `~/rose/runtime.json` |
| `oauth_redirect_hosts` | Hosts OAuth clients may redirect back to |
| `max_output_chars` | Cap on the text one tool call returns to the model (default 12000; 0 = no cap) |
| `large_output_chars` | Outputs bigger than this are logged for `rose tokens` (default 4000) |
| `disabled_tools` | Tools ROSE doesn't offer at all, e.g. `["webcam_capture", "lock_screen"]` |

Rules:

- **Allowlist.** Paths outside `allowed_dirs` are refused, after resolving symlinks and `..`.
- **Destructive actions need `confirm=true`.** This covers overwriting or replacing files, closing windows, and anything that presses Enter (`press_keys` with Return, or `type_text` containing newlines). Enter is guarded because it sends messages and submits forms.
- **No shell.** There is no delete tool and no shell-execution tool. `open_app` only launches installed `.desktop` apps.
- **Logging.** Every tool call is logged to `~/rose/logs/YYYY-MM-DD.jsonl`.

## Token budget

ROSE's tool list is sent to the model every session, and every tool result lands in its context. So:

- **Short descriptions.** Each tool's docstring is its description: one line, about 200 characters at most. Safety requirements (`confirm=true`, "ask the user") always stay in.
- **Compact schemas.** Pydantic's auto `title` keys are stripped and optional types are written as `"type": ["integer", "null"]`. Property names are never changed.
- **Stable order.** Tools register from one fixed table (`_tool_table()` in `rose/server.py`), so the tool list is byte-identical between sessions and clients can prompt-cache it. Append new tools; don't reorder.
- **Compact output** (`rose/output.py`, applied to every tool after the guard):
  - Dicts and lists become one-line JSON instead of the SDK's indented pretty-print.
  - Empty fields (`None`, `""`, `[]`, `{}`) are dropped; `False` and `0` stay.
  - Trailing spaces and blank-line runs are trimmed, except where text is payload: `read_file`, `clipboard_read`, `run_command`, `bridge_read`.
  - Text is capped at `max_output_chars` with a note saying how much was cut (`read_file` pages with `offset=`).
- **Fewer tools.** Rarely used diagnostics are merged: FreeLLMAPI's nine read-only tools are now one `freellm_info(kind)`. Drop more with `disabled_tools`.
- **Find heavy tools.** Outputs over `large_output_chars` go to `~/rose/logs/output-YYYY-MM-DD.jsonl`.
  - `rose tokens` ranks the tools by output tokens.
  - `rose tokens --schema` shows how big the tool list is (currently about 6.8k tokens for 96 tools, including the instructions).

Untrusted-content labels and injection warnings are never trimmed.

## FreeLLMAPI bridge

The `freellm_*` tools forward to [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi), a local router over free LLM tiers. It runs as its own service (`freellmapi.service`, `127.0.0.1:3001`, unit in `deploy/`).

- **Off the internet.** It only does static-key auth, so it is never exposed: remote clients reach it through ROSE's OAuth-protected tunnel.
- **Separate settings.** Its URL is `FREELLMAPI_URL` (default `http://127.0.0.1:3001`). Its key is in `~/.config/freellmapi-bridge/unified.key` (set with `rose freellm set-key`). `FREELLMAPI_DISABLED=1` turns the tools off.
- **Check it** with `rose freellm status`.

## Tests

```sh
uv run pytest -q
```

The suite runs in a throwaway `ROSE_HOME` and never touches `~/rose`, the live server, or the desktop. It covers:
- the file allowlist and confirm rules;
- config caching;
- OAuth tokens and refresh rotation;
- the guard: tripwire, untrusted labels, lockdown, remote approvals and grants, rate limit;
- bridge sanitizing;
- the Enter and network-cut confirm guards;
- the FreeLLMAPI bridge (mocked);
- screenshot coordinate mapping;
- output shaping and schema compaction;
- the HTTP transport end to end: 401 without a token, session with a token, foreign Origin refused, stable tool order.

`rose selftest [--full]` still exercises every tool against the running server on the real desktop.

## Performance notes

- **`take_screenshot`** grabs the X screen in-process through XCB, about 60 ms instead of about 1.3 s for `maim`. The PNG save, the preview and the OCR then run in parallel. Windows are listed once and `xrandr` is cached for 10 s.
  - Totals: about 1.3 s with OCR (was about 3.2 s), about 0.2 s with `include_text=false`.
  - Full-resolution PNGs use fast compression, so they are about 30% bigger on disk.
- **`system_status`** keeps psutil's per-process counters between calls, so it only waits for the 0.5 s CPU sample on the first call: about 40 ms afterwards (was about 550 ms).
- **`quick_settings`, `bluetooth_devices`, `media_control` and `github_my_prs`** run their independent subprocess or network calls in parallel.
- **`list_apps` and `open_app`** cache the parsed `.desktop` files until an applications folder changes.
- **`focus_window`** polls for the focus change instead of always sleeping 0.3 s.
- **Config** is parsed once and re-read only when the file changes. `psutil` and `httpx` load lazily.

## Adding a tool

1. Write a plain function in `rose/tools/<name>.py` with type hints and a one-line docstring. The SDK builds the schema from the hints, and the docstring becomes the description, so keep it short and mention `confirm=true` if the tool needs it. For expected failures, raise `rose.common.ToolError("message")` (use `from None` inside `except`). That message reaches Claude; any other exception shows up only as "Error executing tool".
2. Append it to `_tool_table()` in `rose/server.py` with `READ_ONLY`, `WRITES` or `DESTRUCTIVE` (or the `OPEN_*` variants for tools that reach the internet). If its output carries outside text, add it to `UNTRUSTED_TOOLS` in `rose/guard.py`.
3. Add a test in `tests/`, run `uv run pytest -q`, then `rose reload`.

## Layout

```
rose/
  server.py    # tool table + registration, stdio/HTTP entry points, login and approval routes
  output.py    # compact tool output, size cap, large-output log (rose tokens)
  guard.py     # lockdown, rate limit, approvals and grants, untrusted labels, injection tripwire
  auth.py      # OAuth provider, tokens, passphrase, login/approval pages, admin CLI
  config.py    # config.toml (cached), runtime.json, paths
  common.py    # ToolError, call logging, subprocess/HTTP/systemd/notification helpers
  cli.py       # the `rose` command
  tunnel.py    # ngrok / Cloudflare tunnel, fail-closed
  health.py, gui.py, watch.py, push.py, mailer.py, maintenance.py, metrics.py, selftest.py
  tools/       # apps, browser, bridge, clipboard, device, files, forge, freellm, github, input, notify,
               # quick, screenshot, system, ui, webcam
tests/         # pytest suite (uv run pytest -q)
scripts/       # mcp_probe.py: remote conformance probe
deploy/        # systemd user units
```
