"""GitHub monitoring through the gh CLI (read-only). Uses gh's own login."""

from __future__ import annotations

import json
import re

from rose.common import ToolError, require, run

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _gh(args: list[str]):
    require("gh", "sudo apt install gh && gh auth login")
    try:
        out = run(["gh", *args], timeout=30)
    except ToolError as e:
        if "auth login" in str(e) or "not logged" in str(e).lower():
            raise ToolError("gh is not logged in. Run: gh auth login")
        raise
    return json.loads(out) if out.strip() else None


def _repo(repo: str) -> str:
    if not _REPO.match(repo):
        raise ToolError("repo must look like owner/name")
    return repo


_PR_FIELDS = "repository,number,title,url,updatedAt,isDraft"


def github_my_prs() -> dict:
    """Your open PRs and PRs waiting for your review, across GitHub."""
    mine = _gh(["search", "prs", "--author=@me", "--state=open", "--limit", "30", "--json", _PR_FIELDS])
    review = _gh(["search", "prs", "--review-requested=@me", "--state=open", "--limit", "30", "--json", _PR_FIELDS])
    fmt = lambda prs: [  # noqa: E731
        {"repo": p["repository"]["nameWithOwner"], "number": p["number"], "title": p["title"],
         "draft": p["isDraft"], "updated": p["updatedAt"], "url": p["url"]} for p in prs or []]
    return {"authored": fmt(mine), "review_requested": fmt(review)}


def github_pr_status(repo: str, number: int) -> dict:
    """One PR's state, review decision, mergeability and CI check results."""
    pr = _gh(["pr", "view", str(number), "-R", _repo(repo), "--json",
              "title,state,isDraft,reviewDecision,mergeable,url,headRefName,statusCheckRollup"])
    checks = []
    for c in pr.get("statusCheckRollup") or []:
        checks.append({"name": c.get("name") or c.get("context"),
                       "status": c.get("status") or c.get("state"),
                       "conclusion": c.get("conclusion")})
    failing = [c["name"] for c in checks if (c["conclusion"] or "").upper() in ("FAILURE", "TIMED_OUT", "CANCELLED")
               or (c["status"] or "").upper() in ("FAILURE", "ERROR")]
    pending = [c["name"] for c in checks if (c["status"] or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")]
    return {"title": pr["title"], "state": pr["state"], "draft": pr["isDraft"], "branch": pr["headRefName"],
            "review_decision": pr["reviewDecision"], "mergeable": pr["mergeable"], "url": pr["url"],
            "checks_total": len(checks), "failing": failing, "pending": pending}


def github_ci_runs(repo: str, branch: str = "", limit: int = 10) -> list[dict]:
    """Recent GitHub Actions runs for a repo (optionally one branch)."""
    args = ["run", "list", "-R", _repo(repo), "--limit", str(max(1, min(limit, 50))), "--json",
            "workflowName,displayTitle,headBranch,status,conclusion,createdAt,url,event"]
    if branch:
        if not re.match(r"^[\w./-]+$", branch):
            raise ToolError("Invalid branch name.")
        args += ["--branch", branch]
    return _gh(args) or []


def github_notifications(limit: int = 20) -> list[dict]:
    """Unread GitHub notifications (review requests, mentions, CI failures...)."""
    items = _gh(["api", f"notifications?per_page={max(1, min(limit, 50))}"]) or []
    return [{"repo": n["repository"]["full_name"], "type": n["subject"]["type"], "title": n["subject"]["title"],
             "reason": n["reason"], "updated": n["updated_at"]} for n in items]
