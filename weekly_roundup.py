import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

import requests

JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_API_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"

CENTRAL_TZ = ZoneInfo("America/Chicago")

# Optional: extra constraints (project = ABC, labels, etc.)
BASE_JQL = ""  # leave blank for now


# Team leads (test now; can hardcode all accountIds later)
# NOTE: We will EXCLUDE Adam + Marissa as requested.
TEAM_LEADS: List[Dict[str, Optional[str]]] = [
    {"name": "Kay Thomas", "accountId": "712020:e42cac78-cbc0-4090-985a-549ad893ef45"},
    {"name": "Maryuri Orellana", "accountId": None},
    {"name": "Emmanuel Whyte", "accountId": None},
    {"name": "Evan Sandora", "accountId": None},
    {"name": "Kyler VanderValk", "accountId": None},
    {"name": "Zane Roberts", "accountId": None},
]


@dataclass
class Config:
    jira_email: str
    jira_api_token: str
    slack_webhook: str
    event_name: str
    force_run: bool


def require_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def parse_bool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def get_config() -> Config:
    jira_email = require_env("JIRA_EMAIL")
    jira_api_token = require_env("JIRA_API_TOKEN")
    slack_webhook = require_env("SLACK_WEBHOOK")

    event_name = os.getenv("GITHUB_EVENT_NAME", "").strip()
    force_run_env = os.getenv("FORCE_RUN", "").strip()

    # Manual "Run workflow" or FORCE_RUN=true should run regardless of time.
    force_run = parse_bool(force_run_env) or (event_name == "workflow_dispatch")

    return Config(
        jira_email=jira_email,
        jira_api_token=jira_api_token,
        slack_webhook=slack_webhook,
        event_name=event_name,
        force_run=force_run,
    )


def central_now() -> datetime:
    return datetime.now(tz=CENTRAL_TZ)


def last_friday_noon_central(now_ct: datetime) -> datetime:
    """Most recent Friday 12:00 PM CT at or before now_ct."""
    # Python: Monday=0 ... Sunday=6; Friday=4
    days_since_friday = (now_ct.weekday() - 4) % 7
    candidate_date = (now_ct - timedelta(days=days_since_friday)).date()
    candidate_dt = datetime.combine(candidate_date, time(12, 0), tzinfo=CENTRAL_TZ)

    # If it's Friday but before noon, go back one week
    if now_ct.weekday() == 4 and now_ct < candidate_dt:
        candidate_dt -= timedelta(days=7)

    return candidate_dt


def jira_user_search(config: Config, query: str) -> List[dict]:
    """
    Search users by name/email fragment.
    Jira Cloud endpoint: GET /rest/api/3/user/search?query=...
    """
    url = f"{JIRA_API_BASE}/user/search"
    resp = requests.get(
        url,
        headers={"Accept": "application/json"},
        auth=(config.jira_email, config.jira_api_token),
        params={"query": query},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Jira user search error {resp.status_code}: {resp.text}")
    data = resp.json()
    return data if isinstance(data, list) else []


def resolve_account_id(config: Config, display_name: str) -> Optional[str]:
    """
    Resolve a Jira Cloud accountId for a display name.
    We DO NOT use names in JQL — names are only used here for lookup.
    Selection priority:
      1) active + exact displayName match (case-insensitive)
      2) active + contains match
      3) first active
      4) first result
    """
    results = jira_user_search(config, display_name)
    if not results:
        return None

    dn_lower = display_name.strip().lower()

    def is_active(u: dict) -> bool:
        return bool(u.get("active", False))

    def dn(u: dict) -> str:
        return str(u.get("displayName", "")).strip()

    # 1) active exact match
    for u in results:
        if is_active(u) and dn(u).lower() == dn_lower and u.get("accountId"):
            return u["accountId"]

    # 2) active contains match
    for u in results:
        if is_active(u) and dn_lower in dn(u).lower() and u.get("accountId"):
            return u["accountId"]

    # 3) first active
    for u in results:
        if is_active(u) and u.get("accountId"):
            return u["accountId"]

    # 4) first result
    first = results[0]
    return first.get("accountId")


def get_team_lead_ids(config: Config) -> Tuple[List[str], List[str]]:
    """
    Returns (account_ids, warnings).
    """
    ids: List[str] = []
    warnings: List[str] = []

    for tl in TEAM_LEADS:
        name = (tl.get("name") or "").strip()
        aid = (tl.get("accountId") or "").strip()

        if aid:
            ids.append(aid)
            continue

        resolved = resolve_account_id(config, name)
        if not resolved:
            warnings.append(f"Could not resolve accountId for: {name}")
            continue

        ids.append(resolved)

    # Deduplicate while preserving order
    seen = set()
    deduped: List[str] = []
    for x in ids:
        if x not in seen:
            deduped.append(x)
            seen.add(x)

    return deduped, warnings


def build_jql(account_ids: List[str], start_ct: datetime, end_ct: datetime) -> str:
    """
    Jira JQL expects user references by accountId in Jira Cloud.
    Times are passed in Central time with offset.
    """
    if not account_ids:
        raise RuntimeError("No Jira accountIds available for reporter filter.")

    start_str = start_ct.strftime("%Y-%m-%d %H:%M")
    end_str = end_ct.strftime("%Y-%m-%d %H:%M")
    tz_offset = end_ct.strftime("%z")  # e.g. -0600 / -0500

    parts = []
    if BASE_JQL.strip():
        parts.append(f"({BASE_JQL.strip()})")

    # reporter IN (<accountId>, <accountId>, ...)
    reporter_clause = "reporter in (" + ", ".join(account_ids) + ")"
    parts.append(reporter_clause)

    parts.append(f'created >= "{start_str} {tz_offset}"')
    parts.append(f'created < "{end_str} {tz_offset}"')

    return " AND ".join(parts)


def get_issues(config: Config, jql: str) -> List[dict]:
    """
    Jira Cloud NEW endpoint:
    POST /rest/api/3/search/jql
    """
    url = f"{JIRA_API_BASE}/search/jql"

    issues: List[dict] = []
    next_token: Optional[str] = None

    while True:
        body = {
            "jql": jql,
            "maxResults": 100,
            "fields": [
                "summary",
                "issuetype",
                "project",
                "priority",
                "status",
                "created",
                "reporter",
            ],
            "fieldsByKeys": True,
        }
        if next_token:
            body["nextPageToken"] = next_token

        resp = requests.post(
            url,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            auth=(config.jira_email, config.jira_api_token),
            json=body,
            timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

        data = resp.json()
        batch = data.get("issues", []) or []
        issues.extend(batch)

        next_token = data.get("nextPageToken")
        is_last = data.get("isLast", True)

        if is_last or not next_token:
            break

    return issues


def format_issue_line(issue: dict) -> str:
    key = issue.get("key", "UNKNOWN")
    fields = issue.get("fields", {}) or {}
    summary = (fields.get("summary") or "").strip()
    status = (fields.get("status") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    issue_url = f"https://{JIRA_DOMAIN}/browse/{key}"

    bits = [f"<{issue_url}|{key}>", summary]
    meta = [x for x in [status, priority] if x]
    if meta:
        bits.append(f"({', '.join(meta)})")

    return "• " + " — ".join(bits)


def post_to_slack(webhook: str, text: str) -> None:
    resp = requests.post(webhook, json={"text": text}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack webhook error {resp.status_code}: {resp.text}")


def main() -> int:
    config = get_config()
    now_ct = central_now()

    # Scheduled behavior:
    # - If schedule triggers outside the intended hour, do not fail the job; just skip.
    # Manual workflow_dispatch always runs.
    if not config.force_run:
        if not (now_ct.weekday() == 4 and now_ct.hour == 12):
            print("Not within Friday 12pm CT hour; skipping.")
            return 0

    start_ct = last_friday_noon_central(now_ct)
    end_ct = now_ct

    account_ids, warnings = get_team_lead_ids(config)
    jql = build_jql(account_ids, start_ct, end_ct)

    print(f"Window: {start_ct.isoformat()} → {end_ct.isoformat()}")
    print(f"Resolved reporter accountIds ({len(account_ids)}): {account_ids}")
    if warnings:
        print("WARNINGS:")
        for w in warnings:
            print(f" - {w}")
    print(f"JQL: {jql}")

    issues = get_issues(config, jql)

    # Slack header
    header = (
        "*Weekly Jira Roundup (Team Leads)*\n"
        f"Window: {start_ct.strftime('%a %b %d, %Y %I:%M %p CT')} → {end_ct.strftime('%a %b %d, %Y %I:%M %p CT')}\n"
        f"Reporters tracked: {len(account_ids)}\n"
        f"Total: {len(issues)}"
    )

    # Include warnings in Slack (helpful during test phase)
    warn_block = ""
    if warnings:
        warn_lines = "\n".join([f"• {w}" for w in warnings])
        warn_block = "\n*Lookup warnings:*\n" + warn_lines

    if not issues:
        post_to_slack(config.slack_webhook, header + warn_block + "\n• No issues found.")
        return 0

    lines = [format_issue_line(i) for i in issues]
    body = "\n".join(lines)

    msg = header + warn_block + "\n" + body

    # Slack message size guard (simple)
    if len(msg) > 35000:
        msg = header + warn_block + "\n" + "\n".join(lines[:150]) + "\n• (truncated)"

    post_to_slack(config.slack_webhook, msg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
