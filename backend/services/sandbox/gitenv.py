"""Pinning the git settings a repository must not be allowed to choose for us.

A repository's own `.git/config` is content, not configuration: in a worktree it is the
main checkout's file, and anything the agent or a build script writes into it lands there.
Some keys name a program git executes during an ordinary read (`core.fsmonitor` on
`git status`, a pager for anything that prints).

Settings passed as `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` apply
after every config file, so they win. They are pinned here rather than in a `git -c`
prefix because every command in this application's shells is git's caller (`make`, a test
suite, a build script), not only the ones that say `git`.

Two keys qualify, being both read-to-execution and having a value that means "do not":

- ``core.fsmonitor=false`` — no hook program on a status/diff of a working tree;
- ``core.pager=cat`` — a pager is a program too, and it would block on a pipe besides.

`diff.external`, a diff driver's `textconv` and a filter's `clean`/`smudge` also run
programs, but no value switches them off: git reads them as command lines, so an empty
value is a program named "" and pinning ``diff.external=`` makes every `git diff` fail
(git 2.50). The driver names come from the repository's `.gitattributes`, and blanking the
attributes (``GIT_ATTR_SOURCE`` at the empty tree) would also disable git-lfs and
end-of-line normalisation. Those keys are left to the platform VM, which bounds what any
spawned program may do. The pins cover what the VM cannot: launching a poisoned
`core.fsmonitor`, and a pager blocking on a pipe.
"""

from __future__ import annotations

from collections.abc import Mapping

_PINS: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.pager", "cat"),
)


#: Prefix for every `git` this backend runs on the host itself, outside any fence: no
#: repository-named hook directory, fsmonitor or pager may run as part of it. Passed as
#: `-c` arguments, which beat the repository's own config.
GIT_ARG_PINS: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.pager=cat",
)


def git_config_pins(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` plus the git config pins, continuing whatever count it already carries.

    An existing ``GIT_CONFIG_COUNT`` is continued so the operator's own pairs survive; an
    unreadable one is treated as none, since git would reject the existing pairs anyway.
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
