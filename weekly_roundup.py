import os
import sys
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote_plus

import requests

# ====== Jira / Slack Config ======
JIRA_DOMAIN = "zillowgroup.atlassian.net"
JIRA_API_BASE = f"https://{JIRA_DOMAIN}/rest/api/3"

# This is the ALL-TIME filter you provided (points to your saved Jira filter view)
ALL_TIME_FILTER_URL = "https://zillowgroup.atlassian.net/issues?filter=77859"

# Team Leads (Adam + Marissa intentionally excluded)
TEAM_LEAD_DISPLAY_NAMES = [
    "Kay Thomas",
    "Maryuri Orellana",
    "Emmanuel Whyte",
    "Evan Sandora",
    "Kyler VanderValk",
    "Zane Roberts",
]

BASE_JQL = ""  # Optional additional filters (leave blank unless you want extra constraints)

# Message behavior
MAX_ITEMS_IN_SLACK = 6  # show only 6 issues, rest forced to Jira
TIMEZONE_LABEL = "CT"   # label only (we use timezone-agnostic JQL functions for accuracy)


# ====== Data model ======
@dataclass
class Config:
    jira_email: str
    jira_api_token: str
    slack_webhook: str
    event_name: str
    force_run: bool


# ====== Helpers ======
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

    # If workflow_dispatch or FORCE_RUN=true, run regardless of schedule
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
    """
    Resolve Jira Cloud accountIds for each display name.
    We prefer exact displayName match + active user.
    """
    resolved: dict[str, str] = {}

    for name in display_names:
        results = jira_get(config, "/user/search", params={"query": name, "maxResults": 50})

        if not isinstance(results, list) or not results:
            raise RuntimeError(f'Could not resolve Jira accountId for "{name}"')

        # Prefer exact match + active
        exact = [
            u for u in results
            if (u.get("displayName", "") or "").strip().lower() == name.strip().lower()
            and u.get("active", True)
        ]
        if exact:
            resolved[name] = exact[0]["accountId"]
            continue

        # Fallback: first active result, else first result
        active = [u for u in results if u.get("active", True)]
        pick = active[0] if active else results[0]
        resolved[name] = pick["accountId"]

    return resolved


def build_jql(account_ids: list[str]) -> str:
    """
    Timezone-agnostic "last week" window using Jira JQL functions:
      - startOfWeek(-1) = last Monday 00:00 (in Jira/server/user context)
      - startOfWeek()   = this Monday 00:00
    This avoids timezone drift / seconds formatting issues.
    """
    if not account_ids:
        raise RuntimeError("No Jira accountIds available.")

    parts = []
    if BASE_JQL.strip():
        parts.append(f"({BASE_JQL.strip()})")

    reporters = ", ".join([f'"{aid}"' for aid in account_ids])
    parts.append(f"reporter in ({reporters})")

    # Cover last week Monday -> this week Monday
    parts.append("created >= startOfWeek(-1)")
    parts.append("created < startOfWeek()")

    where_clause = " AND ".join(parts)
    return where_clause + " ORDER BY created DESC"


def get_issues(config: Config, jql: str) -> list[dict]:
    """
    Jira Cloud search endpoint (new): POST /rest/api/3/search/jql
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
        issues.extend(data.get("issues", []))

        next_token = data.get("nextPageToken")
        is_last = data.get("isLast", True)

        if is_last or not next_token:
            break

    return issues


def format_issue_line(issue: dict) -> str:
    """
    Slack formatting:
    • KEY — Summary (**Reporter, Status, Priority**)
    """
    key = issue.get("key", "UNKNOWN")
    fields = issue.get("fields", {}) or {}
    summary = (fields.get("summary") or "").strip()

    status = (fields.get("status") or {}).get("name", "")
    priority = (fields.get("priority") or {}).get("name", "")
    reporter = (fields.get("reporter") or {}).get("displayName", "")

    issue_url = f"https://{JIRA_DOMAIN}/browse/{key}"

    meta_parts = [p for p in [reporter, status, priority] if p]
    meta = f" (**{', '.join(meta_parts)}**)" if meta_parts else ""

    return f"• <{issue_url}|{key}> — {summary}{meta}"


def post_to_slack(webhook: str, text: str):
    resp = requests.post(webhook, json={"text": text}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Slack webhook error {resp.status_code}: {resp.text}")


# ====== Main ======
def main() -> int:
    config = get_config()

    # Scheduled behavior:
    # - In production, you'll schedule this via GitHub Actions cron (Mon 8:30am CT).
    # - If it runs outside schedule and isn't forced, we skip without failing.
    #
    # NOTE: GitHub cron uses UTC. Do the conversion in the workflow file.
    if not config.force_run:
        # If you still want a "soft guard", keep it loose:
        # Only allow runs on Monday (any hour) unless forced.
        now = datetime.utcnow()
        # Monday in UTC may not match CT perfectly; so keep this guard minimal.
        # If you prefer, delete this guard entirely and rely purely on cron schedule.
        # We'll just not block anything here to avoid surprises.
        pass

    name_to_id = resolve_account_ids(config, TEAM_LEAD_DISPLAY_NAMES)
    account_ids = list(name_to_id.values())

    jql = build_jql(account_ids)
    issues = get_issues(config, jql)

    header = (
        ":warning: *Escalations Created Last Week*\n\n"
        ":pushpin: Take a minute to review known issues to be aware of this week. "
        "If your issue resembles one below, send the ticket to Tag Team and reference the Jira link. "
        "Please do not add examples to Jira on your own. This is for information purposes only.\n\n"
        f"Total: {len(issues)}\n\n"
    )

    if not issues:
        footer = f"\n… and 0 more. <{ALL_TIME_FILTER_URL}|View all escalations in Jira>"
        post_to_slack(config.slack_webhook, header + "• No issues found.\n" + footer)
        return 0

    # Show first N items
    shown = issues[:MAX_ITEMS_IN_SLACK]
    lines = [format_issue_line(i) for i in shown]
    msg = header + "\n".join(lines)

    remaining = max(0, len(issues) - len(shown))
    footer = f"\n\n… and {remaining} more. <{ALL_TIME_FILTER_URL}|View all escalations in Jira>"
    msg += footer

    # Slack size guard
    if len(msg) > 35000:
        msg = msg[:34000] + "\n\n… (truncated)\n" + f"<{ALL_TIME_FILTER_URL}|View all escalations in Jira>"

    post_to_slack(config.slack_webhook, msg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
