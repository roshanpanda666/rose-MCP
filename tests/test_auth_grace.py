"""Refresh-token reuse grace: a just-rotated refresh token re-sent by the SAME client within the window gets the
same token pair back; anything else keeps the old single-use behaviour."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.auth.provider import TokenError
from mcp.shared.auth import OAuthClientInformationFull

from rose import auth


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def setup(auth_state, monkeypatch):
    monkeypatch.setattr(auth, "_reuse_grace", lambda: 60.0)
    p = auth.RoseOAuthProvider("http://testserver")
    c1 = OAuthClientInformationFull(client_id="c1", redirect_uris=["https://claude.ai/cb"])
    c2 = OAuthClientInformationFull(client_id="c2", redirect_uris=["https://claude.ai/cb"])
    run(p.register_client(c1))
    run(p.register_client(c2))
    tok = p._issue("c1", ["rose"], None)
    return p, c1, c2, tok


def _refresh(p, client, token):
    rt = run(p.load_refresh_token(client, token))
    if rt is None:  # what the SDK token handler does
        raise TokenError(error="invalid_grant", error_description="refresh token does not exist")
    return run(p.exchange_refresh_token(client, rt, []))


def test_sequential_retry_same_client_gets_same_pair(setup):
    p, c1, _, tok = setup
    first = _refresh(p, c1, tok.refresh_token)
    again = _refresh(p, c1, tok.refresh_token)
    assert again.access_token == first.access_token and again.refresh_token == first.refresh_token
    assert 0 < again.expires_in <= auth.ACCESS_TTL
    assert run(p.load_access_token(again.access_token)).client_id == "c1"
    # Only one live pair was created by the two calls (the original one was rotated away)
    s = auth.load_state()
    assert len(s["refresh"]) == 1 and auth._h(first.refresh_token) in s["refresh"]


def test_race_both_loaded_before_exchange(setup):
    p, c1, _, tok = setup
    rt_a = run(p.load_refresh_token(c1, tok.refresh_token))
    rt_b = run(p.load_refresh_token(c1, tok.refresh_token))
    a = run(p.exchange_refresh_token(c1, rt_a, []))
    b = run(p.exchange_refresh_token(c1, rt_b, []))
    assert (a.access_token, a.refresh_token) == (b.access_token, b.refresh_token)


def test_new_refresh_token_still_rotates_normally(setup):
    p, c1, _, tok = setup
    first = _refresh(p, c1, tok.refresh_token)
    second = _refresh(p, c1, first.refresh_token)
    assert second.refresh_token != first.refresh_token
    # The client moved on: re-sending the ORIGINAL token is plain reuse again
    with pytest.raises(TokenError):
        _refresh(p, c1, tok.refresh_token)


def test_different_client_is_refused(setup):
    p, c1, c2, tok = setup
    _refresh(p, c1, tok.refresh_token)
    assert run(p.load_refresh_token(c2, tok.refresh_token)) is None
    rt = auth.RefreshToken(token=tok.refresh_token, client_id="c2", scopes=["rose"], subject="owner")
    with pytest.raises(TokenError) as err:
        run(p.exchange_refresh_token(c2, rt, []))
    assert err.value.error == "invalid_grant"


def test_outside_window_is_refused(setup, monkeypatch):
    p, c1, _, tok = setup
    _refresh(p, c1, tok.refresh_token)
    real = auth.time.time
    monkeypatch.setattr(auth.time, "time", lambda: real() + 61)
    with pytest.raises(TokenError):
        _refresh(p, c1, tok.refresh_token)


def test_grace_zero_keeps_old_behaviour(setup, monkeypatch, capsys):
    p, c1, _, tok = setup
    monkeypatch.setattr(auth, "_reuse_grace", lambda: 0.0)
    rt = run(p.load_refresh_token(c1, tok.refresh_token))
    run(p.exchange_refresh_token(c1, rt, []))
    with pytest.raises(TokenError) as err:
        run(p.exchange_refresh_token(c1, rt, []))
    assert err.value.error == "invalid_grant"
    assert "REUSED" in capsys.readouterr().err


def test_revoked_pair_is_not_replayed(setup):
    p, c1, _, tok = setup
    first = _refresh(p, c1, tok.refresh_token)
    run(p.revoke_token(auth.RefreshToken(token=first.refresh_token, client_id="c1", scopes=["rose"])))
    with pytest.raises(TokenError):
        _refresh(p, c1, tok.refresh_token)


def test_issued_tokens_never_written_in_plain_text(setup):
    p, c1, _, tok = setup
    first = _refresh(p, c1, tok.refresh_token)
    _refresh(p, c1, tok.refresh_token)
    raw = auth.STATE_PATH.read_text()
    for secret in (tok.refresh_token, first.refresh_token, first.access_token):
        assert secret not in raw
    json.loads(raw)


def test_config_key_default_and_override(settings, monkeypatch):
    assert auth._reuse_grace() == 60.0
    from rose import config

    real = config.load_config
    monkeypatch.setattr(auth, "load_config", lambda: {**real(), "refresh_reuse_grace_seconds": 5})
    assert auth._reuse_grace() == 5.0
    monkeypatch.setattr(auth, "load_config", lambda: {**real(), "refresh_reuse_grace_seconds": "bad"})
    assert auth._reuse_grace() == 60.0
    monkeypatch.setattr(auth, "load_config", lambda: {**real(), "refresh_reuse_grace_seconds": -3})
    assert auth._reuse_grace() == 0.0


def test_reuse_after_grace_revokes_the_whole_sign_in(setup, monkeypatch):
    """RFC 9700: a thief who rotated first must lose access when the owner's stale copy comes back."""
    p, c1, _, tok = setup
    thief = _refresh(p, c1, tok.refresh_token)  # rotated by whoever got there first
    thief2 = _refresh(p, c1, thief.refresh_token)
    other = p._issue("c1", ["rose"], None)  # a separate sign-in of the same client is left alone
    with pytest.raises(TokenError):
        _refresh(p, c1, tok.refresh_token)  # the original, long since spent, comes back
    assert run(p.load_access_token(thief2.access_token)) is None
    assert run(p.load_refresh_token(c1, thief2.refresh_token)) is None
    assert run(p.load_access_token(other.access_token)).client_id == "c1"
