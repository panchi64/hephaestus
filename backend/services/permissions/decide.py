"""What to do with a tool call the model wants to make — one function, four answers.

The engine reaches here at the single point where Pydantic AI hands back a *deferred*
call: a call the library declined to execute and returned for someone to rule on. Two
things put a call in that list, and telling them apart is the whole job of this module:

- **the level put it there.** The toolset marks every tool that reaches past the level's
  ceiling as needing approval (``tools/toolsets.py``), so the model's request for one
  comes back undone. This is the level's own question, and the level's approval policy
  answers it: withheld under Plan (so the call is refused outright rather than asked
  about), parked under Manual and Edit, reviewed under Auto. Yolo never asks it — its
  ceiling is above every class, so nothing of its is deferred by the level at all.
- **the tool put itself there.** A global recall, a skill edit, an untrusted external
  tool — tools that gate their own calls for reasons this axis knows nothing about. Those
  are the operator's to answer, which is why they come back ``ASK`` even under a level
  whose scope would have permitted them. A level widens a gate; it never narrows one
  (``levels.py``). Auto is not an exception to that: it answers them by review because
  answering on the operator's behalf is the whole of what choosing Auto means.

  **Yolo is the exception, and the only one.** It answers these with ``ALLOW`` — the
  operator asked not to be asked, and a level that still parked on a corpus recall would
  not be the thing its name promises. That is one branch in :func:`decide`, it says so
  where it is taken, and ``levels.py`` holds the argument for why exactly one level may
  do it.

**Why the vocabulary has four members.** ``ALLOW`` has three producers and they are not
interchangeable. A standing conversation grant produces it — the operator's explicit "stop
asking me about this one". Auto's review returns it for a call it clears. And a **level**
returns it at Yolo, where nothing is asked. Expressing all three in the same vocabulary is
what keeps the engine to one dispatch; telling them apart afterwards is
``agent/gating.py``'s job, because only the grant is revocable while a run sits parked and
so only the grant's allow is re-checked on resume (``routes/runs.py``). Note where the
grant sits in that dispatch: it answers a question this module *asked*, and never overturns
a refusal. A grant is consent to skip a prompt, not consent to act in a thread the operator
set to act in nothing. **At Auto it does not answer the question at all** — it is carried
into the review as the operator's authorization (:func:`review`), because at that level the
question was never "may this tool run" but "what would this call do", and a grant on
`shell_run_command` is not an answer to that for every command. At Yolo it is not consulted
either, there being no prompt left for it to skip.

**The second half of this module is that review** (:func:`review`), which is what
``REVIEW`` resolves to: the deterministic stage first (``judge.py``), the model second
(``reviewer.py``), and the arithmetic over their answers here — deliberately *here*,
where a reader can see the whole rule at once and where the reviewer cannot read it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from services.permissions.capability import Capability
from services.permissions.judge import Tier, judge
from services.permissions.levels import ApprovalPolicy, beyond_scope, permission_spec
from services.permissions.reviewer import (
    Authorization,
    GrantWidth,
    Reviewer,
    ReviewRequest,
    ReviewVerdict,
    TranscriptEntry,
)


class Decision(StrEnum):
    """What happens to one deferred tool call."""

    #: Run it, with no operator round-trip. Never the answer to a *level's* question —
    #: only to the operator's own standing grant, and to a review that cleared it.
    ALLOW = "allow"
    #: Judge and review it (Auto) — :func:`review` settles it into one of the other
    #: three. Not itself an outcome: a caller that cannot run the review parks.
    REVIEW = "review"
    #: Park the run and put the call in front of the operator.
    ASK = "ask"
    #: Refuse it and tell the model why (:func:`blocked_message`). Only a *level* produces
    #: this: under Plan the operator's answer is already on the record, in the level they
    #: chose. Auto's review never does — an unrecoverable act is the case the operator most
    #: needs to be shown, so it parks instead of being refused on their behalf.
    BLOCK = "block"


#: The answers a level gives to a call that reached past its ceiling. Total over
#: :class:`ApprovalPolicy` rather than over the policies that can actually arrive here:
#: ``ALLOW``'s row is unreachable today, because the only level carrying that policy has a
#: ceiling nothing is above, so no call of its is ever *past* the ceiling and none of them
#: reach this lookup. The row is written anyway — a partial mapping keyed on an enum fails
#: as a ``KeyError``, and inside a permission decision is the worst place in this codebase
#: to discover a missing case.
_BY_POLICY = {
    ApprovalPolicy.WITHHOLD: Decision.BLOCK,
    ApprovalPolicy.ASK: Decision.ASK,
    ApprovalPolicy.REVIEW: Decision.REVIEW,
    ApprovalPolicy.ALLOW: Decision.ALLOW,
}


def decide(level: str, tool: str) -> Decision:
    """Rule on one deferred call to ``tool`` in a thread at ``level``.

    Pure and total: an unknown level resolves to the strictest one and an unclassified
    tool to the class that reaches furthest, so neither a corrupt stored value nor an
    operator's own MCP tool can arrive here and be waved through.

    **``ALLOW`` comes back from exactly one level**, and it is the one whose name says so.
    Every other level permits by *not deferring in the first place*, so anything of theirs
    that reaches here is something someone still has to answer; Yolo is the operator
    saying that nobody has to. Note what that costs and where the cost is bounded: a
    standing grant is still the thing that produces an ``ALLOW`` at Manual and Edit, and a
    review still produces one at Auto, so no level below this one changed meaning.
    """
    policy = permission_spec(level).approval_policy
    if not beyond_scope(level, tool):
        # Within the level's scope, so the level is not what deferred it: the tool gated
        # its own call. Below the top level no level may wave that through — the operator
        # answers it, unless they have chosen the level that delegates their answer to the
        # review, which is the whole of what Auto is. Delegating the answer is not
        # deleting the gate.
        #
        # At Yolo it *is* deleting the gate, deliberately and by that name. A level whose
        # proposition is that nothing in the thread stops cannot honour it while still
        # parking on a corpus recall or a vault read — those are gates raised for reasons
        # this axis knows nothing about, and "I do not want to be asked" is an answer to
        # all of them or to none. `levels.py` states the exception; this is where it is
        # taken, and it is the only place.
        match policy:
            case ApprovalPolicy.ALLOW:
                return Decision.ALLOW
            case ApprovalPolicy.REVIEW:
                return Decision.REVIEW
            case _:
                return Decision.ASK
    return _BY_POLICY[policy]


def blocked_message(level: str, tool: str) -> str:
    """What the model is told when a call is refused, in place of the tool's result.

    Names the level and the fact that no prompt is coming, so the model re-plans instead
    of re-calling the tool and waiting: a refusal the model reads as a transient failure
    is a refusal it will spend the rest of the turn retrying.
    """
    return (
        f"This conversation is at the {permission_spec(level).level} permission level, so "
        f"{tool} was not run and the operator was not asked. Nothing here can change "
        "anything until they raise the level; say what you would do instead."
    )


# --- Auto's review ------------------------------------------------------------------
#: Which stage settled a review — the structural judge, or the model.
type ReviewStage = Literal["judge", "reviewer"]


@dataclass(frozen=True)
class ReviewOutcome:
    """What the two stages made of one call, and why.

    ``reason`` is not decoration. Auto's whole proposition is that the operator's answers
    are given for them, and the only thing that makes that acceptable is being able to
    read afterwards *what was decided and on what grounds* — so the reason travels onto
    the work log's review row beside the call it judged.
    """

    decision: Decision
    stage: ReviewStage
    reason: str
    #: The model stage's three axes, when it ran. None when the deterministic stage
    #: settled it, or when the model stage could not be reached at all.
    verdict: ReviewVerdict | None = None
    #: Which deterministic ground cleared it, when one did — None on everything the model
    #: stage settled. It rides onto the review row beside the reason because "cleared, and
    #: it ran fenced to the worktree" and "cleared, because the tool only observes" are
    #: different assurances, and an operator auditing the level has to tell them apart.
    tier: Tier | None = None
    #: Whether the operator's standing grant is what turned this into an ALLOW — the
    #: reviewer found nothing either way and the grant supplied the yes. It travels because
    #: a grant is **revocable** while the run that used it sits parked: the engine marks
    #: such an approval as the grant's (``agent/gating.py``) so the resume path re-checks it
    #: against the grants as they stand then (``routes/runs.py``). An allow the review
    #: reached on its own grounds has nothing to re-check and must not be re-checked.
    by_grant: bool = False
    #: Whether that re-check has to find the grant still at its **wider** width. Set only
    #: where the whole-tool width is what did the work — it cleared an act nobody can undo
    #: — because that is the one allow a narrower surviving grant cannot stand in for. Every
    #: other grant-driven allow is re-checked as it always was, by whether any live grant
    #: still covers the call: at Manual and Edit either width settles it outright, so
    #: demanding the wider one there would deny a call the level's own rule still permits.
    needs_whole_tool: bool = False


@dataclass
class ReviewBudget:
    """How many model reviews one turn may still spend.

    A turn is not one deferred call: a model that keeps reaching past the level's ceiling
    is reviewed on every hop, and each review is a round trip on the utility model with a
    timeout measured in seconds. Without a ceiling, a turn that loops through sensitive
    calls pays for a review per call — latency the operator watches, on a turn that is
    already going wrong.

    So the cap is per *turn* and shared by every batch in it, and what it counts is model
    calls: a structurally-cleared command spends nothing, however many of them there are.
    Beyond it the calls **park**, which is the same degrade every other failure of the
    review takes — the operator is asked the question the review was answering for them.
    Mutable and passed by reference for the same reason the turn's usage budget is.
    """

    limit: int
    spent: int = 0

    def take(self) -> bool:
        """Claim one model review, or report that this turn has none left."""
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


async def review(
    capability: Capability,
    *,
    reviewer: Reviewer | None,
    transcript: Sequence[TranscriptEntry] = (),
    granted: GrantWidth = "none",
    fenced: bool = False,
    budget: ReviewBudget | None = None,
    nonce: str | None = None,
) -> ReviewOutcome:
    """Rule on one call at the Auto level: the deterministic stage, then the model.

    ``nonce`` is the fence token this call's untrusted blocks carry. A caller reviewing a
    *batch* over one transcript passes one for the whole batch, so the shared prose at the
    front of every prompt is byte-identical and an engine can serve it from the prefix it
    already processed (``reviewer.ReviewRequest.nonce``). Omitted, one is minted for this
    call alone — the safe default, and what a single review wants.

    ``fenced`` is the fact about the *host* the deterministic stage needs and cannot look
    up for itself (``judge.py`` is pure). It defaults to the strict reading — no fence —
    because a caller that could not say is a caller with nothing to hold a command to, and
    the cost of being wrong that way is a model call rather than an act nobody cleared.

    ``granted`` is how far the operator's standing conversation grant reaches over this
    call (``services/approval_grants.py``). At every other level a grant of either width
    *is* the answer and settles the call; here the two widths part, because here the
    question is what one particular call would do.

    A ``"command"`` grant is an **input to the review**. It names one act, so it is not an
    answer about every call the tool could make — the grant supplies the authorization and
    the rest of the review still runs, which is what keeps one tick under a test run from
    switching the review off for every command in the thread. It supplies that
    authorization only where the reviewer had **nothing** to say: a reviewer that read the
    operator as having refused this act in this very turn is reading their most recent
    word, and a grant left earlier in the thread does not get to outrank it.

    A ``"tool"`` grant *is* that answer, and is read as one. The operator was offered the
    wider width in its own words — this tool, everything it runs, for this thread — and
    picked it, so the review takes it as their authorization outright rather than only
    where it found a gap. **It reaches the risk axis too**, and is the only thing that
    does: a ``too_destructive`` verdict runs under a whole-tool grant and parks under
    every other (:func:`_verdict_decision`).

    That is the one place the level hands an unrecoverable act over without showing it,
    and it is deliberate. A tool whose act cannot be *read* by the review — arbitrary
    script in the operator's own browser is the case that forced this, since the projection
    withholds the script precisely so a password cannot reach the reviewer — draws the same
    contentless verdict on every call, so it would be re-asked forever however the operator
    answered. Offering a standing yes that can never take effect is worse than honouring
    the one they gave: it is the same promise the card makes, and the same promise Manual
    and Edit have always kept, where a grant of either width settles the call before the
    risk axis is consulted at all. A ``"command"`` grant is not enough for this — agreeing
    to an act is not agreeing that the act is recoverable — and neither is a reviewer's own
    reading of the turn.

    **Both widths are inputs to a review that *ran*.** Where there is no reviewer at all
    there is nothing for a grant to authorize, so the call parks like every other
    unreviewable one — and that is the case the wider width most needs to keep, since a
    grant that settled the call outright here would switch the level off on any
    installation with no utility model bound, where nothing would ever have looked at the
    act to say whether it was recoverable.

    The combination, stated once here and written down nowhere the reviewer can read it
    (``reviewer.py``):

    - the deterministic stage's approval **runs**, with no model call at all;
    - ``too_destructive`` **parks**, unless the operator holds a whole-tool grant over the
      tool — it is the one act they most need to be shown rather than told about
      afterwards, and the only thing that outranks that is their own standing word,
      given in the width whose copy says so;
    - ``low`` risk **runs** unless the operator said no;
    - ``high`` risk runs **only** on an explicit yes;
    - everything else **parks**, and so does every way this can fail.

    Note what is deliberately absent: ``correctness`` moves nothing. It is an observation
    for the operator to read on the review row, not a fourth term — a reviewer that could
    veto on "this looks like the wrong path" would be second-guessing the model's work
    rather than ruling on its permission, and those are different jobs.
    """
    verdict_of_judge = judge(capability, fenced=fenced)
    if verdict_of_judge.approved:
        return ReviewOutcome(
            Decision.ALLOW, "judge", verdict_of_judge.reason, tier=verdict_of_judge.tier
        )
    if reviewer is None:
        # No utility model bound, or none reachable from here. The conservative branch is
        # the default at exactly this point, because the alternative is an action nobody
        # — no operator, no judge, no reviewer — ever agreed to. A standing grant does not
        # rescue it: the grant is an input to a review, and the review this call needed is
        # the one that could not run. The operator is asked instead, which is the same
        # degrade a timeout and an unparseable answer take.
        return ReviewOutcome(
            Decision.ASK, "judge", f"{verdict_of_judge.reason}; no reviewer is available"
        )
    if budget is not None and not budget.take():
        # Claimed before the call rather than counted after it, so a turn cannot exceed the
        # cap by having several reviews in flight at once (``agent/gating.py`` runs a
        # batch's reviews together).
        return ReviewOutcome(
            Decision.ASK,
            "judge",
            f"{verdict_of_judge.reason}; this turn has already spent its {budget.limit} reviews",
        )
    request = ReviewRequest(capability=capability, transcript=transcript)
    verdict = await reviewer(replace(request, nonce=nonce) if nonce else request)
    if verdict is None:
        return ReviewOutcome(
            Decision.ASK, "reviewer", f"{verdict_of_judge.reason}; the review did not complete"
        )
    # Derived once and handed to both halves: the arithmetic and the row's account of it
    # have to be reading the same authorization, and re-deriving it in each was two places
    # for the grant's rule to be stated in.
    authorization = _authorization(verdict, granted=granted)
    decision = _verdict_decision(verdict, authorization=authorization, granted=granted)
    # The same verdict read with no grant at all. Derived once: it is what says whether the
    # grant changed the answer, and re-deriving it per use was two spellings of one
    # counterfactual. Both axes are reset, not just the authorization — a grant that
    # cleared an unrecoverable act did so on the risk axis, and a counterfactual that left
    # the width in place would not notice it.
    without_grant = _verdict_decision(verdict, authorization=verdict.authorization, granted="none")
    # The one clearance the risk axis owes the operator an explicit account of.
    unrecoverable = verdict.risk == "too_destructive" and decision is Decision.ALLOW
    return ReviewOutcome(
        decision,
        "reviewer",
        _verdict_reason(
            verdict,
            grant=granted if authorization != verdict.authorization or unrecoverable else "none",
            unrecoverable=unrecoverable,
        ),
        verdict,
        # Marked as the grant's only where the grant is what *changed* the answer — the
        # same verdict read without it parks. A low-risk act the review would have cleared
        # anyway does not become revocable for having a grant sitting beside it, and
        # marking it so would have the resume path deny a call the review approved.
        by_grant=decision is Decision.ALLOW and without_grant is not Decision.ALLOW,
        needs_whole_tool=unrecoverable,
    )


def _verdict_decision(
    verdict: ReviewVerdict, *, authorization: Authorization, granted: GrantWidth
) -> Decision:
    """The arithmetic, one branch per risk word.

    Written as a match on the *named* values rather than as a chain ending in an else,
    because the else was `high`'s rule wearing "everything else"'s name: a fourth risk
    word — a middle one, added because two levels of severity were not enough — would
    have inherited the one branch that can return ALLOW on nothing more than an
    authorization. Unnamed risk parks, which is what the docstring above always said.

    ``authorization`` is the collapsed axis :func:`_authorization` produces, and every
    branch but the first runs on it alone. The verdict object itself is left exactly as
    the model returned it, because it travels onto the review row and onto the run's own
    event, and what the model concluded from the thread is a different fact from what the
    operator said when they left the grant.

    **``granted`` is taken separately, and only the ``too_destructive`` branch reads it.**
    That branch needs to know *why* the authorization says yes, which the collapsed axis
    cannot tell it: a reviewer inferring consent from the prose of a turn and the operator
    ticking "everything this tool runs, without asking" both arrive here as
    ``explicitly_yes``, and only the second may clear an act nobody can undo. So the width
    is read at first hand — ``"tool"`` is the operator's own word, given under copy that
    says what it costs, and it is the single thing that moves the risk axis. A
    ``"command"`` grant does not: it names one act, and the operator agreeing to an act is
    not them agreeing that the act is recoverable.

    Unrecoverability is otherwise still absolute. With no whole-tool grant this branch
    parks exactly as it always has, which is what keeps a tool nobody granted — and every
    installation where the operator never ticks the wider box — on the old rule.
    """
    match verdict.risk:
        case "too_destructive":
            return Decision.ALLOW if granted == "tool" else Decision.ASK
        case "low":
            return Decision.ASK if authorization == "explicitly_no" else Decision.ALLOW
        case "high":
            return Decision.ALLOW if authorization == "explicitly_yes" else Decision.ASK
        case _:
            return Decision.ASK


def _authorization(verdict: ReviewVerdict, *, granted: GrantWidth) -> Authorization:
    """The authorization the arithmetic runs on: the reviewer's, with a standing grant
    standing in for it as far as the operator said it should.

    **A command-scoped grant fills a gap; it does not overrule a refusal.**
    ``explicitly_no`` is the reviewer's reading of what the operator said in *this* turn,
    and a grant naming one act, recorded earlier in the thread, is older than that by
    construction — treating it as the answer produced a row that reported the operator had
    said no and allowed the call in the same sentence. ``neutral`` is the case that grant
    was recorded for: the thread contains no word about this act, and the operator has
    already said yes to it once.

    **A whole-tool grant is the answer, and replaces the reviewer's reading of it.** That
    width is only ever written because the operator picked it under copy that says what it
    does — everything this tool runs, for this thread, without being asked — so a reviewer
    inferring a refusal from the prose of the turn is not a more recent word than the one
    they typed into the card.

    What this function produces is only ever the authorization axis. The whole-tool width
    also reaches the *risk* axis, and it does so in :func:`_verdict_decision` from the
    width at first hand rather than through the ``explicitly_yes`` this returns — because
    a reviewer that inferred an explicit yes from the turn must not clear an unrecoverable
    act, and once the two are collapsed into one word that distinction is gone.
    """
    if granted == "tool":
        return "explicitly_yes"
    if granted == "command" and verdict.authorization == "neutral":
        return "explicitly_yes"
    return verdict.authorization


def _verdict_reason(
    verdict: ReviewVerdict, *, grant: GrantWidth, unrecoverable: bool = False
) -> str:
    """The row's account of the ruling — the reviewer's two axes as it read them, and the
    grant named separately where one supplied the authorization. Stated apart rather than
    folded together because "the model found you had asked for this" and "you had already
    said yes to this" are different grounds, and only one of them is revocable. Named only
    where it actually did the work (``"none"`` otherwise), so a row never credits a grant
    for an answer the review reached on its own.

    **The two widths are named differently, because they are different sentences.** One
    says the operator had already approved this act; the other says they handed the whole
    tool over for the thread. A row that read the same for both would hide, from the one
    surface that explains a decision, which of the two the operator is actually holding —
    and the wider one is the one they are most likely to want to go and revoke.

    ``unrecoverable`` is the third sentence, and the one this row most owes the operator.
    A whole-tool grant that supplied an authorization and a whole-tool grant that let an
    act nobody can undo run without them are not the same event, and a row that described
    both as "the authorization" would leave the only account of the decision silent about
    the part of it they would most want to find. It is stated in place of that clause
    rather than after it, because on such a row the authorization is no longer what is
    doing the work.
    """
    reason = f"{verdict.risk} risk, authorization {verdict.authorization}"
    if unrecoverable:
        reason += (
            "; this act cannot be undone and ran only because your standing grant "
            "for this whole tool covers it"
        )
    elif grant == "command":
        reason += "; your standing grant for this command is the authorization"
    elif grant == "tool":
        reason += "; your standing grant for this whole tool is the authorization"
    return f"{reason}; {verdict.correctness}" if verdict.correctness else reason
