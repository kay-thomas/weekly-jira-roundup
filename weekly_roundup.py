import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import requests

# =========================
# JIRA + SLACK CONFIG
# =========================

DEFAULT_JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_DOMAIN = os.getenv("JIRA_DOMAIN", DEFAULT_JIRA_DOMAIN).strip() or DEFAULT_JIRA_DOMAIN
JIRA_API_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"

BASE_JQL = os.getenv("BASE_JQL", "").strip()  # optional

CENTRAL_TZ = ZoneInfo("America/Chicago")
UTC_TZ = ZoneInfo("UTC")

# =========================
# ✅ TEAM LEADS (accountIds)
# (Using the 6 accountIds from your working run/logs)
# =========================

TEAM_LEAD_REPORTERS = {
    "Kay Thomas": "712020:e42cac78-cbc0-4090-985a-549ad893ef45",
    "Maryuri Orellana": "712020:894cfdc5-4bcb-4380-acf2-e280c363d6dd",
    "Emmanuel Whyte": "712020:a520d240-bce6-4293-8101-f8d36195930b",
    "Evan Sandora": "712020:453a4414-1382-4054-b762-a6191d545e65",
    "Kyler VanderValk": "712020:f78b4a9f-5620-414c-ba21-b2aebf49ce33",
    "Zane Roberts": "712020:663a58b0-f773-4b95-b2e2-77b78495cdcf",
}

# =========================
# TYPES
# =========================

@dataclass
class Config:
    jira_email: str
    jira_api_token: str
    slack_webhook: str
    event_name: str
    force_run: bool


# =========================
# ENV + TIME HELPERS
# =========================

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
    slack_webhook = require_env("SLACK_WEBHOOK")  # <-- your existing secret name

    event_name = os.getenv("GITHUB_EVENT_NAME", "").strip()
    force_run_env = os.getenv("FORCE_RUN", "").strip()

    # If workflow_dispatch OR FORCE_RUN=true, run regardless of time
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

    # If it's Friday but before noon, go back a week
    if now_ct.weekday() == 4 and now_ct < candidate_dt:
        candidate_dt -= timedelta(days=7)

    return candidate_dt


def to_utc_jql(dt_ct: datetime) -> str:
    """
    Convert Central dt -> UTC and format with explicit offset so Jira can't misinterpret timezone.
    Example: 2026-02-25 16:33 +0000
    """
    dt_utc = dt_ct.astimezone(UTC_TZ)
    return dt_utc.strftime("%Y-%m-%d %H:%M %z")  # includes +0000


# =========================
# JQL + JIRA SEARCH
# =========================

def build_jql(start_ct: datetime, end_ct: datetime) -> str:
    """
    Build JQL using accountId list and created window in UTC with explicit +0000 offset.
    This prevents Jira profile timezone differences from excluding issues.
    """
    reporter_ids = list(TEAM_LEAD_REPORTERS.values())
    reporters = ", ".join(reporter_ids)

    start_str = to_utc_jql(start_ct)
    end_str = to_utc_jql(end_ct)

    parts = []
    if BASE_JQL:
        parts.append(f"({BASE_JQL})")

    parts.append(f"reporter in ({reporters})")
    parts.append(f'created >= "{start_str}"')
    parts.append(f'created < "{end_str}"')

    # ORDER BY is part of the JQL string, not an AND clause
    return " AND ".join(parts) + " ORDER BY created DESC"


def get_issues(config: Config, jql: str) -> list[dict]:
    """
    NEW Jira Cloud endpoint:
    POST /rest/api/3/search/jql
    Supports pagination via nextPageToken.
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
            "fieldsByKeys": True,
        }
        if next_token:
            body["nextPageToken"] = next_token

        resp = requests.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            auth=(config.jira_email, config.jira_api_token),
            json=body,
            timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

        data = resp.json()
        batch = data.get("issues", [])
        issues.extend(batch)

        next_token = data.get("nextPageToken")
        is_last = data.get("isLast", True)

        if is_last or not next_token:
            break

    return issues


# =========================
# SLACK FORMATTING
# =========================

def format_issue_line(issue: dict) -> str:
    key = issue.get("key", "UNKNOWN")
    fields = issue.get("fields", {})

    summary = (fields.get("summary") or "").strip()
    status = (fields.get("status") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "Not Set")
    reporter = (fields.get("reporter") or {}).get("displayName", "Unknown")

    issue_url = f"https://{JIRA_DOMAIN}/browse/{key}"

    # • KEY — Summary (Reporter, Status, Priority)
    return f"• <{issue_url}|{key}> — {summary} ({reporter}, {status}, {priority})"


def jql_to_browse_url(jql: str) -> str:
    return f"https://{JIRA_DOMAIN}/issues/?jql={quote_plus(jql)}"


def post_to_slack(webhook: str, text: str) -> None:
    resp = requests.post(webhook, json={"text": text}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack webhook error {resp.status_code}: {resp.text}")


# =========================
# MAIN
# =========================

def main() -> int:
    config = get_config()
    now_ct = central_now()

    # Scheduled behavior: only post at Friday 12pm CT unless forced
    if not config.force_run:
        if not (now_ct.weekday() == 4 and now_ct.hour == 12):
            print("Not within Friday 12pm CT hour; skipping.")
            return 0

    start_ct = last_friday_noon_central(now_ct)
    end_ct = now_ct

    jql = build_jql(start_ct, end_ct)

    print(f"Window: {start_ct.isoformat()} → {end_ct.isoformat()}")
    print(f"Reporters tracked: {len(TEAM_LEAD_REPORTERS)}")
    print(f"JQL: {jql}")
    print(f"Verify: {jql_to_browse_url(jql)}")

    issues = get_issues(config, jql)

    header = (
        "*Weekly Jira Roundup (Team Leads)*\n"
        f"Window: {start_ct.strftime('%a %b %d, %Y %I:%M %p CT')} → {end_ct.strftime('%a %b %d, %Y %I:%M %p CT')}\n"
        f"Reporters tracked: {len(TEAM_LEAD_REPORTERS)}\n"
        f"Total: {len(issues)}\n"
        f"<{jql_to_browse_url(jql)}|Open this JQL in Jira>"
    )

    if not issues:
        post_to_slack(config.slack_webhook, header + "\n• No issues found.")
        return 0

    lines = [format_issue_line(i) for i in issues]

    msg = header + "\n" + "\n".join(lines)

    # Slack message size guard
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
