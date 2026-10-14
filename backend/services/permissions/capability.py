"""What an action would reach if it ran — its **worst case**, extracted before it runs.

A sensitivity class says what a *tool* can do; this says what *this call* would do. The
difference is the whole reason a level can be reviewed rather than only asked about:
``shell_run_command`` is host execution whatever its arguments say, but `git status` and
`rm -rf ~` are not the same act, and something has to be able to tell them apart without
asking the operator.

**Worst case, never likely case.** Everything here reads an action the way an attacker
would write it. A command is what its syntax permits, not what its author says it does; a
path argument is where it could land, not where it probably lands. That bias is what makes
the extraction usable by a judge that auto-approves: over-describing an action costs a
model call, under-describing one costs the operator something they never agreed to.

**How much can be said varies by tool, and saying so is part of the answer.** A shell
command is read off a grammar (``shell_ast.py``); a file write is one resolved path; an
MCP call is a server, a tool name and a set of argument *keys*, because its effect is
defined on the far side of an API this process cannot see. That last case is not a gap to
be filled in later — it is the honest description, and it is why
:attr:`Capability.unbounded` exists rather than an optimistic empty set.

**One family is the exception to the opening sentence, and deliberately so.** For a tool
the shipped catalog classifies as a *read*, "what this call would do" and "what this tool
can do" are the same fact: it returns something and leaves nothing different behind,
whatever its arguments say. Those calls are described from the class alone
(:attr:`ActionKind.READ`) — not because reading arguments would be hard, but because
there is nothing in them left to find.

**Between "read off a grammar" and "named by its argument keys" sits a third reading, and
it lives next door** (``projections.py``): the per-tool table that says how one of this
installation's *own* tools presents itself, because describing a mail send by its keys
alone ("Calls mail_send with arguments body, subject, to") leaves the reviewer's third
axis, ``correctness``, with nothing to rule on. That table is a separate module for the
reason it is a table at all — it changes when a shipped tool's arguments change, which is
not when this file's reading of a *deferred call* changes, and the question a security
reader has of it ("does anything quote a mail body") is one they should be able to answer
by reading one file.

**A projection makes a review better informed; it never makes one unnecessary.** These
capabilities stay :attr:`ActionKind.OPAQUE` with a non-empty :attr:`Capability.unbounded`,
because quoting four arguments of a mail send does not turn the far side of a mail server
into something this process read. Nothing here can be cleared by ``judge.py``, before or
after — only the amount the model reviewer has to work with changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

from services.permissions.projections import describe
from services.permissions.shell_ast import (
    ShellCommand,
    escapes_workspace,
    shell_reach,
    strip_comments,
)
from services.tool_sensitivity import EXTERNAL_PREFIX, Sensitivity, classified, sensitivity_of

#: How far a shell command says it needs to reach, declared by the model on the call and
#: enforced by the tool that runs it (``tools/shell.py``). It is a *declaration*, not a
#: measurement: the grammar walk checks it against what the command actually names, and a
#: fence built to match it is what makes a contradiction fail rather than merely be noted.
type Reach = Literal["workspace", "network", "host"]

#: The declared values, as a set to validate an argument against.
REACHES: frozenset[str] = frozenset({"workspace", "network", "host"})

#: What a call that names no reach at all is taken to have declared. It is the executing
#: tools' own schema default, so an omitted argument is not a missing declaration — it is
#: the declaration the tool will act on, and the judge has to model what will run.
DEFAULT_REACH: Reach = "workspace"

#: The tools whose schema actually carries a ``reach`` argument (``tools/shell.py``). Only
#: for these does an absent argument mean :data:`DEFAULT_REACH`; for every other shell-shaped
#: tool it means the call declared nothing, which is a different fact and has to read as one
#: — the reviewer's prompt and the operator's review row both say what was declared, and a
#: tool with no such argument reporting "workspace" is the chassis putting words in the
#: model's mouth.
_REACH_ARG_TOOLS = frozenset({"shell_run_command", "shell_start_command"})


class ActionKind(StrEnum):
    """What sort of act this is, which decides what can be said about it at all."""

    #: A command handed to a shell — the kind whose worst case is readable from a
    #: grammar, and the only one whose *arguments* are what has to be ruled on.
    SHELL = "shell"
    #: A tool this installation ships and classifies as observing: it returns something
    #: and leaves nothing different behind. The one kind whose worst case is settled by
    #: the tool alone, so its arguments never need reading (see :func:`capability_of`).
    READ = "read"
    #: A named file or directory this installation would change.
    FILE = "file"
    #: A call to something outside — an operator's MCP server, a connector, a mail or
    #: calendar server. The arguments are the far side's vocabulary, not ours.
    EXTERNAL = "external"
    #: Everything else: a tool whose effect is its own and not written in its arguments.
    OPAQUE = "opaque"


@dataclass(frozen=True)
class Capability:
    """The worst case of one deferred call, as facts something else can rule on.

    Deliberately **not** a verdict. This module says what an action reaches; ``judge.py``
    says whether that is allowed and ``reviewer.py`` says whether it was wanted. Keeping
    the three apart is what lets the rule change without the extraction changing, and what
    lets a test pin "this command reads these two files" independently of any policy.
    """

    #: The namespaced tool the operator's model asked for.
    tool: str
    kind: ActionKind
    #: One line, in the operator's language: what this would do at its worst. Rides on
    #: the review events and into the reviewer's prompt, so it is the same sentence the
    #: operator reads and the model is judged against — two wordings would be two facts.
    summary: str
    #: The act's own model-authored content, where a tool has some worth reading and it is
    #: too long for a line — a delegated task, a research question, a program, the reason
    #: given for opening a credential. None where there is none, which is most tools.
    #:
    #: Kept apart from :attr:`summary` rather than appended to it because the two are read
    #: in different places and by different rules: the summary is the line on the operator's
    #: review row and the one sentence the reviewer scores against, while this is the body
    #: it may need in order to say whether the act matches the request. Both ride inside the
    #: reviewer's untrusted fence (``reviewer.py``) — a projection quotes the model's own
    #: words, and quoting them is not the same as trusting them.
    detail: str | None = None
    #: Every command a shell action would run, in the order they appear.
    commands: tuple[ShellCommand, ...] = ()
    #: Paths the action names for reading.
    reads: tuple[str, ...] = ()
    #: Paths the action names for writing — a redirect target, a file tool's target.
    writes: tuple[str, ...] = ()
    #: Environment variables the command sets for what it runs.
    env_writes: tuple[str, ...] = ()
    #: Whether the grammar walk saw a network address written in the command (a URL, a
    #: network redirect). It is a statement of what was *seen*, not of what the act can
    #: reach; a sandboxed program is never read, so it is False for one.
    network: bool = False
    #: Paths that leave the workspace, or that we cannot place inside it.
    escapes: tuple[str, ...] = ()
    #: How far the call *said* it needs to reach. None for every kind of act that declares
    #: nothing — a mail send, a file write, an MCP call — which is a different fact from
    #: declaring the widest reach, and the two must not read the same on a review row.
    reach: Reach | None = None
    #: Whether this act runs inside the conversation's own sandbox rather than on the
    #: host. The sandbox is itself a fence, so an offline call to one is bounded without
    #: anything here having read its program — which is the only way an interpreter's
    #: program is ever bounded.
    sandboxed: bool = False
    #: Why the worst case could not be pinned down — one entry per construct that was not
    #: interpreted. Non-empty means nothing here may be read as complete.
    unbounded: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bounded(self) -> bool:
        """Whether everything in this action was understood. False ⇒ the fields above
        describe *part* of it, and no caller may treat them as the whole."""
        return not self.unbounded

    @property
    def programs(self) -> tuple[str, ...]:
        """The program names a shell action would run, in order."""
        return tuple(command.program for command in self.commands)


#: Shell-shaped tools, by the argument carrying the command. `code_execute` is here for
#: its `bash` language only — its `python` program is code, not a command line, and there
#: is no grammar walk that bounds an interpreter.
_COMMAND_ARG: dict[str, str] = {
    "shell_run_command": "command",
    "shell_start_command": "command",
    "code_run_host_command": "command",
    "code_execute": "code",
}

#: The one tool whose entire security content is an argument *value*. Everything else here
#: is described by its argument keys (see :func:`_arg_shape`), because a value may be a
#: password or a page of untrusted text — but a request to widen the egress allowlist that
#: does not say *which host* describes nothing at all, and a reviewer handed it would be
#: clearing the fence's only exit blind.
_EGRESS_TOOL = "code_request_egress"

#: File tools, by the argument naming the target. Their worst case is one path, which is
#: the whole of what there is to say about them.
_PATH_ARG: dict[str, str] = {
    "files_write_file": "path",
    "files_edit_file": "path",
    "files_create_directory": "path",
}


#: Shell-shaped tools whose command does **not** run in the run's workspace.
#: `code_run_host_command` runs on the operator's own machine, where a sandbox thread's
#: workspace is a directory the host fence denies outright and a worktree does not exist
#: at all (it is withheld from worktree chats). So there is no root its
#: paths can honestly be placed against — and placing them against one anyway is exactly
#: what let `cat .env` read as a workspace file.
_HOST_COMMAND_TOOLS = frozenset({"code_run_host_command"})

#: What goes on the record when a command's paths could not be placed. Both belong in
#: ``unbounded`` rather than reading as a clean walk, because that is what they are: every
#: relative path in the command means *something*, and neither case can say what — which
#: is the one thing the deterministic stage would have to know to clear it. Two wordings
#: because the operator reads them on the review row, and "there is no workspace" and
#: "the workspace is not where this runs" are different facts about their machine.
_UNPLACED = "there is no workspace directory to measure this command's paths against"
_UNPLACED_ON_THE_HOST = (
    "runs on the host, where the workspace this run's paths would be measured against "
    "is not the directory it starts in"
)


def measured_against_root(tool: str) -> bool:
    """Whether this tool's worst case depends on where the run's workspace is.

    Only a command that runs *in* that workspace and a file target are placed against a
    root; every other tool is described by its name, its class and its argument keys, and
    the answer is the same wherever the run works. A host command is in that second group
    for a less obvious reason — it runs on the host, not in the workspace, so a root would
    be the wrong measure rather than a missing one. The caller that has to *open* a
    workspace to supply the root asks first (``agent/gating.py``): opening one is a `git
    worktree add` or a sandbox start, and paying it to judge a mail send buys nothing.
    """
    if tool in _HOST_COMMAND_TOOLS:
        return False
    return tool in _COMMAND_ARG or tool in _PATH_ARG


#: Shell-shaped tools whose command runs inside the conversation's own sandbox. The
#: sandbox is the fence there, so nothing about the command has to be understood for the
#: act to be bounded — see :attr:`Capability.sandboxed`.
_SANDBOXED_TOOLS = frozenset({"code_execute"})


def declared_reach(tool: str, args: dict[str, object]) -> Reach | None:
    """How far this call says it needs to reach, or None when it said nothing.

    Four readings, and each is the conservative one for its case. A **host command**
    declares ``host`` whatever its arguments say: it runs on the operator's machine, which
    is the definition of the widest reach. An argument **absent from a tool that has one**
    is :data:`DEFAULT_REACH` — not a missing declaration but the schema default the tool
    will actually run under, and reading it as anything else would make every call that
    left the argument off escalate while running fenced to the worktree anyway. A tool with
    **no such argument at all** declares nothing, and says so with ``None``: `code_execute`
    cannot state a reach, so reporting one for it would be this module writing a
    declaration the model never made. A **value this module does not recognise** is
    ``host``, and an explicit ``null`` is one of those: the tool's own validation would
    refuse it, so no such call ever runs, and the widest reading is the only one that
    cannot be wrong about a call that somehow did.
    """
    if tool in _HOST_COMMAND_TOOLS:
        return "host"
    if "reach" not in args:
        return DEFAULT_REACH if tool in _REACH_ARG_TOOLS else None
    value = args["reach"]
    return cast("Reach", value) if value in REACHES else "host"


def shell_capability(
    tool: str, command: str, *, root: Path | None, reach: Reach | None = DEFAULT_REACH
) -> Capability:
    """One shell command as a capability — the grammar walk, wrapped in the common shape.

    The summary carries the command **without its comments**. A comment changes nothing
    about what runs; it is the one part of a command addressed to whoever *reads* it, and
    both readers here are ones an author might want to talk into something — the reviewer
    that scores the act, and the operator looking at the approval row. What runs is
    untouched, and the walk above still read the whole thing.

    **No root is a fact about the reading, not a permissive default.** Absolute and upward
    paths already read as escapes without one (``escapes_workspace``), but a bare
    `cat .env` names no directory at all, so nothing escapes and nothing is written — and
    a stage looking only at those fields would clear a command whose working directory
    this process never established. It is recorded as unread instead, which escalates the
    whole capability the way any uninterpreted construct does.

    A host command is read that way **whatever root it is handed**: it runs on the
    operator's machine rather than in the workspace, so a root here would be a wrong
    measure rather than a missing one, and the decision belongs where the reading is made
    rather than at each of the callers that happen to know a root.

    ``reach`` is what the call *declared*, carried onto the capability unexamined. Checking
    it against the paths the walk found is a policy question and belongs to the stage that
    rules (``judge.py``); recording the two side by side is this module's whole job.
    """
    on_the_host = tool in _HOST_COMMAND_TOOLS
    root = None if on_the_host else root
    walk = shell_reach(command, root=root)
    unplaced = () if root is not None else (_UNPLACED_ON_THE_HOST if on_the_host else _UNPLACED,)
    return Capability(
        tool=tool,
        kind=ActionKind.SHELL,
        summary=f"Runs the shell command: {strip_comments(command)}",
        commands=walk.commands,
        reads=walk.reads,
        writes=walk.writes,
        env_writes=walk.env_writes,
        network=walk.network,
        escapes=walk.escapes,
        reach=reach,
        sandboxed=tool in _SANDBOXED_TOOLS,
        unbounded=(*walk.unbounded, *unplaced),
    )


def capability_of(tool: str, args: dict[str, object], *, root: Path | None = None) -> Capability:
    """The worst case of one deferred call to ``tool`` with ``args``.

    Total: every tool resolves to *something*, because a call that has arrived has to be
    described before it can be ruled on. What varies is how much can be said, and a tool
    this module has no rule for is described by its name and the keys of its arguments —
    enough for a reviewer to judge, and never enough for the judge to clear.
    """
    command_arg = _COMMAND_ARG.get(tool)
    if command_arg is not None:
        return _command_capability(tool, args, command_arg, root)

    path_arg = _PATH_ARG.get(tool)
    if path_arg is not None:
        return _file_capability(tool, args, path_arg, root)

    if tool.startswith(EXTERNAL_PREFIX):
        # The slug and the far-side tool name, which is all this process knows: an MCP
        # server's arguments are its own vocabulary, and guessing at their meaning would
        # be inventing a boundary rather than describing one.
        source, _, remote = tool.removeprefix(EXTERNAL_PREFIX).partition("_")
        return Capability(
            tool=tool,
            kind=ActionKind.EXTERNAL,
            summary=(f"Calls {remote or source} on the {source} connector with {_arg_shape(args)}"),
            network=True,
            unbounded=("an external tool's effect is defined on the far side",),
        )

    if classified(tool) and sensitivity_of(tool) is Sensitivity.READ:
        # A tool the shipped catalog classifies as observing. Its arguments say *what*
        # it looks at, and nothing at all about what it changes — because the answer to
        # that is "nothing", and it is the same answer for every argument set. So this is
        # the one description that is complete without reading them, and `unbounded` is
        # correctly empty: there is no construct here that went uninterpreted.
        #
        # Only a *classified* name qualifies. An unknown one resolves to the class that
        # reaches furthest (`tool_sensitivity`), so nothing an operator's own MCP server
        # names can arrive here wearing a read's clothes.
        #
        # A projection here is for the **operator's row**, not for a ruling: the class has
        # already settled the act, and `kind` stays READ so it still clears with no review.
        # What it buys is that the row says which page was opened rather than which
        # argument keys were passed.
        summary, detail = describe(
            tool, args, fallback=f"Reads with {tool}, using {_arg_shape(args)}"
        )
        return Capability(tool=tool, kind=ActionKind.READ, summary=summary, detail=detail)

    if tool == _EGRESS_TOOL:
        return Capability(
            tool=tool,
            kind=ActionKind.OPAQUE,
            summary=f"Opens this workspace's network to {_domain_shape(args)}",
            network=True,
            # Which hosts is the whole of what the request says; what they would then be
            # used for is not written anywhere in it, and that is the part worth a human.
            unbounded=("what an opened host carries is not written in the request",),
        )

    summary, detail = describe(tool, args, fallback=f"Calls {tool} with {_arg_shape(args)}")
    return Capability(
        tool=tool,
        kind=ActionKind.OPAQUE,
        summary=summary,
        detail=detail,
        # A projection quotes more of the call; it does not turn the far side of a mail
        # server or a sub-agent's whole catalog into something this process read. So the
        # reason stands whether or not one applied — and `bounded`, the property whose
        # entire job is to say "the fields here describe the whole act", keeps answering
        # False about an act nothing here bounded.
        unbounded=("this tool's effect is not written in its arguments",),
    )


def _command_capability(
    tool: str, args: dict[str, object], command_arg: str, root: Path | None
) -> Capability:
    command = _text_arg(args, command_arg)
    if command is None:
        return Capability(
            tool=tool,
            kind=ActionKind.SHELL,
            summary=f"Runs {tool} with no command given",
            unbounded=("the call carries no command to read",),
        )
    if tool == "code_execute" and args.get("language", "python") != "bash":
        language = args.get("language", "python")
        summary, detail = describe(
            tool,
            # The schema default filled in, for the reason `declared_reach` fills in its
            # own: an omitted argument is not an absent fact but the value the tool will
            # run under, and both the review row and the reviewer are describing what is
            # about to happen rather than what was typed.
            {**args, "language": language},
            fallback="Runs a program in the conversation's sandbox",
        )
        return Capability(
            tool=tool,
            kind=ActionKind.OPAQUE,
            summary=summary,
            detail=detail,
            sandboxed=True,
            unbounded=("an interpreter's program is not bounded by its arguments",),
        )
    # `network` on the returned capability is the grammar walk's own — a URL named in the
    # command. There is no tool argument left to widen it with: egress is no longer a flag
    # on the run, so the workspace reaches what its allowlist names and adding to that list
    # is its own approval-gated call.
    return shell_capability(tool, command, root=root, reach=declared_reach(tool, args))


def _file_capability(
    tool: str, args: dict[str, object], path_arg: str, root: Path | None
) -> Capability:
    path = _text_arg(args, path_arg)
    if path is None:
        return Capability(
            tool=tool,
            kind=ActionKind.FILE,
            summary=f"Runs {tool} with no path given",
            unbounded=("the call carries no path to read",),
        )
    return Capability(
        tool=tool,
        kind=ActionKind.FILE,
        summary=f"Writes to {path}",
        writes=(path,),
        escapes=(path,) if escapes_workspace(root, path) else (),
    )


def _text_arg(args: dict[str, object], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) else None


def _domain_shape(args: dict[str, object]) -> str:
    """The hosts an egress request names, in the order it named them. The exception to
    :func:`_arg_shape`'s rule, argued for at :data:`_EGRESS_TOOL`."""
    value = args.get("domains")
    entries = cast("list[object]", value) if isinstance(value, list) else []
    names = [str(d) for d in entries if str(d).strip()]
    return ", ".join(names) if names else "no host it named"


def _arg_shape(args: dict[str, object]) -> str:
    """An argument set named by its keys, never by its values.

    The shape goes to a model and onto an event; the values may be a password, a mail
    body, or a page of untrusted text. Naming the keys says what kind of call this is
    without moving the payload anywhere it was not already going.
    """
    if not args:
        return "no arguments"
    return "arguments " + ", ".join(sorted(args))
