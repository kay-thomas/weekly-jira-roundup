import os
import sys
from dataclasses import dataclass
from urllib.parse import quote_plus

import requests

JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_API_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"

TEAM_LEAD_DISPLAY_NAMES = [
    "Kay Thomas",
    "Maryuri Orellana",
    "Emmanuel Whyte",
    "Evan Sandora",
    "Kyler VanderValk",
    "Zane Roberts",
]

BASE_JQL = ""


@dataclass
class Config:
    jira_email: str
    jira_api_token: str
    slack_webhook: str


def require_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def get_config() -> Config:
    return Config(
        jira_email=require_env("JIRA_EMAIL"),
        jira_api_token=require_env("JIRA_API_TOKEN"),
        slack_webhook=require_env("SLACK_WEBHOOK"),
    )


def jira_get(config: Config, path: str, params: dict | None = None):
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


def resolve_account_ids(config: Config, display_names: list[str]) -> list[str]:
    account_ids = []

    for name in display_names:
        results = jira_get(
            config,
            "/user/search",
            params={"query": name, "maxResults": 50},
        )

        if not results:
            raise RuntimeError(f'Could not resolve Jira accountId for "{name}"')

        exact = [
            u for u in results
            if u.get("displayName", "").strip().lower() == name.lower()
            and u.get("active", True)
        ]

        pick = exact[0] if exact else results[0]
        account_ids.append(pick["accountId"])

    return account_ids


def build_jql(account_ids: list[str]) -> str:
    reporters = ", ".join([f'"{rid}"' for rid in account_ids])

    parts = []
    if BASE_JQL.strip():
        parts.append(f"({BASE_JQL.strip()})")

    parts.append(f"reporter in ({reporters})")
    parts.append("created >= startOfWeek(-1w)")
    parts.append("created < startOfWeek()")

    return " AND ".join(parts) + " ORDER BY created DESC"


def get_issues(config: Config, jql: str) -> list[dict]:
    url = f"{JIRA_API_BASE}/search/jql"

    issues = []
    next_token = None

    while True:
        body = {
            "jql": jql,
            "maxResults": 100,
            "fields": [
                "summary",
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
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

        data = resp.json()
        issues.extend(data.get("issues", []))

        next_token = data.get("nextPageToken")
        if not next_token:
            break

    return issues


def format_issue_line(issue: dict) -> str:
    key = issue.get("key", "UNKNOWN")
    fields = issue.get("fields", {})

    summary = (fields.get("summary") or "").strip()
    status = (fields.get("status") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    reporter = (fields.get("reporter") or {}).get("displayName", "")

    issue_url = f"https://{JIRA_DOMAIN}/browse/{key}"

    meta = [x for x in [reporter, status, priority] if x]

    # Entire parenthesis block bolded
    meta_str = f" *({', '.join(meta)})*" if meta else ""

    return f"• <{issue_url}|{key}> — {summary}{meta_str}"


def jira_search_link(jql: str) -> str:
    return f"https://{JIRA_DOMAIN}/issues/?jql={quote_plus(jql)}"


def post_to_slack(webhook: str, text: str):
    resp = requests.post(webhook, json={"text": text}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack webhook error {resp.status_code}: {resp.text}")


def main() -> int:
    config = get_config()

    account_ids = resolve_account_ids(config, TEAM_LEAD_DISPLAY_NAMES)
    jql = build_jql(account_ids)

    print(f"Reporters tracked: {len(account_ids)}")
    print(f"JQL: {jql}")
    print(f"Verify: {jira_search_link(jql)}")

    issues = get_issues(config, jql)

    header = (
        "⚠️ *Escalations Created Last Week*\n\n"
        "📌 Take a minute to review known issues to be aware of this week.\n"
        "If your issue resembles one below, reference it in your Tag Team escalation.\n\n"
        f"Total: {len(issues)}\n"
        f"<{jira_search_link(jql)}|Open this JQL in Jira>\n\n"
    )

    if not issues:
        post_to_slack(config.slack_webhook, header + "• No issues found.")
        return 0

    lines = [format_issue_line(i) for i in issues]
    msg = header + "\n".join(lines)

    if len(msg) > 35000:
        msg = header + "\n".join(lines[:150]) + "\n• (truncated)"

    post_to_slack(config.slack_webhook, msg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
