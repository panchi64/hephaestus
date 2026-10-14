"""How one of *this installation's own* tools presents itself to a review.

Between "read off a grammar" (``shell_ast.py``) and "named by its argument keys"
(``capability.py``'s fallback) sits a third reading, and this table is it. A mail send, a
calendar event, a click, a vault read and a delegated task are all tools this installation
**ships**, so their argument names are a fixed vocabulary someone here can read (and a test
pins them against the real schemas, since one of those vocabularies is a dependency's) —
and describing one by its keys alone ("Calls mail_send with arguments body, subject, to")
leaves the reviewer's third axis, ``correctness``, with nothing to rule on: it can see that
an email would be sent and not who it goes to.

**Three groups, because there are three different things an argument can be worth.** A
projection says which keys *identify* the act, which carry the act's model-authored
**content**, and which may only be **measured** — a typed string is the operator's own
password as often as it is a search term, and a mail body is the one part of a send that
has no business travelling to a second model to be scored.

**Every cut says how much it withheld, and no key's budget is another key's.** Both rules
exist for the same attack. A description is assembled from values the model wrote, so a
model that pads the first key can push the deciding one off the end — and a reviewer
reading a silent ``…`` cannot tell a short argument from a long one it was shown the front
of. So each key is cut to its own limit and each cut states the full length, which turns
"the program looked harmless" into "the program is 3 510 characters and you read 2 000".

This is a table rather than a function per tool so that adding a tool is a row, and so that
the question a security reader actually has — *does anything quote a mail body* — is
answered by reading one file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

#: How much of one *identity* value a summary quotes. A subject line, an address list or a
#: selector fits; a pasted document does not, and the summary is a line the operator reads
#: on a review row rather than the place to put one.
VALUE_CHARS = 160

#: How much of **each** content key :attr:`~services.permissions.capability.Capability.detail`
#: carries. Per key rather than shared across them, and that is the whole point: with one
#: pooled budget, a model that padded ``explanation`` would evict the ``new_text`` a future
#: conversation is going to follow, and the reviewer would be told to rule on words it was
#: never shown. Generous — a delegated task or a program is the thing being ruled on — and
#: still bounded, because this string goes to a second model inside every review of the call.
DETAIL_CHARS = 2_000


@dataclass(frozen=True)
class Projection:
    """How one first-party tool's own arguments are read for a review."""

    #: How the summary opens — what this tool does, in the operator's own language.
    verb: str
    #: The keys whose values say *which* act this is: who it is aimed at, which record it
    #: names, which switch it flips. Quoted (shortened) into the summary, because a
    #: reviewer asked whether the act matches the request has to see its target.
    identity: tuple[str, ...] = ()
    #: The keys carrying the act's model-authored content — a task, a question, a program.
    #: Too long for a line and too load-bearing to drop, so they become
    #: :attr:`~services.permissions.capability.Capability.detail`.
    content: tuple[str, ...] = ()
    #: The keys reported by **length alone**. What gets typed into a page is the operator's
    #: own password as often as it is a search term, and a review does not need to read it
    #: to know a form was filled in.
    measured: tuple[str, ...] = ()

    def describe(self, args: Mapping[str, object]) -> tuple[str, str | None]:
        """This call as (summary, detail).

        A key the call did not pass contributes nothing: most of these tools have optional
        arguments, and rendering ``cc: null`` would spend the operator's line on the
        absence of a thing.

        **What a group withholds is a key's *value*, never the fact that it was passed.**
        A key in none of the three groups is still *named* at the end of the line, the way
        an unprojected tool is described (``capability._arg_shape``) — because the values
        are what can be a mail body or a typed password, and a name cannot. Dropping the
        names too was a real hole rather than a tidier line: ``occurrence_start`` is the
        whole difference between cancelling one instance of a standup and deleting the
        series, and a summary that reads identically either way tells the reviewer the
        wrong act.
        """
        parts = [
            f"{key}: {_quoted(args[key], VALUE_CHARS)}"
            for key in self.identity
            if args.get(key) is not None
        ]
        parts += [
            f"{key}: {len(str(args[key]))} characters"
            for key in self.measured
            if args.get(key) is not None
        ]
        named = set(self.identity) | set(self.measured) | set(self.content)
        rest = sorted(key for key in args if key not in named and args[key] is not None)
        if rest:
            parts.append("also passing " + ", ".join(rest))
        summary = f"{self.verb} — {'; '.join(parts)}" if parts else self.verb
        content = "\n".join(
            _content(key, args[key], DETAIL_CHARS)
            for key in self.content
            if args.get(key) is not None
        )
        return summary, content or None


def _quoted(value: object, limit: int) -> str:
    """One identity value on a single line, however it was written.

    JSON-encoded rather than interpolated: a value with a newline in it would otherwise
    break the summary into lines of its own, and the summary is rendered as a sentence on
    the operator's review row and read as one sentence by the reviewer.

    A **list** is shortened by dropping entries rather than by cutting the string, and it
    says how many there were. The difference is the review's whole question on a mail send:
    a thirteenth recipient nobody named is exactly what ``correctness`` exists to catch, and
    a `to` list cut mid-JSON hides both the recipient and the fact that there was one.
    """
    if isinstance(value, list):
        return _quoted_list(cast("list[object]", value), limit)
    return _shortened(json.dumps(value, default=str), limit)


def _quoted_list(values: list[object], limit: int) -> str:
    """A list as its first entries, prefixed by how many there are in all when it is cut.

    At least one entry is always rendered, however long it is — a summary that dropped the
    only recipient would be worse than one that quotes a long one.
    """
    shown: list[str] = []
    used = 0
    for value in values:
        item = _shortened(json.dumps(value, default=str), limit)
        used += len(item) + 2
        if shown and used > limit:
            break
        shown.append(item)
    rendered = "[" + ", ".join(shown) + "]"
    if len(shown) == len(values):
        return rendered
    return f"{len(values)} entries, {len(shown)} shown: {rendered}"


def _shortened(text: str, limit: int) -> str:
    """``text`` cut to ``limit``, *saying* what it withheld.

    The count is the load-bearing half. A bare ellipsis tells a reviewer that something was
    cut and nothing about the size of what it is ruling on, which is the difference between
    "a subject line" and "a subject line with a program hidden behind it".
    """
    return text if len(text) <= limit else f"{text[:limit]}… ({len(text)} characters in all)"


def _content(key: str, value: object, limit: int) -> str:
    """One content key for the detail block, labelled and — where it is cut — measured."""
    text = str(value)
    if len(text) <= limit:
        return f"{key}: {text}"
    return f"{key} ({limit} of {len(text)} characters): {text[:limit]}"


#: The tools in this installation's **shipped** catalog, whose arguments are therefore a
#: fixed vocabulary someone here can read. Most are written in this repository; the
#: ``browse_*`` rows name a pinned dependency's parameters (``pydantic_ai_harness``), which
#: is the same kind of claim only as long as something checks it — so
#: ``tests/test_auto_review.py`` pins every key below against the real tool schemas, the
#: way ``tools/browse.py`` pins its own tool names. Anything absent falls back to its
#: argument *keys* (``capability._arg_shape``) — the honest description for an operator's
#: own MCP server, whose argument names mean whatever the far side decided they mean.
PROJECTIONS: dict[str, Projection] = {
    # Mail. The body is in none of the three groups, on purpose: it is the one part of a
    # send that is pure content, it is frequently the operator's own words, and knowing
    # who a message goes to and what it announces itself as is what a review of "should
    # this be sent" actually turns on. `explanation` is the opposite case — the tool
    # *mandates* it as a plain statement of who the mail reaches and what it says, written
    # for a reader, which is precisely the reviewer's `correctness` input.
    "mail_send": Projection(
        "Sends an email",
        identity=("account_id", "to", "cc", "subject"),
        content=("explanation",),
    ),
    "mail_reply": Projection(
        "Replies to an email",
        identity=("message_id", "reply_all"),
        content=("explanation",),
    ),
    "mail_mark": Projection("Changes an email's flags", identity=("message_id", "seen", "flagged")),
    # Calendar. A list per tool rather than one shared union, because the three take
    # genuinely different arguments and a row naming keys its tool does not have is a row
    # nothing can check against the real schema. What each carries beyond the event's
    # identity is the argument that *changes the act* rather than describing it: `rrule`
    # is the difference between one meeting and every Monday forever, and
    # `occurrence_start` between cancelling one standup and deleting the series.
    # `description` is deliberately in no group — it is the event's prose, the calendar's
    # equivalent of a mail body — and is named at the end of the line like any other key.
    "calendar_create_event": Projection(
        "Adds an event to a calendar",
        identity=("calendar_id", "title", "start", "end", "all_day", "rrule"),
    ),
    "calendar_update_event": Projection(
        "Changes an existing calendar event",
        identity=("event_id", "title", "start", "end", "rrule"),
    ),
    "calendar_delete_event": Projection(
        "Deletes a calendar event", identity=("event_id", "occurrence_start")
    ),
    # Browsing. Every one of these runs inside a browser carrying the operator's own
    # logins, so *where* on the page it lands is the whole question — and what is typed
    # into it is the one thing a review must not carry off to another model. Navigation is
    # here for the row the operator reads rather than for a review: it is a classified
    # read and cleared as one, and "opens example.com" is what they need to see rather
    # than "reads with browse_navigate, using arguments url".
    "browse_navigate": Projection("Opens a page in the browser", identity=("url",)),
    "browse_click": Projection("Clicks an element on the page", identity=("selector",)),
    "browse_type_text": Projection(
        "Types into a field on the page", identity=("selector",), measured=("text",)
    ),
    "browse_press_key": Projection("Presses a key on the page", identity=("key", "selector")),
    "browse_select_option": Projection(
        "Chooses an option on the page", identity=("selector", "values")
    ),
    # Measured rather than quoted, for the same reason `browse_type_text` is: filling a
    # field is one line of script (`document.querySelector('#password').value = …`), so a
    # projection that copied the program out would carry off the very credential the row
    # above it protects. What the reviewer rules on here is the act — arbitrary script in
    # a browser holding the operator's own logins — which the verb already states in full.
    "browse_execute_js": Projection(
        "Runs JavaScript with the page's own authority", measured=("script",)
    ),
    "browse_handle_next_dialog": Projection(
        "Answers the page's next dialog", identity=("accept",), measured=("prompt_text",)
    ),
    # The vault. The stated reason is the act's content rather than its identity: it is a
    # sentence the model wrote about why it needs a credential, and it is what the operator
    # is really being asked to weigh.
    "vault_get_entry": Projection(
        "Reads one stored credential in full, password included",
        identity=("entry_id",),
        content=("reason",),
    ),
    "vault_list_entries": Projection(
        "Lists the vault's entries — names and usernames, not passwords",
        content=("reason",),
    ),
    "agents_spawn": Projection(
        "Hands a task to another agent with its own tools, which does it on its own",
        identity=("agent_name",),
        content=("task",),
    ),
    # A skill's text is followed in *future* conversations, so a review of an edit is a
    # review of the replacement words themselves — the name alone says a file changed and
    # nothing about what it will now instruct. Both halves ride as content, with the
    # explanation the tool mandates alongside them. (`skills_create` has no row because it
    # never reaches a review: it is ungated and inside every acting level's ceiling, since
    # all it can produce is a draft the operator has to publish themselves.)
    "skills_edit": Projection(
        "Edits a stored skill's instructions",
        identity=("name",),
        content=("explanation", "old_text", "new_text"),
    ),
    "code_execute": Projection(
        "Runs a program in the conversation's sandbox",
        identity=("language",),
        content=("code",),
    ),
}


def describe(tool: str, args: Mapping[str, object], *, fallback: str) -> tuple[str, str | None]:
    """This call's summary and detail — projected where the tool is one of ours.

    ``fallback`` is what an unprojected tool says about itself, and it is the caller's
    because only the caller knows what *kind* of unprojected tool this is (a read, an
    external connector, an opaque effect).
    """
    projection = PROJECTIONS.get(tool)
    return projection.describe(args) if projection is not None else (fallback, None)
