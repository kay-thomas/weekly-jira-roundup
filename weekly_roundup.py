import os
import sys
from dataclasses import dataclass
from urllib.parse import quote_plus

import requests

JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_API_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"

# Team Leads (Adam + Marissa intentionally excluded)
TEAM_LEAD_DISPLAY_NAMES = [
    "Kay Thomas",
    "Maryuri Orellana",
    "Emmanuel Whyte",
    "Evan Sandora",
    "Kyler VanderValk",
    "Zane Roberts",
]

# How many days back to include
ROLLING_DAYS = 7

BASE_JQL = ""


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

    force_run = parse_bool(force_run_env) or (event_name == "workflow_dispatch")

    return Config(
        jira_email=jira_email,
        jira_api_token=jira_api_token,
        slack_webhook=slack_webhook,
        event_name=event_name,
        force_run=force_run,
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


def resolve_account_ids(config: Config, display_names: list[str]) -> dict[str, str]:
    resolved = {}

    for name in display_names:
        results = jira_get(config, "/user/search", params={"query": name, "maxResults": 50})

        if not isinstance(results, list) or not results:
            raise RuntimeError(f'Could not resolve Jira accountId for "{name}"')

        exact = [
            u for u in results
            if (u.get("displayName", "") or "").strip().lower() == name.strip().lower()
            and u.get("active", True)
        ]

        if exact:
            resolved[name] = exact[0]["accountId"]
            continue

        active = [u for u in results if u.get("active", True)]
        pick = active[0] if active else results[0]

        resolved[name] = pick["accountId"]

    return resolved


def build_jql(account_ids: list[str]) -> str:
    if not account_ids:
        raise RuntimeError("No Jira accountIds available.")

    reporters = ", ".join([f'"{rid}"' for rid in account_ids])

    parts = []
    if BASE_JQL.strip():
        parts.append(f"({BASE_JQL.strip()})")

    parts.append(f"reporter in ({reporters})")
    parts.append(f"created >= -{ROLLING_DAYS}d")

    return " AND ".join(parts) + " ORDER BY created DESC"


def get_issues(config: Config, jql: str) -> list[dict]:
    url = f"{JIRA_API_BASE}/search"

    issues = []
    start_at = 0

    while True:
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": 100,
            "fields": "summary,issuetype,project,priority,status,created,reporter",
        }

        resp = requests.get(
            url,
            params=params,
            auth=(config.jira_email, config.jira_api_token),
            headers={"Accept": "application/json"},
            timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

        data = resp.json()

        issues.extend(data.get("issues", []))

        total = data.get("total", 0)
        start_at += data.get("maxResults", 0)

        if start_at >= total:
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
    meta_str = f" ({', '.join(meta)})" if meta else ""

    return f"• <{issue_url}|{key}> — {summary}{meta_str}"


def jira_search_link(jql: str) -> str:
    return f"https://{JIRA_DOMAIN}/issues/?jql={quote_plus(jql)}"


def post_to_slack(webhook: str, text: str):
    resp = requests.post(webhook, json={"text": text}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack webhook error {resp.status_code}: {resp.text}")


def main() -> int:
    config = get_config()

    name_to_id = resolve_account_ids(config, TEAM_LEAD_DISPLAY_NAMES)
    account_ids = list(name_to_id.values())

    jql = build_jql(account_ids)

    print(f"Reporters tracked: {len(account_ids)}")
    print(f"JQL: {jql}")
    print(f"Verify: {jira_search_link(jql)}")

    issues = get_issues(config, jql)

    header = (
        "*Weekly Jira Roundup (Team Leads)*\n"
        f"Rolling window: last {ROLLING_DAYS} days\n"
        f"Reporters tracked: {len(account_ids)}\n"
        f"Total: {len(issues)}\n"
        f"<{jira_search_link(jql)}|Open this JQL in Jira>"
    )

    if not issues:
        post_to_slack(config.slack_webhook, header + "\n• No issues found.")
        return 0

    lines = [format_issue_line(i) for i in issues]
    msg = header + "\n" + "\n".join(lines)

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
