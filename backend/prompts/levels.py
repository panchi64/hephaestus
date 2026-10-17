"""Per-level prompt fragments — what the model is told about how far it may go.

A thread's permission level is enforced whether or not the model knows about it: Plan is
enforced by *absence* (the mutating tools are never offered), Manual and Edit by a park,
Auto by a review. So a fragment here buys nothing in safety — it buys the model an
explanation for what it is about to experience.

That matters at four of the five. At **Plan** the base instruction to act rather than
propose is the wrong posture, and a model that does not know why its tools are missing
reads a deliberately narrowed catalog as a broken installation and says so. At **Manual**
every change stops on the operator, so how the work is *shaped* changes: ten separate
edits are ten interruptions, and the model is the only thing that can group them. At
**Auto** there is one thing the model is asked to *state* rather than merely do — the
reach a command needs — and a declaration nothing explained would be filled in by guess.
At **Yolo** that same declaration is still what the sandbox is built from, and it is now
the *only* thing reading it — so the fragment has to say both that nothing will stop the
work and that the one statement the model makes about it is still load-bearing.

**A fragment is delivered at its level in every workspace, so it may claim nothing about the
catalog.** The tools that take a ``reach`` are the shell's, and an operator can switch the shell
off; a sentence beginning "every shell command" would be a false premise in a thread that was
never offered it. It is written as the
conditional it actually is — *where* a tool asks how far a command reaches — which costs a clause
and is true everywhere the fragment is delivered.

**Edit says nothing.** It is the level the base prompt was written for — act, the approval
gate catches what is genuinely dangerous — and repeating that here would spend
head-of-prompt tokens agreeing with the paragraph above it. Restating a rule is not
reinforcing it; it is diluting the one place it is stated.

Wired as a dynamic instruction, so — like every other instruction — the model only ever
sees the current turn's copy, rebuilt from the thread's live level and never sourced from
history. A level the operator changes mid-thread therefore takes effect on the next turn
with no history to contradict it.
"""

from __future__ import annotations

# Plan: read-only by construction. The second sentence is the load-bearing one — without
# it the model spends the turn hunting for a tool that was withheld on purpose, or reports
# the gap as a fault.
PLAN_LEVEL = """\
This thread is in plan mode: read and search, but change nothing. A tool you would need \
in order to act is absent on purpose, not missing; say what you would have used it for \
rather than looking for another way around. Investigate properly first — read the code, \
the files and the history the work touches.

Ask before you plan, not after. Where the request can be read more than one way, where \
two approaches would lead to materially different work, or where a decision turns on what \
the operator wants rather than on what the code says, use `builtin_ask_user` (several \
questions in one call, each with the option you recommend first) and plan on the \
answers. This is the level where asking is cheapest and guessing is most expensive: a \
plan built on an assumption spends the operator's whole review on correcting it, and a \
wrong assumption approved is a wrong assumption carried out. Do not ask what the files, \
the code or your tools can tell you; find that out yourself.

Then end the turn by calling `plan_submit`, which is the only way out of this mode. Write \
the plan at whatever length the work actually needs: which files, which commands, what \
would change in each, what you found that the operator would not expect, and how it would \
be verified. The operator approves it, asks for changes, or rejects it; an approved plan \
raises this thread's level and you carry it out in the same turn."""


# Manual: the level where the model's own batching decides how many times the operator is
# interrupted. Nothing else in the brief tells it that, because at every other level it is
# not true.
MANUAL_LEVEL = """\
This thread is at the Manual level: every change pauses for the operator's approval before \
it happens. Work in whole steps rather than small ones — group related changes into a \
single call, and related calls into a single stretch, so they approve one coherent piece \
of work instead of a dozen fragments of it."""


# Auto: the level where the approvals are given for the model, by a review it never sees
# the rules of. Only two things here are the model's to act on — declare the reach, and
# prefer the contained approach — and both are said in the plainest way available. The
# review's own rubric is deliberately not restated: a model told what clears the bar writes
# for the bar rather than writing the truth, which is the same reason the reviewer is never
# told the passing combination either (`prompts/utility.py`).
AUTO_LEVEL = """\
This thread is at the Auto level: a review decides in the operator's place, and only what \
it will not settle stops for them. Work that stays inside this workspace and reaches no \
network is settled without asking anyone, so prefer the approach that stays there. Where \
a tool asks how far a command needs to reach, answer it honestly — that declaration is \
what the sandbox around the command is built from, and one that needs more than it \
declared fails inside that sandbox rather than going ahead. Anything reaching the network \
is reviewed, and anything whose effect could not be undone stops for the operator however \
plainly it was asked for."""


# Yolo: nothing reviews, nothing parks, and the model is the last thing standing between
# the operator and an act nobody can undo. Two claims only — what has changed, and what
# has not — because a fragment that spent its length on warnings would be a rubric, and a
# model told what the bar is writes for the bar (the reason the reviewer's own rules are
# never restated to it either).
#
# The second paragraph is the load-bearing one, and it is about *reach* rather than about
# caution. At Auto a dishonest declaration was caught twice, by the review and by the
# fence; here only the fence is left, and a model that reads "nothing will stop me" as
# "the reach argument no longer matters" declares `host` for convenience and throws away
# the one bound the operator still has. The clause is conditional for the same reason
# Auto's is — the tools that take a reach are the shell's, and the shell is code mode's.
YOLO_LEVEL = """\
This thread is at the Yolo level: the operator has turned every approval off, and nothing \
you do here will pause for them — not a command, not a message sent, not a credential \
read, not an act that cannot be undone. There is no review behind you and no prompt in \
front of you. Work as carefully as you would if each of these were being read before it \
ran, because none of them will be, and say plainly in the turn what you actually did \
rather than leaving them to discover it.

The sandbox has not gone anywhere. Where a tool asks how far a command needs to reach, \
that answer is now the only thing deciding what the command is held to, so give it \
honestly and no wider than the work needs: take `workspace` wherever the work stays in \
this workspace, and ask for more only where it genuinely does not. Over-declaring buys \
you nothing here and removes the last bound on a command that goes wrong."""
