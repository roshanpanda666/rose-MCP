# ROSE MCP server

A local MCP server that gives Claude simple, reliable tools for controlling this Linux machine.
ROSE has no LLM inside it: Claude does the reasoning and ROSE does the actions.

Built with the official MCP Python SDK (`mcp` 2.x, where `FastMCP` is now called `MCPServer`). It is managed with `uv` and runs over stdio locally, or over streamable HTTP with auth for remote use.

## Tools

| Group | Tools |
| --- | --- |
| Clipboard | `clipboard_read`, `clipboard_write` (`xclip` on X11, `wl-clipboard` on Wayland) |
| Screen | `take_screenshot` (full PNG saved to `~/rose/screenshots/`, plus a downscaled preview; `monitor=` crops to one screen), `list_monitors` |
| Notifications | `send_notification` |
| Apps & windows | `list_apps`, `open_app` (installed desktop apps only, no arguments), `open_url` (http/https only), `list_windows`, `focus_window`, `close_window` (polite close, needs `confirm`) |
| Mouse & keyboard | `mouse_click`, `scroll`, `type_text`, `press_keys`, `mouse_position` |
| Webcam | `list_cameras`, `webcam_capture` (saved to `~/rose/webcam/`; shows a desktop notification on every capture) |
| GitHub (read-only) | `github_my_prs`, `github_pr_status`, `github_ci_runs`, `github_notifications` (uses `gh`) |
| Files | `list_allowed_dirs`, `list_dir`, `read_file`, `write_file`, `move_file`, `search_files`, all limited to the allowlist |
| System | `system_status` (CPU, RAM, disk, battery, top processes) |
| Device (by command) | `media_control` (play, pause, next and so on, for any MPRIS player), `audio_devices`, `set_audio_device`, `set_mic_muted`, `bluetooth_devices`, `bluetooth_connect`, `wifi_networks`, `wifi_connect` (saved networks only; needs `confirm`; switches back if there's no internet after 90 s), `speak`, `open_path`, `stop_process` (SIGTERM; needs `confirm`; protected processes are refused) |
| Command presets | `list_commands` and `run_command(name)` run the fixed commands under `[commands]` in `config.toml`. There's no shell and no arguments. |
| Quick settings | `quick_settings`, `set_quick_setting(setting, value)` for Wi-Fi, Bluetooth, Airplane mode, Power mode, Night Light, Dark Style, Do Not Disturb, Caffeine, volume, mute and brightness, plus `lock_screen`. Wi-Fi off and Airplane on need `confirm` and undo themselves automatically (default 2 min) through a systemd timer, because they cut ROSE's own connection. |

Mouse coordinates default to pixels in the latest screenshot preview, and ROSE converts them to real screen pixels. So the normal loop is: focus the window, take a screenshot, click or type, then take another screenshot to check.

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

`scripts/rose-remote.sh` and `deploy/rose-remote.service` are the v2 setup. Replace them with the two units above.

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

- **`ui_list`, `ui_read`, `ui_click`, `ui_fill`** work through AT-SPI, via `rose/atspi_helper.py` running under the system Python. They read and operate an app's buttons and fields **by name**, with no screenshots. GNOME and GTK apps work out of the box. Chromium and Electron apps need `ui_enable_app_accessibility` and then an app restart.
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
2. Set `public_url` in `~/rose/config.json` to your tunnel's https URL.
3. Run `uv run rose/server.py --http`. It listens on `127.0.0.1:8765` only, so the tunnel is the only way in.
4. Point the tunnel at `http://127.0.0.1:8765`, then add `<public_url>/mcp` as a custom connector in Claude.

Quick start with a Cloudflare quick tunnel (no account; the URL changes on every restart):

```sh
scripts/rose-remote.sh     # or: install deploy/rose-remote.service as a systemd user service
cat ~/rose/public_url.txt  # connector URL
```

The script refuses to start without auth. It shuts everything down unless the public URL answers 401 to an unauthenticated request. It also shows the connector URL as a desktop notification.

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

After changing code, run `scripts/rose-reload.sh`. It restarts only the HTTP server, so the tunnel and its URL stay the same.

## Configuration and safety

`~/rose/config.json` is created with defaults on first run. Set `ROSE_HOME` to use a different folder.

| Key | Meaning |
| --- | --- |
| `allowed_dirs` | Folders the file tools may use |
| `max_read_bytes` | Size limit for `read_file` and content search |
| `input_enabled` | Set `false` to turn off mouse and keyboard control |
| `webcam_device` | Default camera, e.g. `/dev/video0` |
| `http_port`, `public_url` | HTTP mode settings |
| `oauth_redirect_hosts` | Hosts OAuth clients may redirect back to |

Rules:

- **Allowlist.** Paths outside `allowed_dirs` are refused, after resolving symlinks and `..`.
- **Destructive actions need `confirm=true`.** This covers overwriting or replacing files, closing windows, and anything that presses Enter (`press_keys` with Return, or `type_text` containing newlines). Enter is guarded because it sends messages and submits forms.
- **No shell.** There is no delete tool and no shell-execution tool. `open_app` only launches installed `.desktop` apps.
- **Logging.** Every tool call is logged to `~/rose/logs/YYYY-MM-DD.jsonl`.

## Adding a tool

1. Write a plain function in `rose/tools/<name>.py`. Give it type hints and a docstring: the SDK builds the tool's schema from the hints and its description from the docstring. For expected failures, raise `rose.common.ToolError("message")`, and the message is passed to Claude. Any other exception shows up to Claude only as "Error executing tool".
2. Register it in `register_tools()` in `rose/server.py` with `READ_ONLY`, `WRITES` or `DESTRUCTIVE`.
3. Test the function directly, then reconnect `rose` from `/mcp` in Claude Code.

## Layout

```
rose/
  server.py   # tool registration, stdio and HTTP entry points, login routes
  auth.py     # OAuth provider, tokens, passphrase, login page, admin CLI
  config.py   # paths and settings (~/rose/config.json)
  common.py   # ToolError, call logging, X11/Wayland detection, subprocess helper
  tools/      # clipboard, screenshot, notify, apps, input, webcam, github, files, system
```
