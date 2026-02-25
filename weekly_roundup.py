import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests

JIRA_DOMAIN = "zillowgroup.atlassian.net"

# ✅ TEST MODE: Kay Thomas only
KAY_THOMAS_ACCOUNT_ID = "712020:e42cac78-cbc0-4090-985a-549ad893ef45"

# Optional extra filters you might add later (project = XYZ, etc.)
BASE_JQL = ""  # leave blank for now

CENTRAL_TZ = ZoneInfo("America/Chicago")


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

    # ✅ If you click "Run workflow" (workflow_dispatch), it runs regardless of time.
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
    """
    Most recent Friday 12:00 PM CT at or before now_ct.
    """
    # Python: Monday=0 ... Sunday=6; Friday=4
    days_since_friday = (now_ct.weekday() - 4) % 7
    candidate_date = (now_ct - timedelta(days=days_since_friday)).date()
    candidate_dt = datetime.combine(candidate_date, time(12, 0), tzinfo=CENTRAL_TZ)

    # If it's Friday but before noon, go back one week
    if now_ct.weekday() == 4 and now_ct < candidate_dt:
        candidate_dt -= timedelta(days=7)

    return candidate_dt


def build_jql(start_ct: datetime, end_ct: datetime) -> str:
    """
    Jira JQL expects user references by accountId in Jira Cloud.
    We pass timestamps in Central time with their respective offsets (DST-safe).
    """
    start_str = start_ct.strftime("%Y-%m-%d %H:%M %z")  # includes offset
    end_str = end_ct.strftime("%Y-%m-%d %H:%M %z")      # includes offset

    parts = []
    if BASE_JQL.strip():
        parts.append(f"({BASE_JQL.strip()})")

    parts.append(f"reporter = {KAY_THOMAS_ACCOUNT_ID}")
    parts.append(f'created >= "{start_str}"')
    parts.append(f'created < "{end_str}"')

    return " AND ".join(parts)


def get_issues(config: Config, jql: str) -> list[dict]:
    """
    ✅ Uses NEW Jira Cloud endpoint:
    POST /rest/api/3/search/jql

    Atlassian removed the old /rest/api/3/search endpoint (410).
    """
    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    issues: list[dict] = []
    next_token: str | None = None

    while True:
        body: dict = {
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
            headers=headers,
            auth=(config.jira_email, config.jira_api_token),
            json=body,
            timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

        data = resp.json()

        batch = data.get("issues", [])
        issues.extend(batch)

        # New pagination model for this endpoint
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
    payload = {"text": text}
    resp = requests.post(webhook, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack webhook error {resp.status_code}: {resp.text}")


def main() -> int:
    config = get_config()
    now_ct = central_now()

    # ✅ Scheduled behavior: only post during Friday 12pm CT hour
    # Manual run (workflow_dispatch) overrides this.
    if not config.force_run:
        if not (now_ct.weekday() == 4 and now_ct.hour == 12):
            print("Not within Friday 12pm CT hour; skipping.")
            return 0

    start_ct = last_friday_noon_central(now_ct)
    end_ct = now_ct

    jql = build_jql(start_ct, end_ct)
    print(f"Window: {start_ct.isoformat()} → {end_ct.isoformat()}")
    print(f"JQL: {jql}")

    issues = get_issues(config, jql)

    header = (
        "*Weekly Jira Roundup (Kay Thomas)*\n"
        f"Window: {start_ct.strftime('%a %b %d, %Y %I:%M %p CT')} → "
        f"{end_ct.strftime('%a %b %d, %Y %I:%M %p CT')}\n"
        f"Total: {len(issues)}"
    )

    if not issues:
        post_to_slack(config.slack_webhook, header + "\n• No issues found.")
        return 0

    lines = [format_issue_line(i) for i in issues]
    body = "\n".join(lines)

    # Slack message size guard
    msg = header + "\n" + body
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
