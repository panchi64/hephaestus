"""Pinning the git settings a repository must not be allowed to choose for us.

A repository's own `.git/config` is **content**, not configuration: in a worktree it is
the *main checkout's* file, and anything the agent — or a build script it ran — writes
into it lands there. Several of its keys name a program git then executes as part of an
ordinary read: `core.fsmonitor` runs a hook on `git status`, and a pager is launched for
anything that prints. So a command that looks like an observation, and is cleared as one,
can execute whatever a repository put in that file.

Git's answer is `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`: settings
handed in through the environment, applied *after* every config file, so they win. That is
why the pins live here rather than in a `git -c` prefix — a prefix only covers the one
invocation someone remembered to write it on, and every command in this application's
shells is git's caller whether it says `git` or not (`make`, a test suite, a build script).

Two keys — the ones that turn a read into an execution *and* have a value that means "do
not":

- ``core.fsmonitor=false`` — no hook program on a status/diff of a working tree;
- ``core.pager=cat`` — a pager is a program too, and it would block on a pipe besides.

**What is missing from that list cannot be put there.** `diff.external`, a diff driver's
`textconv` and a filter driver's `clean`/`smudge` each run a program during an ordinary
`git diff`, `git log -p`, `git blame` — even `git status`, which runs the clean filter to
decide whether a file changed. No *value* switches them off: git reads these keys as
command lines, so the empty string is not an absence but a program named "", and pinning
``diff.external=`` makes **every** `git diff` die with `cannot run : No such file or
directory` (git 2.50), in a clean repository as readily as a poisoned one. The driver
keys cannot even be named from here — the driver's name is whatever the repository's own
`.gitattributes` says. Blanking the attributes wholesale (``GIT_ATTR_SOURCE`` at the
empty tree) does stop all three, and stops git-lfs and end-of-line normalisation with
them: it would trade a repository that can run a program for a repository the agent
silently commits raw blobs into.

**So those keys are the fence's to answer, and it does.** A project command runs in the
platform VM, which bounds what a spawned program may *do* — the only formulation that
covers a program the repository named, a program the build named, and a program nobody
named — instead of guessing what git might be asked to run. What the pins below still buy
is the case the fence cannot see: they keep a poisoned `core.fsmonitor` from being
*launched* at all, and they keep a pager from blocking on a pipe. Two keys, and nothing
about the drivers.
"""

from __future__ import annotations

from collections.abc import Mapping

#: The pins, in the order they are numbered. Kept as a tuple because the numbering is
#: positional and a dict would invite someone to reorder it for tidiness.
_PINS: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.pager", "cat"),
)


def git_config_pins(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` plus the git config pins, continuing whatever count it already carries.

    An existing ``GIT_CONFIG_COUNT`` is *continued* rather than overwritten: the operator's
    own environment may already be passing settings this way, and renumbering from zero
    would silently drop theirs. An unreadable count is treated as none — a malformed
    value means nothing downstream can trust the existing pairs anyway, and the pins
    matter more than preserving something git will itself reject.
    """
    env = dict(base)
    try:
        start = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        start = 0
    if start < 0:
        start = 0
    for offset, (key, value) in enumerate(_PINS):
        index = start + offset
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(_PINS))
    return env
