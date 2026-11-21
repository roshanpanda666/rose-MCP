"""FreeLLMAPI bridge: FreeLLMAPI's MCP tools, served through ROSE.

FreeLLMAPI (github.com/tashfeenahmed/freellmapi) is a local OpenAI-compatible router over the free tiers
of many LLM providers. It has its own MCP endpoint (POST /mcp, stateless JSON-RPC, unified key as a
Bearer token), but no OAuth. claude.ai remote connectors need OAuth, and ROSE's rule is that nothing
goes on the internet without auth. So FreeLLMAPI stays on 127.0.0.1 and ROSE forwards its tools: remote
clients reach them through ROSE's OAuth-protected tunnel, with ROSE's guardrails (lockdown, rate limit,
approval for destructive calls, untrusted-content labels, logging).

Kept separate from ROSE's own config and auth: the URL comes from FREELLMAPI_URL (default
http://127.0.0.1:3001), the unified key from FREELLMAPI_KEY or ~/.config/freellmapi-bridge/unified.key (0600,
set with `rose freellm set-key`). Set FREELLMAPI_DISABLED=1 to turn the freellm_* tools off.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Literal

from rose.common import ToolError

KEY_PATH = Path.home() / ".config" / "freellmapi-bridge" / "unified.key"
_ids = itertools.count(1)


def _key() -> str:
    env = os.environ.get("FREELLMAPI_KEY", "").strip()
    if env:
        return env
    try:
        return KEY_PATH.read_text().strip()
    except OSError:
        raise ToolError("FreeLLMAPI key not set. On the laptop run: rose freellm set-key "
                        "(the unified key from the FreeLLMAPI dashboard's Keys page).") from None


def _base() -> str:
    if os.environ.get("FREELLMAPI_DISABLED") == "1":
        raise ToolError("FreeLLMAPI tools are turned off (FREELLMAPI_DISABLED=1).")
    return os.environ.get("FREELLMAPI_URL", "http://127.0.0.1:3001").rstrip("/")


def call(tool: str, arguments: dict | None = None, timeout: float = 30) -> dict | str:
    """Call one FreeLLMAPI MCP tool and return its structured result (or text)."""
    import httpx  # lazy: keeps ROSE's startup light

    body = {"jsonrpc": "2.0", "id": next(_ids), "method": "tools/call",
            "params": {"name": tool, "arguments": {k: v for k, v in (arguments or {}).items() if v is not None}}}
    try:
        r = httpx.post(f"{_base()}/mcp", json=body, timeout=timeout,
                       headers={"Authorization": f"Bearer {_key()}", "Accept": "application/json"})
    except httpx.ConnectError:
        raise ToolError("FreeLLMAPI is not running. On the laptop: systemctl --user start freellmapi") from None
    except httpx.TimeoutException:
        raise ToolError(f"FreeLLMAPI did not answer within {int(timeout)} s.") from None
    try:
        msg = r.json()
    except ValueError:
        raise ToolError(f"FreeLLMAPI returned HTTP {r.status_code} with a non-JSON body.") from None
    if "error" in msg:  # 401 bad key, 403 MCP disabled, unknown tool...
        raise ToolError(f"FreeLLMAPI: {msg['error'].get('message', msg['error'])}")
    res = msg.get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        raise ToolError(f"FreeLLMAPI {tool} failed: {text[:1000]}")
    if "structuredContent" in res:
        return res["structuredContent"]
    try:
        return json.loads(text)
    except ValueError:
        return text


# ---------------- tools (names prefixed freellm_ so they don't clash with ROSE's own) ----------------

# freellm_info kind -> (FreeLLMAPI tool, fixed arguments). One tool instead of nine read-only ones keeps
# ROSE's tool list (sent to the model every session) short.
INFO = {
    "health": ("healthcheck", {}),
    "models": ("list_models", {"available_only": True}),
    "all_models": ("list_models", {"available_only": False}),
    "provider_health": ("provider_health", {}),
    "providers": ("provider_list", {}),
    "usage_24h": ("usage_summary", {"range": "24h"}),
    "usage_7d": ("usage_summary", {"range": "7d"}),
    "usage_30d": ("usage_summary", {"range": "30d"}),
    "quota": ("quota_outlook", {}),
    "routing": ("routing_info", {}),
    "cache": ("cache_stats", {}),
    "compression": ("compression_stats", {}),
}
InfoKind = Literal["health", "models", "all_models", "provider_health", "providers", "usage_24h", "usage_7d",
                   "usage_30d", "quota", "routing", "cache", "compression"]


def freellm_ask(prompt: str, system: str = "", model: str = "", temperature: float | None = None,
                max_tokens: int | None = None, timeout_s: int = 120) -> dict:
    """Ask a free FreeLLMAPI model a text task (only when the user wants FreeLLMAPI). model: id or "auto"."""
    timeout_s = max(1, min(int(timeout_s), 300))
    r = call("ask_freellmapi", {"prompt": prompt, "system": system or None, "model": model or None,
                                "temperature": temperature, "max_tokens": max_tokens,
                                "timeout_ms": timeout_s * 1000}, timeout=timeout_s + 15)
    if isinstance(r, dict):  # keep what matters; drop routing internals unless something went wrong
        keep = ("answer", "model", "finish_reason", "fallback_trail")
        r = {k: r.get(k) for k in keep} | {"tokens": (r.get("usage") or {}).get("total_tokens")}
        if isinstance(r["answer"], str):
            r["answer"] = r["answer"].strip()
    return r


def freellm_info(kind: InfoKind = "health") -> dict:
    """FreeLLMAPI diagnostics: health (call first on failures), models, providers, usage, quota, routing, cache."""
    tool, args = INFO[kind]
    return call(tool, args)


def freellm_set_routing(strategy: Literal["priority", "balanced", "smartest", "fastest", "reliable",
                                          "custom", "decision"]) -> dict:
    """Set FreeLLMAPI's routing strategy (changes server state; only on request)."""
    return call("set_routing_strategy", {"strategy": strategy})


def freellm_web_search(query: str, max_results: int = 5, topic: Literal["general", "news"] = "general") -> dict:
    """Web search via FreeLLMAPI's search providers: titles, URLs, extracts."""
    return call("web_search", {"query": query, "max_results": max(1, min(int(max_results), 20)), "topic": topic},
                timeout=60)


def freellm_clear_cache(confirm: bool = False) -> dict:
    """Flush FreeLLMAPI's response cache (irreversible). Needs confirm=true."""
    if not confirm:
        raise ToolError("Clearing the FreeLLMAPI cache can't be undone. Ask the user, then call again "
                        "with confirm=true.")
    return call("clear_cache")


def freellm_health() -> dict:
    """Readiness summary (used by `rose freellm status`)."""
    return call("healthcheck")


# ---------------- CLI: rose freellm set-key | status ----------------

def cli(argv: list[str]) -> int:
    import getpass

    cmd = argv[0] if argv else "status"
    if cmd == "set-key":
        key = getpass.getpass("FreeLLMAPI unified key (Keys page, starts with freellmapi-): ").strip()
        if not key.startswith("freellmapi-"):
            print("That doesn't look like a FreeLLMAPI unified key.")
            return 1
        KEY_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        print(f"Saved to {KEY_PATH} (0600).")
        cmd = "status"
    if cmd == "status":
        try:
            h = freellm_health()
        except ToolError as e:
            print(f"FreeLLMAPI: {e}")
            return 1
        print(f"FreeLLMAPI at {_base()}: {h.get('status')}; providers {h.get('healthy_providers')}/"
              f"{h.get('configured_providers')} healthy; {h.get('available_models')} usable models; "
              f"default model {h.get('default_model')}; search {h.get('search', {}).get('status')}")
        return 0
    print("Usage: rose freellm set-key | status")
    return 1
