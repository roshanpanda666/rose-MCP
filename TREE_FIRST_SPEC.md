# Spec: Tree-first observation, screenshot as fallback

## Goal
Cut screenshot usage drastically. ROSE should observe and act through the accessibility tree (AT-SPI) and the browser DOM by default, and only fall back to screenshots when the tree can't do the job. It should keep (and improve) the "check my own work" behaviour that screenshots currently provide.

## 1. Default observe/act path
- Observe with `ui_list` / `ui_read` (desktop apps via AT-SPI) and `browser_read` / `browser_tabs` (DOM via CDP).
- Act with `ui_click` / `ui_fill` / `browser_click` / `browser_fill`, targeting controls by **name/role/id**, never by pixel coordinates.
- `mouse_click` with coordinates is last resort only (after a screenshot fallback).
- For Chromium/Electron apps (VS Code, Brave, Slack), make sure `ui_enable_app_accessibility` has been applied; if the tree comes back empty for one of these, suggest enabling it + restarting the app before falling back to screenshots.

## 2. Focus-verify before typing (the main bug fix)
Most "typed into the wrong place" errors are focus bugs. Before any `type_text` or `press_keys` that inserts text:
1. `list_windows` → confirm the intended window is `active`. If not, `focus_window` it.
2. Prefer `ui_fill(window, element, text)` over `type_text` whenever the target field is in the tree — it targets the field directly, no focus guessing.
3. Only use raw `type_text` when no addressable field exists.

## 3. Read-back verification after typing/acting
After typing or a state-changing click:
- Re-read the target field / window via `ui_read` or `ui_list` (or `browser_read` for web) and confirm the expected text/state is present **in the expected element**.
- Confirm the active window title is still what was expected.
- If read-back fails (text missing, landed elsewhere, unexpected dialog appeared) → undo if possible (e.g. ctrl+z in the wrong field), then escalate to the screenshot fallback to diagnose.

## 4. Screenshot fallback — only when
- The tree is empty/unavailable: canvas apps, games, image editors, remote desktop/VNC, video, apps without accessibility.
- A read-back check failed and the tree doesn't explain why.
- The task is inherently visual (user asks "what does this look like", charts, images).
When a screenshot is needed:
- Capture the **active window or a single monitor**, not the full multi-monitor desktop.
- Downscale the preview; prefer `include_text=true` (OCR + window list) and skip the image when text is enough.

## 5. Tool descriptions / system guidance
- `take_screenshot` description: mark it as FALLBACK — "use only when ui_list/ui_read/browser_read can't see what you need, or a read-back check failed."
- `ui_list` / `ui_read` / `ui_fill` descriptions: mark as the PREFERRED way to see and act on the desktop.
- `type_text` description: add "verify the target window is active first; prefer ui_fill when the field is in the tree."
- Add a short operating policy in the server instructions: observe via tree → act by name → read back → screenshot only on failure.

## 6. Optional: metrics
- Log per-task counts of tree calls vs screenshot calls, so we can see the screenshot rate drop.

## Constraints
- Linux only for now (AT-SPI). Don't break existing tools' signatures.
- Work on a new local branch (e.g. `tree-first`), commit locally, don't push.

## Acceptance
- Common flows (open folder in VS Code, fill a form in Brave, type into a terminal) complete with zero screenshots.
- A deliberate wrong-focus typing test is caught by read-back and corrected without a screenshot where possible.
