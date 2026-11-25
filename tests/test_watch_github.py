"""rose.watch GitHub alerts: CI passing again, new reviews / review comments, silent seeding."""

from __future__ import annotations

import pytest

from rose import watch

PR = {"repo": "me/app", "number": 7, "title": "Add thing", "url": "https://github.com/me/app/pull/7"}


@pytest.fixture
def gh(monkeypatch):
    """Fake GitHub: mutate `world` between ticks; pushes are collected."""
    from rose.tools import github
    world = {"failing": [], "pending": [], "reviews": [], "comments": [], "authored": [PR]}
    pushes = []
    monkeypatch.setattr(github, "github_my_prs", lambda: {"authored": world["authored"], "review_requested": []})
    monkeypatch.setattr(github, "github_pr_status", lambda r, n: {"failing": world["failing"],
                                                                  "pending": world["pending"], "checks_total": 2})

    def api(path):
        if path == "user":
            return {"login": "me"}
        return world["reviews"] if "/reviews" in path else world["comments"]

    monkeypatch.setattr(watch, "_gh_api", api)
    monkeypatch.setattr(watch, "push", lambda title, message="", **kw: pushes.append((title, message, kw)))
    return world, pushes


def _review(i, state, body="", login="alice"):
    return {"id": i, "state": state, "body": body, "user": {"login": login, "type": "User"},
            "html_url": f"https://x/{i}"}


def test_first_run_is_silent(gh):
    world, pushes = gh
    world["failing"] = ["test"]
    world["reviews"] = [_review(1, "APPROVED")]
    world["comments"] = [{"id": 5, "body": "nit", "user": {"login": "bob"}}]
    state = {}
    watch._tick_github(state)
    assert pushes == []
    assert state["ci_red"] == ["me/app#7"] and state["pr_activity_seen"]["me/app#7"] == {"r": 1, "c": 5}


def test_ci_passing_again(gh):
    world, pushes = gh
    state = {}
    watch._tick_github(state)
    world["failing"] = ["test"]
    watch._tick_github(state)
    assert pushes[-1][0] == "❌ CI failing on your PR"
    world["failing"], world["pending"] = [], ["test"]
    watch._tick_github(state)  # still running: no news yet
    assert len(pushes) == 1
    world["pending"] = []
    watch._tick_github(state)
    assert pushes[-1][0] == "✅ CI passing again" and len(pushes) == 2
    watch._tick_github(state)
    assert len(pushes) == 2


def test_upgrade_seeds_red_from_ci_failing(gh):
    world, pushes = gh
    state = {"github_started": True, "ci_failing": ["me/app#7:test"]}
    watch._tick_github(state)  # green now, was failing under the old code
    assert [p[0] for p in pushes] == ["✅ CI passing again"]


def test_new_reviews_and_comments(gh):
    world, pushes = gh
    state = {}
    watch._tick_github(state)
    world["reviews"] = [_review(10, "CHANGES_REQUESTED", "Please rename"), _review(11, "COMMENTED"),
                        _review(12, "APPROVED", login="me")]
    world["comments"] = [{"id": 20, "body": "why?", "user": {"login": "alice"}},
                         {"id": 21, "body": "bot", "user": {"login": "ci", "type": "Bot"}}]
    watch._tick_github(state)
    assert len(pushes) == 1
    title, msg, kw = pushes[0]
    assert title == "🔁 Changes requested" and "Please rename" in msg and "+1 inline comment" in msg
    assert kw["priority"] == "high"
    watch._tick_github(state)
    assert len(pushes) == 1  # nothing new
    world["comments"].append({"id": 30, "body": "ok now", "user": {"login": "alice"}, "html_url": "u"})
    watch._tick_github(state)
    assert pushes[-1][0] == "💬 New review comment" and "ok now" in pushes[-1][1]
    world["reviews"].append(_review(40, "APPROVED"))
    watch._tick_github(state)
    assert pushes[-1][0] == "✅ PR approved"


def test_closed_pr_drops_out(gh):
    world, pushes = gh
    state = {}
    world["failing"] = ["test"]
    watch._tick_github(state)
    world["authored"] = []
    watch._tick_github(state)
    assert state["ci_red"] == [] and state["pr_activity_seen"] == {} and pushes == []


def test_watch_status_lists_new_watchers():
    w = watch.watch_status()["watching"]
    assert "CI passing again" in w and any(x.startswith("work log") for x in w)
    assert any(x.startswith("context triggers") for x in w)
