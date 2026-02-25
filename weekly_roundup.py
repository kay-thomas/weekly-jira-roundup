import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import requests

JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_API_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"

CENTRAL_TZ = ZoneInfo("America/Chicago")

# ✅ Team Leads (exclude Adam + Marissa per your note)
TEAM_LEAD_DISPLAY_NAMES = [
    "Kay Thomas",
    "Maryuri Orellana",
    "Emmanuel Whyte",
    "Evan Sandora",
    "Kyler VanderValk",
    "Zane Roberts",
]

BASE_JQL = ""  # optional: e.g. 'project in (ABC, FCOM, FESC, DEEP) AND statusCategory != Done'


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

    # ✅ Manual runs (workflow_dispatch) always run.
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
    # Monday=0 ... Sunday=6; Friday=4
    days_since_friday = (now_ct.weekday() - 4) % 7
    candidate_date = (now_ct - timedelta(days=days_since_friday)).date()
    candidate_dt = datetime.combine(candidate_date, time(12, 0), tzinfo=CENTRAL_TZ)

    # If it's Friday but before noon, go back one week
    if now_ct.weekday() == 4 and now_ct < candidate_dt:
        candidate_dt -= timedelta(days=7)

    return candidate_dt


def jira_get(config: Config, path: str, params: dict | None = None) -> dict | list:
    url = f"{JIRA_API_BASE}{path}"
    resp = requests.get(
        url,
        params=params or {},
        auth=(config.jira_email, config.jira_api_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Jira API GET {path} error {resp.status_code}: {resp.text}")
    return resp.json()


def resolve_account_ids(config: Config, display_names: list[str]) -> dict[str, str]:
    """
    Resolve Jira Cloud accountIds from display names using:
      GET /rest/api/3/user/search?query=<name>&maxResults=50
    We then select the exact displayName match (case-insensitive). If none,
    we fall back to the first active result.
    """
    resolved: dict[str, str] = {}

    for name in display_names:
        results = jira_get(config, "/user/search", params={"query": name, "maxResults": 50})
        if not isinstance(results, list) or not results:
            raise RuntimeError(f'Could not resolve Jira accountId for "{name}" (no results).')

        # Prefer exact (case-insensitive) displayName match and active users
        exact = [
            u for u in results
            if (u.get("displayName", "") or "").strip().lower() == name.strip().lower()
            and u.get("active", True) is True
        ]
        if exact:
            resolved[name] = exact[0]["accountId"]
            continue

        # Otherwise pick first active result
        active = [u for u in results if u.get("active", True) is True]
        pick = active[0] if active else results[0]
        if "accountId" not in pick:
            raise RuntimeError(f'Could not resolve Jira accountId for "{name}" (missing accountId).')

        resolved[name] = pick["accountId"]

    return resolved


def build_jql(start_ct: datetime, end_ct: datetime, account_ids: list[str]) -> str:
    """
    IMPORTANT: Jira JQL date parsing is most reliable WITHOUT an explicit timezone suffix.
    Jira interprets the timestamps in the querying user's timezone/profile.
    So we send: YYYY-MM-DD HH:mm (no -0600).
    """
    start_str = start_ct.strftime("%Y-%m-%d %H:%M")
    end_str = end_ct.strftime("%Y-%m-%d %H:%M")

    parts = []
    if BASE_JQL.strip():
        parts.append(f"({BASE_JQL.strip()})")

    # Reporter list (accountIds)
    # Jira Cloud JQL supports: reporter in (<accountId>, <accountId>, ...)
    reporters = ", ".join(account_ids)
    parts.append(f"reporter in ({reporters})")

    parts.append(f'created >= "{start_str}"')
    parts.append(f'created < "{end_str}"')

    # Nice ordering for readability
    parts.append("ORDER BY created DESC")

    return " AND ".join(parts)


def get_issues(config: Config, jql: str) -> list[dict]:
    """
    Uses NEW Jira Cloud endpoint:
      POST /rest/api/3/search/jql

    Paginates via nextPageToken.
    """
    url = f"{JIRA_API_BASE}/search/jql"

    issues: list[dict] = []
    next_token = None

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
        }
        if next_token:
            body["nextPageToken"] = next_token

        resp = requests.post(
            url,
            auth=(config.jira_email, config.jira_api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
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
    reporter = (fields.get("reporter") or {}).get("displayName", "")

    issue_url = f"https://{JIRA_DOMAIN}/browse/{key}"

    bits = [f"<{issue_url}|{key}>", summary]
    meta = [x for x in [reporter, status, priority] if x]
    if meta:
        bits.append(f"({', '.join(meta)})")

    return "• " + " — ".join(bits)


def post_to_slack(webhook: str, text: str) -> None:
    resp = requests.post(webhook, json={"text": text}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack webhook error {resp.status_code}: {resp.text}")


def jira_search_link(jql: str) -> str:
    # Works in browser for quick verification/debugging.
    return f"https://{JIRA_DOMAIN}/issues/?jql={quote_plus(jql)}"


def main() -> int:
    config = get_config()
    now_ct = central_now()

    # ✅ Scheduled behavior: only post at Friday 12pm CT unless forced.
    if not config.force_run:
        if not (now_ct.weekday() == 4 and now_ct.hour == 12):
            print("Not within Friday 12pm CT hour; skipping.")
            return 0

    start_ct = last_friday_noon_central(now_ct)
    end_ct = now_ct

    # ✅ Resolve accountIds dynamically (so you don't have to hardcode them)
    name_to_id = resolve_account_ids(config, TEAM_LEAD_DISPLAY_NAMES)
    account_ids = list(name_to_id.values())

    jql = build_jql(start_ct, end_ct, account_ids)

    print(f"Window: {start_ct.isoformat()} → {end_ct.isoformat()}")
    print(f"Reporters tracked: {len(account_ids)}")
    print(f"JQL: {jql}")
    print(f"Verify: {jira_search_link(jql)}")

    issues = get_issues(config, jql)

    header = (
        "*Weekly Jira Roundup (Team Leads)*\n"
        f"Window: {start_ct.strftime('%a %b %d, %Y %I:%M %p CT')} → {end_ct.strftime('%a %b %d, %Y %I:%M %p CT')}\n"
        f"Reporters tracked: {len(account_ids)}\n"
        f"Total: {len(issues)}\n"
        f"<{jira_search_link(jql)}|Open this JQL in Jira>"
    )

    if not issues:
        post_to_slack(config.slack_webhook, header + "\n• No issues found.")
        return 0

    lines = [format_issue_line(i) for i in issues]
    msg = header + "\n" + "\n".join(lines)

    # Slack message size guard (simple)
    if len(msg) > 35000:
        msg = header + "\n" + "\n".join(lines[:150]) + "\n• (truncated)"

    post_to_slack(config.slack_webhook, msg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
