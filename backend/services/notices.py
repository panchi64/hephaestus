"""The envelope a chassis notice reaches the model in, and the transcript reads back.

A notice (the open-tasks reminder) rides into a thread as a user message, the only shape
Pydantic AI has for words from outside the model. Wrapped, so neither the model nor the
transcript reads it as the operator's. Recognised by opener *and* preamble, like the
sub-agent envelopes (``services/subagents/report.py``): anyone can type an opening tag.
"""

from __future__ import annotations

import re

from core.untrusted import neutralize_tags

NOTICE_TAG = "system-notice"
NOTICE_CLOSE = "</system-notice>"

_PREAMBLE = (
    "From the workspace, not from the operator: they have not seen this and are not "
    "waiting on a reply to it."
)

_OPENER = re.compile(r'<system-notice kind="(?P<kind>[a-z][a-z0-9-]*)">\n')


def notice_envelope(kind: str, text: str) -> str:
    """A ``kind`` notice, framed as one."""
    safe = neutralize_tags(text.strip(), (NOTICE_TAG, "untrusted"))
    return f'<system-notice kind="{kind}">\n{_PREAMBLE}\n\n{safe}\n{NOTICE_CLOSE}'


def notice_body(text: str) -> tuple[str, str] | None:
    """``(kind, body)`` of a notice envelope, or None for anything else."""
    stripped = text.strip()
    opened = _OPENER.match(stripped)
    if opened is None:
        return None
    rest = stripped[opened.end() :]
    if not rest.startswith(f"{_PREAMBLE}\n\n") or not rest.endswith(NOTICE_CLOSE):
        return None
    body = rest[len(_PREAMBLE) + 2 : -len(NOTICE_CLOSE)].strip()
    return opened.group("kind"), body
