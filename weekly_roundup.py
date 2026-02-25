import os
import sys
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

# =========================
# CONFIGURATION
# =========================

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

# Business timezone (anchor here)
CENTRAL_TZ = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# Optional additional JQL (can leave empty)
BASE_JQL = os.getenv("BASE_JQL", "").strip()

# =========================
# TEAM LEAD EMAILS
# =========================

TEAM_LEAD_EMAILS = [
    "kay.thomas@followupboss.com",
    "maryuri.orellana@followupboss.com",
    "emmanuel.whyte@followupboss.com",
    "evan.sandora@followupboss.com",
    "kyler.vandervalk@followupboss.com",
    "zane.roberts@followupboss.com",
]

# =========================
# HELPERS
# =========================

def get_week_window():
    """
    Window:
    Friday 12:00 PM Central
    → Now (when workflow runs)
    """

    now_ct = datetime.now(CENTRAL_TZ)

    # Find most recent Friday
    weekday = now_ct.weekday()
    days_since_friday = (weekday - 4) % 7
    last_friday = now_ct - timedelta(days=days_since_friday)

    start_ct = last_friday.replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )

    end_ct = now_ct

    return start_ct, end_ct


def get_account_id(email: str) -> str:
    url = f"{JIRA_BASE_URL}/rest/api/3/user/search"
    resp = requests.get(
        url,
        params={"query": email},
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Failed to resolve accountId for {email}: {resp.text}")

    users = resp.json()
    if not users:
        raise RuntimeError(f"No Jira user found for email: {email}")

    return users[0]["accountId"]


def build_jql(start_ct: datetime, end_ct: datetime, account_ids: list[str]) -> str:
    """
    Convert Central business window → UTC before sending to Jira.
    This guarantees timezone-safe totals regardless of Jira profile settings.
    """

    if not account_ids:
        raise RuntimeError("No Jira accountIds available.")

    start_utc = start_ct.astimezone(UTC)
    end_utc = end_ct.astimezone(UTC)

    start_str = start_utc.strftime("%Y-%m-%d %H:%M")
    end_str = end_utc.strftime("%Y-%m-%d %H:%M")

    parts = []

    if BASE_JQL:
        parts.append(f"({BASE_JQL})")

    reporters = ", ".join(account_ids)
    parts.append(f"reporter in ({reporters})")
    parts.append(f'created >= "{start_str}"')
    parts.append(f'created < "{end_str}"')

    return " AND ".join(parts) + " ORDER BY created DESC"


def get_issues(jql: str):
    url = f"{JIRA_BASE_URL}/rest/api/3/search"

    resp = requests.get(
        url,
        params={
            "jql": jql,
            "maxResults": 100,
            "fields": "summary,status,priority,reporter",
        },
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

    return resp.json().get("issues", [])


def format_slack_message(start_ct, end_ct, issues, jql):
    total = len(issues)

    header = (
        "*Weekly Jira Roundup (Team Leads)*\n"
        f"Window: {start_ct.strftime('%a %b %d, %Y %I:%M %p CT')} → "
        f"{end_ct.strftime('%a %b %d, %Y %I:%M %p CT')}\n"
        f"Reporters tracked: {len(TEAM_LEAD_EMAILS)}\n"
        f"Total: {total}\n"
    )

    jql_link = f"{JIRA_BASE_URL}/issues/?jql={quote_plus(jql)}"

    body = f"<{jql_link}|Open this JQL in Jira>\n"

    if total == 0:
        body += "• No issues found."
    else:
        for issue in issues:
            key = issue["key"]
            summary = issue["fields"]["summary"]
            status = issue["fields"]["status"]["name"]
            priority = (
                issue["fields"]["priority"]["name"]
                if issue["fields"]["priority"]
                else "Not Set"
            )
            reporter = issue["fields"]["reporter"]["displayName"]

            body += (
                f"• *{key}* — {summary} "
                f"({reporter}, {status}, {priority})\n"
            )

    return header + body


def post_to_slack(message: str):
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json={"text": message},
        headers={"Content-Type": "application/json"},
    )

    if resp.status_code != 200:
        raise RuntimeError(f"Slack webhook failed: {resp.text}")


# =========================
# MAIN
# =========================

def main():
    start_ct, end_ct = get_week_window()

    print(f"Window: {start_ct} → {end_ct}")

    account_ids = []
    for email in TEAM_LEAD_EMAILS:
        account_id = get_account_id(email)
        account_ids.append(account_id)

    print(f"Reporters tracked: {len(account_ids)}")

    jql = build_jql(start_ct, end_ct, account_ids)

    print(f"JQL: {jql}")

    issues = get_issues(jql)

    message = format_slack_message(start_ct, end_ct, issues, jql)

    post_to_slack(message)

    return 0


if __name__ == "__main__":
    sys.exit(main())
