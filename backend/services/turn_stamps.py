"""What an edited message still claims of the turn it replaces.

Pure text rules, shared by no route but owned by none: whether a stamped slash command or
``@`` reference survives a rewrite of the prompt it was written on.
"""

from __future__ import annotations

import re

from services.commands import Invocation
from services.conversations import TurnStamps


def still_named(stamps: TurnStamps, prompt: str) -> TurnStamps:
    """The parts of a turn's stamps its edited text still claims.

    One rule, the composer's: **the text is the turn of record.** The `/` menu already
    applies it on the way out — a command staged and then typed away does not ride the send
    — and an edit is the same question asked later, about a message the operator has just
    rewritten. So a command survives while the prompt still opens with its name, and a
    reference survives while the prompt still contains its `@path`.

    Matching the *bare* name as well as the qualified one, because a client may have sent
    either and the operator sees only what they typed.

    **Both halves match on a whole token, never on a prefix.** A bare ``startswith`` would
    read ``/reviewer the auth`` as the command ``review`` carrying the argument
    ``er the auth`` — the name is kept *and* its argument is sliced at the wrong place, so
    a template runs on text nobody wrote. The same applies to a path: ``@src/a.ts`` is not
    named by a message that says ``@src/a.tsx``. So the character after the token has to
    end it, which for a command means whitespace or end of prompt, and for a path means
    anything that could not be part of the path itself.

    **The argument is re-read from the edited text, never carried over.** The name says
    *which* command; everything after it is the operator's own words, and those are the
    half an edit exists to change. Keeping the stamped argument would launch a sub-agent on
    the brief that was corrected, under a turn that visibly says otherwise.
    """
    typed = prompt.strip()
    command = stamps.command
    if command is not None:
        # Longest first, so a qualified stamp reads its own argument rather than the bare
        # form swallowing the prefix.
        token = next(
            (
                f"/{name}"
                for name in sorted(
                    {command.name, command.name.rpartition(":")[2]}, key=len, reverse=True
                )
                if _opens_with(typed, f"/{name}")
            ),
            None,
        )
        command = (
            Invocation(name=command.name, argument=typed[len(token) :].strip())
            if token is not None
            else None
        )
    return TurnStamps(
        command=command,
        file_refs=[path for path in stamps.file_refs if _names_path(prompt, path)],
    )


def _opens_with(text: str, token: str) -> bool:
    """``text`` begins with ``token`` and ends it there — not part of a longer name."""
    return text.startswith(token) and (
        len(text) == len(token) or text[len(token)].isspace()
    )


#: The run of characters after a token that could still be part of the same path.
#: Deliberately broader than the path characters a picker offers: the question is whether
#: the text names *this* file, and a token that continues into anything name-shaped names a
#: different one.
_PATH_RUN = re.compile(r"[^\s\"'`,;:)\]}>]*")


def _names_path(text: str, path: str) -> bool:
    """``text`` contains ``@path`` as a whole token rather than as the head of a longer one.

    **A trailing dot is punctuation, not an extension.** "please read @src/a.ts." is an
    ordinary sentence and the commonest way a path is written in one, so asking only
    "is the next character path-shaped" would drop the reference on a full stop while
    correctly dropping it on ``.bak``. The two are told apart by what follows the dots.

    Mirrored in the composer (``features/chat/files/useFileRefs.namesPath``) — the same
    question asked at two moments, and the two have to agree.
    """
    token = f"@{path}"
    start = text.find(token)
    while start != -1:
        run = _PATH_RUN.match(text, start + len(token)).group()
        if not run or run.strip(".") == "":
            return True
        start = text.find(token, start + 1)
    return False
