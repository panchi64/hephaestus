"""What makes a written command well-formed — the rules, once, for both authors of one.

A workflow arrives from two places: the operator typing into the settings pane, and a
markdown file in a checkout under ``.claude/commands/``. They are the same thing with
different provenance, they land in the same picker, and a rule that held for one and not
the other would be a name the operator could write in a file but not in the form.

So the caps and the name rule live here and both callers run them. What differs between the
two is only **how a violation is reported**: the store raises, because someone is looking at
a form and waiting to be told; the file scan catches and skips, because nobody is watching a
checkout while the model is mid-turn (``services/subagents/definitions`` makes the same
split for the same reason).

**Why the caps are where they are.** A description is one line in a picker and joins no
prompt, so it is bounded for the operator's eyes rather than for a budget. The *body* is the
one field that reaches the model, once per turn it is invoked on, at the tail — so its cap
is the one that matters, and it is set beside the sub-agent brief's rather than invented:
both are operator-authored prose that rides one turn, and there is no reason for a command
template to be allowed to be larger than an agent's whole standing instruction.
"""

from __future__ import annotations

import re

from core.exceptions import CommandValidationError

#: What a name may be. The same shape a sub-agent's is, and for the same reason — these are
#: names the operator *types*, and two different rules for two things typed after the same
#: slash would be a distinction nobody could see the point of.
_NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$")

#: Long enough for a phrase, short enough to stay typeable. Matches the skills' own bound.
NAME_MAX_CHARS = 64

#: The picker's row label.
TITLE_MAX_CHARS = 120

#: The line under it. Matches the sub-agent roster's cap, because it is shown in the same
#: kind of list for the same purpose.
DESCRIPTION_MAX_CHARS = 400

#: "a brief for the reviewer" — a hint, not a prompt.
ARGUMENT_HINT_MAX_CHARS = 120

#: The template itself, and the only field here that a model ever reads. Set beside
#: ``services/subagents/definitions.BRIEF_MAX_CHARS``; the cap is against a file that is not
#: a template at all — a whole design document committed under ``commands/``.
BODY_MAX_CHARS = 8_000

_WHITESPACE_RUN = re.compile(r"\s+")


def validate_name(raw: str) -> str:
    """The typed handle, normalised.

    Lowercased and spaces folded to hyphens rather than refused: ``Stand Up`` is what
    someone writes when they are thinking about the ritual rather than about an identifier,
    and there is exactly one thing they can have meant. A leading slash is taken off for the
    same reason — the operator sees ``/standup`` everywhere else, so typing it into the name
    box is the obvious mistake to make and a silly one to punish.
    """
    value = raw.strip().lstrip("/").lower().replace(" ", "-")
    if not value:
        raise CommandValidationError("name", "a command needs a name to be typed by")
    if len(value) > NAME_MAX_CHARS:
        raise CommandValidationError("name", f"the name is over {NAME_MAX_CHARS} characters")
    if not _NAME.match(value):
        raise CommandValidationError(
            "name",
            (
                f"{value!r} is not a usable command name — lowercase letters, digits, "
                "hyphens and underscores only"
            ),
        )
    return value


def validate_body(raw: str) -> str:
    """The template. The one field with nothing sensible to fall back to: a command whose
    body is empty expands to nothing, which is a name that does nothing when typed."""
    value = raw.strip()
    if not value:
        raise CommandValidationError(
            "body", "a command needs a template to put in front of the model"
        )
    if len(value) > BODY_MAX_CHARS:
        raise CommandValidationError("body", f"the template is over {BODY_MAX_CHARS} characters")
    return value


def validate_line(raw: str, *, field: str, limit: int, required: bool = False) -> str:
    """One line of picker copy — the title, the description, the argument hint.

    Whitespace runs collapse rather than being refused, so a description written across
    three lines in a YAML block scalar renders as the single line the picker draws. Over the
    cap is an error rather than a silent clip *here*, because someone is looking at the form
    — the file scan clips instead, on its way past.
    """
    value = _WHITESPACE_RUN.sub(" ", raw or "").strip()
    if required and not value:
        raise CommandValidationError(field, f"a command needs a {field}")
    if len(value) > limit:
        raise CommandValidationError(field, f"the {field} is over {limit} characters")
    return value
