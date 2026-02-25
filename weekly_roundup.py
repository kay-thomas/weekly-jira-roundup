import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests

JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_API_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"

# ✅ TEST MODE: Kay Thomas only
KAY_THOMAS_ACCOUNT_ID = "712020:e42cac78-cbc0-4090-985a-549ad893ef45"

BASE_JQL = ""  # optional, leave blank for now

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

    # ✅ If you click "Run workflow" (workflow_dispatch), it will run no matter what time it is.
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
    Times are passed in Central time with offset.
    """
    start_str = start_ct.strftime("%Y-%m-%d %H:%M")
    end_str = end_ct.strftime("%Y-%m-%d %H:%M")
    tz_offset = end_ct.strftime("%z")  # e.g. -0600 / -0500

    parts = []
    if BASE_JQL.strip():
        parts.append(f"({BASE_JQL.strip()})")

    parts.append(f"reporter = {KAY_THOMAS_ACCOUNT_ID}")
    parts.append(f'created >= "{start_str} {tz_offset}"')
    parts.append(f'created < "{end_str} {tz_offset}"')

    return " AND ".join(parts)


def jira_auth_header(email: str, token: str) -> dict:
    basic = f"{email}:{token}".encode("utf-8")
    return {"Authorization": "Basic " + requests.utils.to_native_string(__import__("base64").b64encode(basic))}


def get_issues(config: Config, jql: str) -> list[dict]:
    url = f"{JIRA_API_BASE}/search"
    headers = {
        **jira_auth_header(config.jira_email, config.jira_api_token),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    issues: list[dict] = []
    start_at = 0
    max_results = 50

    while True:
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": "summary,issuetype,project,priority,status,created,reporter",
        }
        resp = requests.get(url, headers=headers, params=params, timeout=30)

        if resp.status_code >= 400:
            raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

        data = resp.json()
        batch = data.get("issues", [])
        issues.extend(batch)

        total = data.get("total", 0)
        start_at += len(batch)
        if start_at >= total or not batch:
            break

    return issues


def format_issue_line(issue: dict) -> str:
    key = issue.get("key", "UNKNOWN")
    fields = issue.get("fields", {})
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

    # ✅ Scheduled behavior: only post at Friday 12pm CT (your workflow already triggers at that time),
    # but if something triggers outside that time, we don't want a hard fail—just skip.
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

    header = f"*Weekly Jira Roundup (Kay Thomas)*\nWindow: {start_ct.strftime('%a %b %d, %Y %I:%M %p CT')} → {end_ct.strftime('%a %b %d, %Y %I:%M %p CT')}\nTotal: {len(issues)}"
    if not issues:
        post_to_slack(config.slack_webhook, header + "\n• No issues found.")
        return 0

    lines = [format_issue_line(i) for i in issues]
    body = "\n".join(lines)

    # Slack message size guard (simple)
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
