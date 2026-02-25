import os
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Jira domain
JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_URL = f"https://{JIRA_DOMAIN}/rest/api/3/search"

# Secrets (set in GitHub → Settings → Secrets → Actions)
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK")

# Team Leads (reporters)
REPORTERS = [
    "Emmanuel Whyte",
    "Kay Thomas",
    "Evan Sandora",
    "Kyler VanderValk",
    "Maryuri Orellana",
    "Melissa Kayle",
    "Adam MeClure",
    "Zane Roberts",
]

def compute_window_utc(now_utc: datetime):
    """
    Window:
      Start = Last Friday 12:00 PM America/Chicago
      End   = This Friday 12:00 PM America/Chicago (end-exclusive)
    """
    central = ZoneInfo("America/Chicago")
    now_central = now_utc.astimezone(central)

    # Find most recent Friday (Mon=0 .. Sun=6, Fri=4)
    days_since_friday = (now_central.weekday() - 4) % 7
    this_friday_noon = (now_central - timedelta(days=days_since_friday)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )

    window_end_central = this_friday_noon
    window_start_central = window_end_central - timedelta(days=7)

    return (
        window_start_central.astimezone(timezone.utc),
        window_end_central.astimezone(timezone.utc),
        window_start_central,
        window_end_central,
    )

def jira_dt(dt_utc: datetime) -> str:
    return dt_utc.strftime("%Y-%m-%d %H:%M")

def build_jql(start_utc: datetime, end_utc: datetime) -> str:
    reporters = ", ".join(f"\"{r}\"" for r in REPORTERS)
    return f"""
reporter IN ({reporters})
AND created >= "{jira_dt(start_utc)}"
AND created < "{jira_dt(end_utc)}"
ORDER BY created DESC
""".strip()

def get_issues(jql: str):
    resp = requests.get(
        JIRA_URL,
        headers={"Accept": "application/json"},
        params={"jql": jql, "maxResults": 100, "fields": "summary"},
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("issues", [])

def format_message(issues, start_central: datetime, end_central: datetime) -> str:
    header = (
        "📊 *Weekly TL Jira Roundup*\n"
        f"*Window:* {start_central.strftime('%b %d, %I:%M %p %Z')} → "
        f"{end_central.strftime('%b %d, %I:%M %p %Z')} (end-exclusive)"
    )

    if not issues:
        return f"{header}\n\nNo tickets created in this window."

    lines = []
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        link = f"https://{JIRA_DOMAIN}/browse/{key}"
        lines.append(f"• <{link}|{key}> – {summary}")

    return f"{header}\n\n*Total Created:* {len(issues)}\n\n" + "\n".join(lines)

def post_to_slack(text: str):
    resp = requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=30)
    resp.raise_for_status()

def should_post_now(now_utc: datetime) -> bool:
    """
    GitHub cron runs in UTC.
    We schedule two UTC times and only post if it's exactly
    Friday 12 PM America/Chicago.
    """
    central = ZoneInfo("America/Chicago")
    now_c = now_utc.astimezone(central)
    return (now_c.weekday() == 4) and (now_c.hour == 12)

if __name__ == "__main__":
    now_utc = datetime.now(timezone.utc)

    if not should_post_now(now_utc):
        raise SystemExit("Not within Friday 12pm CT hour; skipping.")

    start_utc, end_utc, start_c, end_c = compute_window_utc(now_utc)
    jql = build_jql(start_utc, end_utc)
    issues = get_issues(jql)
    msg = format_message(issues, start_c, end_c)
    post_to_slack(msg)
