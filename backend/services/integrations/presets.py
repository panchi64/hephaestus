"""The static catalog of connector presets (`INTEG-1`).

A **preset** is everything the system knows about a third-party service before the
operator supplies anything: where it lives, how it authenticates, one request that proves
a credential works (`INTEG-3`), and the handful of actions worth exposing to the agent.
The operator's row (``models.external_tool.Integration``) then holds only their choices —
the label, the base URL if theirs differs, and the sealed credential.

Why a catalog rather than "point at any URL": an action's *shape* is what makes it
callable by a model with typed arguments and reviewable by an operator at approval time.
A generic "make an HTTP request" tool would collapse every connector into one
undifferentiated capability, which is precisely the thing `AE-3.6`'s per-tool trust is
meant to keep separable.

Presets here are **public-cloud services on purpose.** Every outbound request is
SSRF-guarded (`core/ssrf`), which refuses loopback/private/link-local targets, so a
LAN-only connector could not be reached even if it were listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AuthKind = Literal["bearer", "header", "basic", "none"]


@dataclass(frozen=True)
class IntegrationAction:
    """One callable operation on a connector — and one unit of `AE-3.6` trust.

    ``path`` may carry ``{placeholder}`` segments; the caller supplies them (and any
    query parameters) as a flat string map, so the model has one obvious way to pass
    arguments regardless of connector.
    """

    name: str
    method: Literal["GET", "POST"]
    path: str
    description: str
    # Whether the action sends a JSON body. Kept explicit so a GET can never be handed
    # one by a model that guessed.
    takes_body: bool = False


@dataclass(frozen=True)
class IntegrationPreset:
    id: str
    name: str
    # A short operator-facing grouping ("CODE HOST", "MESSAGING", …).
    category: str
    description: str
    base_url: str
    auth: AuthKind
    # Where a credential goes when ``auth == "header"`` (e.g. GitLab's PRIVATE-TOKEN).
    header_name: str | None = None
    credential_required: bool = True
    # A cheap, read-only request that answers "does this credential work?" (`INTEG-3`).
    test_path: str = "/"
    # Headers every request to this service needs regardless of credentials (API
    # versioning, content negotiation).
    extra_headers: tuple[tuple[str, str], ...] = ()
    actions: tuple[IntegrationAction, ...] = field(default_factory=tuple)


PRESETS: tuple[IntegrationPreset, ...] = (
    IntegrationPreset(
        id="github",
        name="GitHub",
        category="CODE HOST",
        description="Repository browsing and issue management.",
        base_url="https://api.github.com",
        auth="bearer",
        test_path="/user",
        extra_headers=(("Accept", "application/vnd.github+json"),),
        actions=(
            IntegrationAction(
                "get_repo", "GET", "/repos/{owner}/{repo}", "Read a repository's metadata."
            ),
            IntegrationAction(
                "list_issues",
                "GET",
                "/repos/{owner}/{repo}/issues",
                "List a repository's issues.",
            ),
            IntegrationAction(
                "create_issue",
                "POST",
                "/repos/{owner}/{repo}/issues",
                "Open a new issue on a repository.",
                takes_body=True,
            ),
        ),
    ),
    IntegrationPreset(
        id="gitlab",
        name="GitLab",
        category="CODE HOST",
        description="Project browsing and issue tracking.",
        base_url="https://gitlab.com/api/v4",
        auth="header",
        header_name="PRIVATE-TOKEN",
        test_path="/user",
        actions=(
            IntegrationAction("get_project", "GET", "/projects/{id}", "Read a project's metadata."),
            IntegrationAction(
                "list_issues", "GET", "/projects/{id}/issues", "List a project's issues."
            ),
        ),
    ),
    IntegrationPreset(
        id="jira",
        name="Jira",
        category="PROJECT MGMT",
        description="Issue tracking and sprint management.",
        # Every Jira Cloud tenant has its own host, so this is a template the operator
        # is expected to replace with theirs.
        base_url="https://your-org.atlassian.net",
        auth="basic",
        test_path="/rest/api/3/myself",
        actions=(
            IntegrationAction(
                "search_issues", "GET", "/rest/api/3/search/jql", "Search issues with JQL."
            ),
            IntegrationAction(
                "get_issue", "GET", "/rest/api/3/issue/{key}", "Read one issue by key."
            ),
        ),
    ),
    IntegrationPreset(
        id="slack",
        name="Slack",
        category="MESSAGING",
        description="Channel listing and message posting.",
        base_url="https://slack.com/api",
        auth="bearer",
        test_path="/auth.test",
        actions=(
            IntegrationAction("list_channels", "GET", "/conversations.list", "List conversations."),
            IntegrationAction(
                "post_message",
                "POST",
                "/chat.postMessage",
                "Post a message to a channel.",
                takes_body=True,
            ),
        ),
    ),
    IntegrationPreset(
        id="notion",
        name="Notion",
        category="KNOWLEDGE BASE",
        description="Page search and retrieval.",
        base_url="https://api.notion.com",
        auth="bearer",
        test_path="/v1/users/me",
        extra_headers=(("Notion-Version", "2022-06-28"),),
        actions=(
            IntegrationAction(
                "search", "POST", "/v1/search", "Search pages and databases.", takes_body=True
            ),
            IntegrationAction("get_page", "GET", "/v1/pages/{page_id}", "Read one page."),
        ),
    ),
    IntegrationPreset(
        id="ntfy",
        name="ntfy",
        category="PUSH NOTIFICATIONS",
        description="Push notification delivery for alerts and completions.",
        base_url="https://ntfy.sh",
        auth="bearer",
        credential_required=False,
        test_path="/v1/health",
        actions=(
            IntegrationAction(
                "publish",
                "POST",
                "/{topic}",
                "Publish a notification to a topic.",
                takes_body=True,
            ),
        ),
    ),
)

_BY_ID = {preset.id: preset for preset in PRESETS}


def preset(preset_id: str) -> IntegrationPreset | None:
    return _BY_ID.get(preset_id)


def action(preset_id: str, action_name: str) -> IntegrationAction | None:
    found = _BY_ID.get(preset_id)
    if found is None:
        return None
    return next((a for a in found.actions if a.name == action_name), None)
