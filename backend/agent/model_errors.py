"""Reading a model provider's failure — which stop it is, and what to tell the operator.

Three of the engine's outcomes are decided here: a prompt that overran the context window
(a definitive ceiling, reported and never papered over), an inference server that couldn't
load the model (operator-actionable, engine-side), and which of *our own* per-turn budgets
tripped. All three reach the operator verbatim, so the sentences are as much a part of
this module as the classification.

**Why this isn't purely structural.** A cloud provider returns a machine-readable error
code and that is what we read. A local inference server — llama.cpp, LM Studio, vLLM,
which is what this host mostly talks to — returns prose, and there is no code to read.
So the classification is: structured signal first, prose scan second, and the prose scan
is bounded by what the HTTP status already rules out. That is honest about a heuristic we
cannot remove while local engines answer in English, and it stops the heuristic being
consulted where the provider has already told us the answer.
"""

from __future__ import annotations

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from runs import Run

_BODY = TypeAdapter(dict[str, JsonValue])

# --- context-window overflow ------------------------------------------------------

# A provider's own code for "the prompt is bigger than the context window". A code is a
# contract the provider published; the message is prose it may reword at any time — so a
# recognized code decides on its own, without consulting the text.
_OVERFLOW_CODES = frozenset({"context_length_exceeded", "string_above_max_length"})

# Statuses an overflow can legitimately arrive as: the request was refused as malformed or
# too large. Notably *excluded* are 401/403 (auth), 404 (bad model), 429 (rate limit) and
# every 5xx — a server-side fault or a quota error is not a context ceiling no matter what
# its message happens to mention, and gating on this is what stops a rate-limit body that
# quotes token counts from stopping the run with a misleading context-window message.
_OVERFLOW_STATUSES = frozenset({400, 413, 422})

# How the common engines phrase it when there is no code to read. Matched case-insensitively
# as substrings, so each marker must be specific enough that an *unrelated* error can't
# carry it: deliberately omitted are generic phrasings ("context window", "too many tokens",
# "reduce the length") that also appear in rate-limit and validation errors.
_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "prompt is too long",
    "exceeds context",
    "exceed context",
    "context size",
    "n_ctx",
)


def provider_error_code(exc: ModelHTTPError) -> str | None:
    """The provider's own machine-readable error code, if the response carried one.

    Both the OpenAI-compatible shape (``{"error": {"code": ...}}``) and the Anthropic one
    (``{"error": {"type": ...}}``) put it in the same place. A body that is a bare string
    (what most local servers send) has no code, and this returns ``None`` — which is the
    signal to fall back to reading the prose.
    """
    try:
        body = _BODY.validate_python(exc.body)
    except ValidationError:
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code") or error.get("type")
    return code.lower() if isinstance(code, str) else None


def is_transient(exc: Exception) -> bool:
    """Whether sending the same request again could succeed: the server was overloaded or
    rate-limited, the connection dropped, or the stream was cut short. A refusal of the
    request itself (bad model, bad auth, a malformed prompt) would only fail again, and so
    would a tool call cut off at the output-token limit."""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, ModelAPIError | httpx.TransportError)


def is_context_overflow(exc: ModelHTTPError) -> bool:
    """Whether ``exc`` is the model refusing a prompt that overran its context window.

    A recognized provider code answers on its own. Otherwise the message is scanned, but
    only for statuses an overflow can actually arrive as — so a 429 or a 500 whose body
    happens to quote the prompt is never mistaken for one.
    """
    if (code := provider_error_code(exc)) is not None:
        if code in _OVERFLOW_CODES:
            return True
        # The provider named its error something else. Trust that over a text scan: a
        # published code is exactly the structured signal the scan is a substitute for.
        return False
    if exc.status_code not in _OVERFLOW_STATUSES:
        return False
    return any(marker in str(exc).lower() for marker in _CONTEXT_OVERFLOW_MARKERS)


#: The persisted stop marker for a turn that could not be brought inside the window. Exact
#: text, exported rather than repeated: the client keys its **Compact and retry** offer on
#: this string — both live (off the ``limit.notice``) and on reload (off the turn's stored
#: ``blocked_reason``) — so the two spellings can never drift apart.
CONTEXT_OVERFLOW_DETAIL = "context window exceeded"

#: The same ceiling, reached *after* this turn already folded the thread and retried. A
#: separate marker rather than a flag on the first, because it is the client's cue to
#: withhold the **Compact and retry** offer: compacting again is precisely what just
#: failed, and a button that re-sends the same oversized request is a button that lies.
CONTEXT_OVERFLOW_AFTER_FOLD_DETAIL = "context window exceeded after compaction"


def context_limit_message(run: Run, *, compacted: bool = False) -> str:
    """The operator-facing stop message — names the model's context window (the number the
    operator needs) when known, says what the run already tried, and what is left to try.

    ``compacted`` is whether this turn *did* fold the thread and still overran. The two
    cases need different sentences: after a fold, compacting again is not the answer and
    offering it would send the operator round the same loop; before one, it is the cheapest
    thing they can do and it is one menu item away. "Start a new chat" is deliberately not
    the lead in either case — it is the one option that abandons the thread, and it was
    reading as the recommendation."""
    window = run.context_window
    ceiling = f" of {window:,} tokens" if window else ""
    if compacted:
        return (
            f"This turn still exceeded the model's context window{ceiling} after its earlier "
            "messages were folded into a summary. Edit or rewind to remove content — or "
            "start a new chat — to keep going."
        )
    return (
        f"This conversation reached the model's context window{ceiling} and can't continue. "
        "Compact now (in the conversation menu) folds the earlier turns into a summary; "
        "editing or rewinding removes them outright."
    )


# --- the engine couldn't load the model -------------------------------------------

# On-demand inference servers (LM Studio, llama.cpp, …) reject a request for a model they
# couldn't bring up with a terse, mechanical message — and no error code, since these are
# the servers that don't publish one. The most common cause here is a side-by-side compare
# firing two *unloaded* models at once: the server can only cold-load one at a time, so the
# second aborts.
_MODEL_LOAD_MARKERS = ("failed to load model", "engine protocol startup was aborted")


def model_load_hint(exc: ModelHTTPError) -> str | None:
    """An operator-actionable message if ``exc`` is an engine model-load failure, else
    ``None`` (leave other HTTP errors with their own detail). The fix is engine-side, so
    the hint points there rather than implying an app bug."""
    if not any(marker in str(exc).lower() for marker in _MODEL_LOAD_MARKERS):
        return None
    model = exc.model_name or "the selected model"
    return (
        f"Couldn't load {model!r} on its inference server. Load it before use — in "
        "LM Studio, pre-load each model you want to compare, or raise “Max loaded "
        "models” / enable JIT so the server can hold more than one at once."
    )
