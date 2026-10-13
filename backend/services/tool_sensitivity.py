"""What a tool *does* if it runs, in five classes.

Three markings live on the tools today and none of them answers this question.
``requires_approval=True`` says "pause before running this"; an ``ApprovalRequired`` raised
from inside a call says "pause, now that I have seen the arguments"; a tool carrying
neither says nothing at all. All three are about **when to ask the operator** — they
schedule a prompt. None of them describes the act itself, which is what a permission level
has to know *before* the model is offered the tool. "May this run in a read-only thread?"
has no answer in a marking whose only purpose is to raise a dialog.

So every tool carries a **sensitivity**: the worst thing it can do, named. The classes are
kinds of reach, not degrees of danger:

- ``read`` — observes and returns. Reaching the network to observe (a web search, a page
  fetch, an IMAP body) is still reading: nothing anywhere is different afterwards.
- ``workspace_write`` — changes state this installation owns. The run's own files, the
  conversation's plan, the operator's memory and skills, a local draft. Visible from
  inside Hephaestus, inspectable, and undoable from here.
- ``host_exec`` — hands a command or a program to an interpreter, so the worst case is not
  bounded by the tool's own arguments. The sandbox is a fence around the blast
  radius, not a reason to call running arbitrary code a file write.
- ``external_effect`` — reaches something outside: a mail server, a calendar server, a live
  page in the operator's own browser session, a third-party tool.
- ``secret`` — touches credential material. Reading a password hands the model something no
  later approval can take back, which is why it is its own class and not an external read.

**Why the names are written out.** ``tools/`` sits above ``services/`` in the dependency
order, so walking the live catalog from here would invert it — the same constraint
``services/workspace_kind.py`` works under. The literal is not left to rot:
``tests/test_tool_sensitivity.py`` pins it against the catalog a real run resolves, in both
directions, so a renamed tool fails a test rather than silently losing its class.

**Why an unclassified name is treated as an external effect.** The catalog is not closed:
``external_{slug}_{tool}`` names come from the operator's own MCP servers and connectors,
so they cannot appear in any literal here. An unknown tool reaches something this module
cannot bound, which is exactly what ``external_effect`` means — and it is the fail-closed
direction: an unclassified tool is withheld from a read-only thread rather than admitted
to one.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class Sensitivity(StrEnum):
    """The worst case of one tool, as a class of reach."""

    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    HOST_EXEC = "host_exec"
    EXTERNAL_EFFECT = "external_effect"
    SECRET = "secret"

    @property
    def escalation(self) -> int:
        """How far past pure observation this class sits.

        The three top classes share a rank deliberately. They are different kinds of reach,
        not degrees of one — a shell command, a sent email and a read password are not on a
        line, and pretending they are would invite a policy that quietly ordered them.
        A level that wants to treat them differently names them; a level that wants "more
        than a workspace write" compares.
        """
        return _ESCALATION[self]

    def above(self, floor: Sensitivity) -> bool:
        """Whether this class escalates past ``floor`` — *not* a total order (see
        :attr:`escalation`): two classes of equal rank are each not above the other."""
        return self.escalation > floor.escalation


_ESCALATION: Mapping[Sensitivity, int] = {
    Sensitivity.READ: 0,
    Sensitivity.WORKSPACE_WRITE: 1,
    Sensitivity.HOST_EXEC: 2,
    Sensitivity.EXTERNAL_EFFECT: 2,
    Sensitivity.SECRET: 2,
}

#: The class nothing escalates past — what a caller means by "every kind of reach".
#:
#: Derived rather than spelled, and that is the whole reason it exists. The three top
#: classes share a rank (:attr:`Sensitivity.escalation`), so naming one of them at a call
#: site would read as a claim that the other two sit below it — and a sixth class that
#: reached further than all of them would silently leave such a call site behind. ``max``
#: over the rank is the statement actually being made: whichever class is furthest out,
#: this is it. Which of the tied three it lands on is immaterial, because
#: :meth:`Sensitivity.above` compares ranks and never identities.
TOP_SENSITIVITY: Sensitivity = max(Sensitivity, key=lambda s: s.escalation)

#: What a name this module has never heard of resolves to (see the module docstring).
UNCLASSIFIED: Sensitivity = Sensitivity.EXTERNAL_EFFECT

#: The prefix every operator-supplied MCP and connector tool is named under.
EXTERNAL_PREFIX = "external_"


#: Every registered tool, by namespaced name, grouped by what it can do. Grouped rather
#: than listed name-by-class because the grouping is the argument: a tool added to the
#: wrong group is obvious next to its neighbours in a way a trailing enum member is not.
SENSITIVITY_CLASSES: Mapping[Sensitivity, frozenset[str]] = {
    Sensitivity.READ: frozenset(
        {
            # Asking the operator a question changes nothing anywhere — it suspends the
            # turn and waits. Read, therefore, at every level: a thread that may only
            # look is exactly the one that should still be able to ask.
            "builtin_ask_user",
            "builtin_now",
            # Browsing that only observes. Every one of these returns what the page
            # already shows — moving through history and scrolling included, which change
            # the agent's viewport and nothing on the far side.
            "browse_console_messages",
            "browse_get_text",
            "browse_go_back",
            "browse_go_forward",
            "browse_hover",
            "browse_navigate",
            "browse_network_requests",
            "browse_screenshot",
            "browse_scroll",
            "browse_snapshot",
            "browse_tabs",
            "browse_wait_for",
            "calendar_agenda",
            "calendar_list_calendars",
            "corpus_retrieve",
            "files_file_info",
            "files_find_files",
            "files_list_directory",
            "files_read_file",
            "files_search_files",
            "mail_list_accounts",
            "mail_list_messages",
            "mail_read",
            "memory_recall",
            # The plan this thread is working from — a record we wrote and the operator
            # already read. Reading it reaches nothing.
            "plan_read",
            "platform_read",
            "platform_search",
            "plugins_read",
            "project_active",
            "project_list",
            "repo_inventory_agent_context",
            "routines_list",
            # Reading the status and output of a command someone else started. Starting
            # and stopping one are execution; asking what it printed is not.
            "shell_check_command",
            "skills_open",
            # Asking how the agents of this thread's delegation tree are getting on.
            # Reading transcripts the operator can open themselves reaches nothing.
            "agents_list",
            "agents_read",
            "tasks_read",
            # Retrieval over the network. The operator's machine and every service on it
            # are exactly as they were afterwards.
            "web_fetch",
            "web_search",
        }
    ),
    Sensitivity.WORKSPACE_WRITE: frozenset(
        {
            "attachments_provision",
            "files_create_directory",
            "files_edit_file",
            "files_write_file",
            # Sealed in this installation's own database and shown to the operator — the
            # draft never reaches the mail server, which is the whole point of preferring
            # it over `mail_reply`.
            "mail_draft_reply",
            "memory_remember",
            # A branch and a checkout beside the operator's repository, which reach their
            # own folder only through a merge they press.
            "project_open_worktree",
            # Moving the thread's own permission level, and recording the plan that moves
            # it. Both change a row this installation owns and the operator watches; what
            # stops `plan_submit` raising the level unasked is its own approval marking,
            # not this class (`tools/plan.py`).
            "plan_enter",
            "plan_submit",
            # Agents and skills an agent writes. A draft runs nowhere; publishing one, or
            # rewriting a published one, is gated on the tool itself.
            "agents_publish",
            "agents_write",
            # Stopping agents this thread spawned. Nothing new runs; the reach is that of
            # the spawn that was already allowed.
            "agents_stop",
            # A plugin an agent writes is a draft: it runs only through `plugins_test`.
            "plugins_write",
            # A routine an agent drafts never fires until the operator approves it;
            # rewriting or deleting an approved one is gated on the tool itself.
            "routines_delete",
            "routines_write",
            "skills_create",
            "skills_edit",
            "tasks_update_statuses",
            "tasks_write",
            # Killing a process this thread started. Nothing new runs and nothing outside
            # the workspace changes — the reach is the reach of the command that was
            # already approved into existence, and asking again to *end* it is asking the
            # operator to authorize the smaller half of something they already allowed.
            "shell_stop_command",
            # The View is this conversation's own surface: showing and tearing down its
            # live head changes what the operator sees and nothing they own.
            "view_close",
            "view_show",
        }
    ),
    Sensitivity.HOST_EXEC: frozenset(
        {
            # Arbitrary code, whichever machine runs it: `code_execute` in the
            # conversation's sandbox, `code_run_host_command` on the operator's own machine,
            # and the shell in the VM or, declared `host`, on that machine.
            "code_execute",
            "code_run_host_command",
            # Runs a plugin draft's code in the platform VM.
            "plugins_test",
            "shell_run_command",
            "shell_start_command",
            # A spawned agent's whole catalog, reached through one call. Its worst case is
            # at least the worst case of running a program, its arguments say nothing about
            # which tools it will use, and it runs after the spawning turn has ended.
            "agents_spawn",
            # A message can point another agent at any work its own catalog covers, and is
            # acted on after the sending turn has ended — the same statement as the spawn.
            # Reachable at every level all the same (`permissions.COORDINATION_TOOLS`): the
            # tool asks only when the message reaches an agent that can write from anywhere
            # but below it.
            "agents_message",
            # Starts a routine's agent turn, with its pre-authorizations, after this one.
            "routines_run_now",
        }
    ),
    Sensitivity.EXTERNAL_EFFECT: frozenset(
        {
            # Browsing that acts. A click submits, a keystroke sends, and evaluated
            # JavaScript runs with the page's own authority — inside a browser session
            # carrying the operator's logins.
            "browse_click",
            "browse_execute_js",
            "browse_handle_next_dialog",
            "browse_press_key",
            "browse_select_option",
            "browse_type_text",
            "calendar_create_event",
            "calendar_delete_event",
            "calendar_update_event",
            # Widens where a workspace may reach on the network. Nothing leaves the box
            # when it is called, and everything the box holds can once it has been —
            # which is the effect, and it is on the outside.
            "code_request_egress",
            # Reversible, and still on the far side of the operator's mail server.
            "mail_mark",
            "mail_reply",
            "mail_send",
        }
    ),
    Sensitivity.SECRET: frozenset(
        {
            "vault_get_entry",
            # Names, usernames and URLs — never the passwords. Still the shape of the
            # operator's credential set, which is not something a read-only thread needs.
            "vault_list_entries",
        }
    ),
}


#: Tools the chassis offers on every agent from outside the category catalog, by the name
#: the library gives them — so they carry no ``category_`` prefix, never appear on the
#: operator's tool settings page, and are exempt from the check that pins every classified
#: name to a catalog tool.
#:
#: ``run_code`` is a read, and that is a statement about the tool rather than about the
#: script: running the script touches nothing, since the sandbox has no filesystem, no
#: network and no clock. Everything a script *does* it does by calling another tool, and
#: each of those calls is gated on its own (``agent/code_mode.py``).
CHASSIS_TOOLS: Mapping[str, Sensitivity] = {"run_code": Sensitivity.READ}


_BY_NAME: Mapping[str, Sensitivity] = {
    **{name: sensitivity for sensitivity, names in SENSITIVITY_CLASSES.items() for name in names},
    **CHASSIS_TOOLS,
}


def sensitivity_of(name: str) -> Sensitivity:
    """The class of one namespaced tool name, falling back to :data:`UNCLASSIFIED`.

    Never raises: the names reaching this come from a model's tool call and from the
    operator's own external sources, neither of which this registry can enumerate ahead of
    time. The fallback is the conservative direction, so an unclassified tool is withheld
    from a read-only thread rather than admitted to one.
    """
    return _BY_NAME.get(name, UNCLASSIFIED)


def classified(name: str) -> bool:
    """Whether this module has an actual answer for ``name``, rather than the fallback.

    The distinction matters to a caller deciding what to do *before* a call exists.
    :func:`sensitivity_of` always answers, because a call that has arrived has to be ruled
    on; a caller marking tools ahead of time can ask whether the answer is knowledge or a
    guess, and treat the two differently where the difference is load-bearing
    (``services/permissions``).
    """
    return name in _BY_NAME


def tools_above(floor: Sensitivity) -> frozenset[str]:
    """Every classified tool that escalates past ``floor``.

    Only classified names, by construction — an unclassified one is unknown to this module
    until it is called, so a catalog-narrowing caller cannot withhold it and does not need
    to: the decision point that sees the call still resolves it through
    :func:`sensitivity_of`.
    """
    return frozenset(name for name, s in _BY_NAME.items() if s.above(floor))


#: The key a toolset uses to state its own tools' class on their ``ToolDefinition``.
#: See :func:`declared_sensitivity` for why the seam exists.
SENSITIVITY_METADATA_KEY = "sensitivity"


def declared_sensitivity(metadata: Mapping[str, Any] | None) -> Sensitivity | None:
    """The class a tool states about *itself*, or ``None`` when it states nothing.

    The registry above is a rule about **names**, which is all a module below ``tools/``
    can key on. That is exact for the catalog this installation ships — a test pins every
    tool in it to a class, in both directions — and silent about anything assembled at
    run time: a toolset composed in a test, and any future source of tools that is neither
    the catalog nor the operator's external surface. Those are precisely the names the
    name-registry has to treat as unknown, and unknown resolves to the class that reaches
    furthest, so without this they would be gated as though they sent mail.

    So a toolset may say what its tools are, on the definition itself, and the gate reads
    the declaration in preference to the guess. A declaration is only ever *believed* for
    tools the installation composes; it is not a channel a model or an external server can
    reach, because neither writes ``ToolDefinition.metadata``.

    An unreadable or unrecognised value is ``None`` — the name registry then answers, which
    is the conservative direction — rather than an error, since a malformed declaration
    must not be able to take a tool out of the catalog.
    """
    if not metadata:
        return None
    try:
        return Sensitivity(metadata[SENSITIVITY_METADATA_KEY])
    except KeyError, ValueError, TypeError:
        return None
