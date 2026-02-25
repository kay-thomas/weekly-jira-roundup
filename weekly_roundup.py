import os
import requests
import datetime
from urllib.parse import quote_plus


# =========================
# CONFIG
# =========================

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK"]

# Permanent Jira Filter View (ALL Team Lead Jiras)
ALL_ESCALATIONS_FILTER_URL = "https://zillowgroup.atlassian.net/issues?filter=77859"

# Reporter account IDs
REPORTERS = [
    "712020:e42cac78-cbc0-4090-985a-549ad893ef45",
    "712020:894cfdc5-4bcb-4380-acf2-e280c363d6dd",
    "712020:a520d240-bce6-4293-8101-f8d36195930b",
    "712020:453a4414-1382-4054-b762-a6191d545e65",
    "712020:f78b4a9f-5620-414c-ba21-b2aebf49ce33",
    "712020:663a58b0-f773-4b95-b2e2-77b78495cdcf",
]


# =========================
# DATE WINDOW (Last Mon–Fri)
# =========================

def get_last_week_window():
    today = datetime.date.today()
    this_monday = today - datetime.timedelta(days=today.weekday())
    last_monday = this_monday - datetime.timedelta(days=7)
    last_friday = last_monday + datetime.timedelta(days=4)

    start = f"{last_monday} 00:00"
    end = f"{last_friday} 23:59"

    return start, end


# =========================
# JQL BUILDER
# =========================

def build_jql():
    start, end = get_last_week_window()
    reporter_clause = ", ".join(f'"{r}"' for r in REPORTERS)

    jql = (
        f"reporter in ({reporter_clause}) "
        f'AND created >= "{start}" '
        f'AND created <= "{end}" '
        "ORDER BY created DESC"
    )
    return jql


# =========================
# JIRA API CALL
# =========================

def get_issues(jql):
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"

    headers = {
        "Accept": "application/json"
    }

    response = requests.get(
        url,
        headers=headers,
        params={"jql": jql, "maxResults": 100},
        auth=(JIRA_EMAIL, JIRA_API_TOKEN)
    )

    if response.status_code != 200:
        raise RuntimeError(f"Jira API error {response.status_code}: {response.text}")

    return response.json().get("issues", [])


# =========================
# SLACK POST
# =========================

def post_to_slack(message):
    response = requests.post(
        SLACK_WEBHOOK,
        json={"text": message}
    )

    if response.status_code != 200:
        raise RuntimeError(f"Slack webhook error {response.status_code}: {response.text}")


# =========================
# MAIN
# =========================

def main():
    jql = build_jql()
    issues = get_issues(jql)

    header = (
        "⚠️ Escalations Created Last Week\n\n"
        "📌 Take a minute to review known issues to be aware of this week. "
        "If your issue resembles one below, send the ticket to Tag Team and reference the Jira link. "
        "Please do not add examples to Jira on your own. This is for information purposes only.\n\n"
        f"Total: {len(issues)}\n\n"
    )

    if not issues:
        post_to_slack(header + "• No issues found.")
        return

    max_display = 6
    displayed_issues = issues[:max_display]

    body_lines = []

    for issue in displayed_issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        reporter = issue["fields"]["reporter"]["displayName"]
        status = issue["fields"]["status"]["name"]
        priority = issue["fields"]["priority"]["name"] if issue["fields"]["priority"] else "Not Set"

        jira_link = f"{JIRA_BASE_URL}/browse/{key}"

        body_lines.append(
            f"• <{jira_link}|{key}> — {summary} "
            f"*({reporter}, {status}, {priority})*"
        )

    body = "\n".join(body_lines)

    remaining = len(issues) - max_display

    footer = ""
    if remaining > 0:
        footer = (
            f"\n\n… and {remaining} more. "
            f"<{ALL_ESCALATIONS_FILTER_URL}|View all escalations in Jira>"
        )
    else:
        footer = (
            f"\n\n<{ALL_ESCALATIONS_FILTER_URL}|View all escalations in Jira>"
        )

    full_message = header + body + footer

    post_to_slack(full_message)


if __name__ == "__main__":
    main()
