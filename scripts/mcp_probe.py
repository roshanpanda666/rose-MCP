"""End-to-end conformance probe of ROSE over its public URL (what claude.ai does), stdlib + httpx."""
import base64, hashlib, json, secrets, sys
import httpx

BASE = sys.argv[1].rstrip("/")
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")


c = httpx.Client(timeout=30, follow_redirects=False, headers={"User-Agent": "rose-probe/1.0"})

# 1. Discovery
r = c.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
prm = r.json() if r.status_code == 200 else {}
check("PRM /.well-known/oauth-protected-resource/mcp", r.status_code == 200, json.dumps(prm)[:200])
r = c.get(f"{BASE}/.well-known/oauth-authorization-server")
asm = r.json() if r.status_code == 200 else {}
check("AS metadata", r.status_code == 200 and "S256" in asm.get("code_challenge_methods_supported", []),
      f"issuer={asm.get('issuer')} reg={asm.get('registration_endpoint')}")

# 2. Unauthenticated MCP -> 401 + WWW-Authenticate pointing at PRM
r = c.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
           headers={"Accept": "application/json, text/event-stream"})
wa = r.headers.get("www-authenticate", "")
check("unauth POST /mcp -> 401", r.status_code == 401, f"status={r.status_code}")
check("WWW-Authenticate has resource_metadata", "resource_metadata=" in wa, wa[:160])

# 3. Dynamic client registration
reg = {"client_name": "rose-probe", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
       "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
       "token_endpoint_auth_method": "none"}
r = c.post(asm.get("registration_endpoint", f"{BASE}/register"), json=reg)
client = r.json() if r.status_code in (200, 201) else {}
check("DCR (claude.ai redirect) accepted", r.status_code in (200, 201), f"status={r.status_code}")
r = c.post(asm.get("registration_endpoint", f"{BASE}/register"),
           json={**reg, "redirect_uris": ["https://evil.example/cb"]})
check("DCR (foreign redirect) refused", r.status_code == 400, f"status={r.status_code}")

# 4. Authorize with PKCE -> redirect to ROSE login page
if client:
    verifier = secrets.token_urlsafe(48)
    chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get(asm["authorization_endpoint"], params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": reg["redirect_uris"][0],
        "code_challenge": chal, "code_challenge_method": "S256", "state": "xyz", "scope": "rose",
        "resource": f"{BASE}/mcp"})
    loc = r.headers.get("location", "")
    check("authorize -> 302 to /rose/login", r.status_code == 302 and "/rose/login?req=" in loc, loc[:90])
    if loc:
        r = c.get(loc)
        check("login page renders", r.status_code == 200 and "passphrase" in r.text, f"status={r.status_code}")

# 5. Authenticated MCP session
if TOKEN:
    h = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream",
         "Origin": "https://claude.ai"}
    r = c.post(f"{BASE}/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}})
    sid = r.headers.get("mcp-session-id")
    init = r.json() if r.status_code == 200 else {}
    check("initialize", r.status_code == 200 and "result" in init,
          f"status={r.status_code} ct={r.headers.get('content-type')} proto={init.get('result', {}).get('protocolVersion')} "
          f"server={init.get('result', {}).get('serverInfo')}")
    check("Mcp-Session-Id issued", bool(sid))
    h2 = {**h, "Mcp-Session-Id": sid or "", "MCP-Protocol-Version": init.get("result", {}).get("protocolVersion", "")}
    r = c.post(f"{BASE}/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("notifications/initialized -> 202", r.status_code == 202, f"status={r.status_code}")
    r = c.post(f"{BASE}/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r.json().get("result", {}).get("tools", []) if r.status_code == 200 else []
    check("tools/list", len(tools) > 50, f"{len(tools)} tools")
    bad = [t["name"] for t in tools if not t.get("description") or "inputSchema" not in t]
    check("every tool has description + inputSchema", not bad, ",".join(bad))
    r = c.post(f"{BASE}/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                "params": {"name": "system_status", "arguments": {}}})
    res = r.json().get("result", {}) if r.status_code == 200 else {}
    check("tools/call system_status", res and not res.get("isError"), str(res)[:140])
    r = c.post(f"{BASE}/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                "params": {"name": "no_such_tool", "arguments": {}}})
    check("unknown tool -> error, not crash", r.status_code == 200 and (
        "error" in r.json() or r.json().get("result", {}).get("isError")), r.text[:120])
    r = c.post(f"{BASE}/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 5, "method": "ping"})
    check("ping", r.status_code == 200 and "result" in r.json())
    r = c.post(f"{BASE}/mcp", headers={**h2, "Origin": "https://evil.example"},
               json={"jsonrpc": "2.0", "id": 6, "method": "ping"})
    check("foreign Origin refused", r.status_code in (400, 403), f"status={r.status_code}")
    r = c.post(f"{BASE}/mcp", headers={**h2, "Mcp-Session-Id": "bogus"},
               json={"jsonrpc": "2.0", "id": 7, "method": "ping"})
    check("unknown session -> 404", r.status_code == 404, f"status={r.status_code}")
    with c.stream("GET", f"{BASE}/mcp", headers={**h2, "Accept": "text/event-stream"}, timeout=10) as r:
        ct = r.headers.get("content-type", "")
        check("GET /mcp opens server->client SSE stream (or 405)",
              (r.status_code == 200 and ct.startswith("text/event-stream")) or r.status_code == 405,
              f"status={r.status_code} ct={ct}")
    r = c.delete(f"{BASE}/mcp", headers=h2)
    check("DELETE session", r.status_code in (200, 204), f"status={r.status_code}")
    r = c.post(f"{BASE}/mcp", headers=h2, json={"jsonrpc": "2.0", "id": 8, "method": "ping"})
    check("session gone after DELETE -> 404", r.status_code == 404, f"status={r.status_code}")
    r = c.post(f"{BASE}/mcp", headers={**h, "Authorization": "Bearer wrong"},
               json={"jsonrpc": "2.0", "id": 9, "method": "ping"})
    check("bad token -> 401", r.status_code == 401, f"status={r.status_code}")

print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")
if client:
    print("probe_client_id", client["client_id"])
