"""The agents this installation ships with.

The default agent is the one a chat runs when it names none. The rest are the workers the
model delegates to, and the launch tool's roster is their ``description`` lines in this
order — cheapest and most-used first — so each line is paid for on every request.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from services.agents.definition import DEFAULT_AGENT, AgentDefinition, ToolAccess
from services.subagents.report import FINDINGS_INSTRUCTION

EXPLORER = "explorer"
REVIEWER = "reviewer"
TEST_RUNNER = "test_runner"
WORKER = "worker"
RESEARCHER = "researcher"

#: The tools a sub-agent whose job is to *report* is not offered. Named rather than derived
#: from a sensitivity class because the two are not the same question: this is about the
#: sub-agent's job, not about how far it may reach, and a reviewer withheld from writing is
#: still allowed to run things.
#:
#: Stated plainly: this stops a sub-agent from *deciding* to edit, not from being able to.
#: One that also has a shell can obviously write a file through it. That is the right shape
#: anyway — the fence that matters is the permission level and the operator's approval, and
#: what this prevents is the common, well-meant failure where an agent asked to find the
#: problem quietly fixes it instead and reports a suite that now passes.
_NO_EDITING: frozenset[str] = frozenset(
    {"files_write_file", "files_edit_file", "files_create_directory"}
)

_EXPLORER_BRIEF = (
    "You are an explorer. Search and read the workspace you have been given and answer "
    "the question you were asked with concrete file paths and quoted evidence.\n\n"
    "You change nothing — you have read access and nothing else, so do not plan around "
    "editing. Be thorough in what you read and narrow in what you report: the agent that "
    "launched you is paying for every line you send back, and it needs the answer, not a "
    "tour of how you found it.\n\n"
    "Report only what you actually found. If the answer is not in the workspace, say so "
    "plainly and say where you looked — a confident guess is worse than nothing here, "
    "because nobody downstream can tell it apart from something you read."
)

_REVIEWER_BRIEF = (
    "You are a reviewer. Read the work you were pointed at and say what is wrong with "
    "it.\n\n"
    "You change nothing. Do not fix what you find — the agent that launched you is the "
    "one deciding what to do about it, and a problem you quietly repaired is one it never "
    "learns about.\n\n"
    "Report problems, each with the file and line it is at and what would go wrong if it "
    "shipped. Order them by how much they matter, and say which ones you are unsure "
    "about. Do not pad the list: a review that lists eleven things to be thorough buries "
    "the two that were real, and everything you raise costs somebody a decision.\n\n"
    "If you found nothing worth raising, say so. That is a useful answer and a short one."
)

_TEST_RUNNER_BRIEF = (
    "You are a test runner. Run what you were asked to run, and report what happened.\n\n"
    "Find the project's own way of running its tests before inventing one — the "
    "instructions it ships, the scripts it defines — and use it.\n\n"
    "Do not fix anything. You are not offered the editing tools, and that is deliberate: "
    "a failure you repaired on the way past is a failure the agent that launched you never "
    "hears about, and it is the one deciding whether the test or the code is wrong.\n\n"
    "Report the command you ran, whether it passed, and for each failure its name and the "
    "part of the output that says why. Quote the error, do not summarise it — the "
    "difference between two assertion failures is usually the whole answer. If the suite "
    "would not run at all, say that plainly rather than reporting zero failures."
)

_RESEARCHER_BRIEF = (
    (
        "You are a researcher. You have been given one question and you read the open web to "
        "answer it.\n\n"
        "You are almost certainly one of several working in parallel on different parts of a "
        "larger question, so stay on the one you were given — another researcher has the "
        "neighbouring topic, and work you duplicate is paid for twice.\n\n"
        "Gather before you conclude. Read enough independent sources to see where they "
        "disagree, and prefer the primary document over anything summarizing it. A single "
        "source that happens to confirm what you expected is the weakest possible evidence. "
        "Where sources conflict, say so, and say which you find more credible and why, rather "
        "than silently picking one.\n\n"
        "Attribute everything. Every non-obvious claim carries a link to the page you actually "
        "read it on, inline, where the claim is made — the agent that launched you is writing "
        "the real answer from your report and cannot check a claim it cannot trace. State "
        "plainly what you could not establish: a named gap is worth more than a confident "
        "sentence covering it.\n\n"
        "Report what you found, not how you searched. Nobody downstream wants the queries.\n\n"
    )
    # The researcher is the one built-in whose report is read by something other than a
    # model: a fan-out of these is what the operator's coverage map is drawn from, and a
    # map cannot be drawn from prose. The instruction is appended rather than woven in so
    # the brief above still reads as one argument about how to research.
    + FINDINGS_INSTRUCTION
)

_WORKER_BRIEF = (
    "You are a worker. Edit the workspace you have been given, run things in it, and "
    "check your own work.\n\n"
    "Do the task you were given and nothing besides it. An unrelated edit is not a "
    "bonus: it lands in somebody else's work, where nobody asked for it and nobody is "
    "looking for it.\n\n"
    "Verify before you report: run what the task tells you to run, or the project's own "
    "tests.\n\n"
    "Report what you changed, file by file, what you ran to check it, and — plainly — "
    "whatever you could not finish."
)


#: The built-ins, delegable ones in the order the model reads them.
BUILTIN: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        name=DEFAULT_AGENT,
        description="the general-purpose agent every chat starts with",
        can_delegate=True,
        delegable=False,
    ),
    AgentDefinition(
        name=EXPLORER,
        description=(
            "searches and reads the workspace and reports back with concrete paths and "
            "quoted evidence, changing nothing"
        ),
        instructions=_EXPLORER_BRIEF,
        # Read-only by absence rather than by asking: at this level a mutating tool is
        # never offered, which is the only form of read-only that survives a model
        # deciding otherwise.
        permission_ceiling="plan",
        # The launching thread's own workspace: an explorer reads the work *in progress*,
        # and a copy taken at launch would answer questions about a tree that has since
        # moved on. Safe to share precisely because it cannot write to it.
        workspace="shared",
    ),
    AgentDefinition(
        name=REVIEWER,
        description=(
            "reads work already done — a change, a file, a design — and reports what is "
            "wrong with it, worst first, changing nothing"
        ),
        instructions=_REVIEWER_BRIEF,
        # Read-only the same way the explorer is: by never being offered a tool that
        # changes anything, which is the only form of it that survives a model deciding
        # the fix was obvious.
        permission_ceiling="plan",
        # The launching thread's own workspace, and necessarily so — a review is of the
        # work in progress, and a copy taken at launch would review a tree that has since
        # moved on.
        workspace="shared",
    ),
    AgentDefinition(
        name=TEST_RUNNER,
        description=(
            "runs the project's tests (or a command you name) and reports what failed and "
            "why, quoting the output — it does not fix anything"
        ),
        instructions=_TEST_RUNNER_BRIEF,
        # Not read-only: running the suite is the job, and a level that withholds
        # execution would leave it reporting on tests it never ran.
        tools=ToolAccess(deny=_NO_EDITING),
        # Without a shell this is not a degraded test runner, it is a model recalling what
        # the suite probably does — which reads downstream exactly like a real result.
        required=frozenset({"shell_run_command"}),
        workspace="shared",
    ),
    AgentDefinition(
        name=WORKER,
        description=(
            "changes things: it edits, runs and verifies, and reports what it did — say "
            "exactly what to change and how to check it, because you cannot correct a "
            "worker while it runs"
        ),
        instructions=_WORKER_BRIEF,
        # Shared like everything else by default — a worker the launching thread is waiting
        # on should just do the work where the work is. The launch call asks for
        # ``isolated`` when the point is precisely that the thread carries on meanwhile.
        workspace="shared",
    ),
    AgentDefinition(
        name=RESEARCHER,
        description=(
            "reads the open web on one question and reports what it found with its "
            "sources — for a topic worth several searches, not a single lookup"
        ),
        instructions=_RESEARCHER_BRIEF,
        # Read-only by absence: reading the web is the entire job, and a level at which a
        # mutating tool is never offered is what keeps the launch itself ungated.
        permission_ceiling="plan",
        # Its own workspace. A researcher is not here for anyone's files — it is reading
        # the open web, which is the case ``own`` was written for.
        workspace="own",
        # Without these this is not a degraded researcher, it is a model recalling what the
        # web probably says — which reads, to everyone downstream, exactly like something
        # it went and looked up.
        required=frozenset({"web_search", "web_fetch"}),
        # Reading enough sources to see where they disagree is many more round trips than
        # answering a question.
        request_limit=120,
    ),
)


def builtin_roster() -> dict[str, AgentDefinition]:
    """The built-ins by name, as a fresh mapping the caller may merge into."""
    return {agent.name: agent for agent in BUILTIN}


def merged_roster(*layers: Iterable[AgentDefinition]) -> dict[str, AgentDefinition]:
    """Every layer folded into one catalog, later layers shadowing earlier ones by name."""
    roster: dict[str, AgentDefinition] = {}
    for layer in layers:
        for agent in layer:
            roster[agent.name] = agent
    return roster


def delegable(roster: Mapping[str, AgentDefinition]) -> dict[str, AgentDefinition]:
    """The agents other agents may hand work to."""
    return {name: agent for name, agent in roster.items() if agent.delegable and agent.published}


#: Agents beyond the built-ins named in the launch tool; the rest are found by search.
ROSTER_LISTED = 8


def describe_roster(roster: Mapping[str, AgentDefinition]) -> str:
    """The roster as the model reads it, inside the launch tool's own description: every
    built-in, then up to ``ROSTER_LISTED`` others by name, and a count of the rest."""
    builtin = [name for name, agent in roster.items() if agent.source == "builtin"]
    others = sorted(name for name, agent in roster.items() if agent.source != "builtin")
    lines = [
        f"`{name}` — {roster[name].description}." for name in [*builtin, *others[:ROSTER_LISTED]]
    ]
    if len(others) > ROSTER_LISTED:
        more = (
            f"…and {len(others) - ROSTER_LISTED} more: find them with `platform_search` "
            "(sources `agents`)."
        )
        lines.append(more)
    return "\n".join(lines)
