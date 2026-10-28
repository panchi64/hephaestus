/**
 * Settling the calls a run parked on — the permissions it needs and the questions it
 * asked.
 *
 * A parked run is waiting on the operator, and it resumes only when one body covers
 * *every* call it stopped for — which is why nothing here submits one call at a time, and
 * why approvals and answers go in the same POST rather than in two. A turn can stop for
 * both reasons at once, and it resumes once; two submissions would mean the second
 * arriving at a run that had already moved on.
 *
 * Each surface (the dock over the composer, the host-command terminal) gathers its whole
 * set and posts it as a batch; the open stream carries the results back, and the
 * optimistic patch exists only so the dock clears on the click rather than a round trip
 * later.
 *
 * **A decision can be lost, and losing one is not a failure to retry.** The same run can
 * be decided from a second tab, or by a retried request that lands after the run has
 * already resumed — the backend answers 409, and resubmitting would 409 forever. So a
 * lost race marks the cards *stale* (visible, inert, explained) and refetches the thread,
 * while an ordinary transport failure leaves them interactive. Those two error paths
 * looking alike is exactly what makes them worth stating apart.
 *
 * **Whether anything is parked is read off the blocks, not tracked beside them.** The
 * transcript already knows; a second flag kept in step with it would be a second answer to
 * the same question, and the one that goes stale is always the flag.
 */

import { createMemo, createSignal, type Accessor } from "solid-js";
import { api, isApiError } from "~/lib/api";
import { toast, type ActionTone } from "~/ui";
import { awaitingKindOf } from "./awaitingKind";
import { bumpGrantsRevision } from "../data/conversations";
import type { ApprovalOutcomeDTO } from "../data/wire";
import { isAnswered } from "../model";
import type {
  Approval,
  ApprovalDecision,
  ChatMessage,
  HostCommandBlock,
  QuestionAnswer,
  QuestionBlock,
  QuestionReply,
} from "../model";
import type { PatchById } from "./patch";

export interface ApprovalDeps {
  messages: ChatMessage[];
  patchById: PatchById;
  /** Whether a turn is in flight — a park is by definition the live turn waiting. */
  sending: () => boolean;
  /** The run the room is currently attached to. What decides whether a parked call is
   *  *this* turn's — see `park` for why the streaming flag could not. */
  activeRunId: () => string | null;
  /** Reconcile with whatever the winning decision did, after this one lost the race. */
  reconcileStaleDecision: () => Promise<void>;
}

/** The tool whose approval is a submitted plan. Answered in the dock like every other
 *  approval; the Plan panel beside it is where the document is *read*. Named here because
 *  three surfaces ask "is this one the plan?" — the dock, to offer Request changes instead
 *  of a bare Deny; the approval card, to show the plan's title rather than a dump of its
 *  own body; and the park below, to mark it. */
export const PLAN_SUBMIT_TOOL = "plan_submit";

/** What the live turn is parked on, for the dock that takes over the composer. `null`
 *  when nothing is — which is what puts the composer back. */
export interface Park {
  /** The message the park belongs to; every submit is addressed to its run. */
  messageId: string;
  approvals: Approval[];
  /** The submitted plan in this park, when it holds one — **a marker, not a split**. The
   *  same object is in `approvals` above, and the dock decides it there with everything
   *  else, because the run resumes on one body covering every parked call.
   *
   *  It used to be held apart, for a panel that answered it on its own. That put the
   *  decision at the far end of the window from the composer every other approval takes
   *  over, made the panel's arrival load-bearing (a host without one could only Stop), and
   *  meant a park could in principle be two submissions, each naming half the batch and
   *  each refused for not covering the rest. The panel now only *renders* the plan; this
   *  field is what tells the dock that one of the calls it is holding is a document —
   *  worth offering Request changes over, and not worth a standing grant. */
  planApproval: Approval | null;
  questions: QuestionBlock["question"][];
  /** True once a submitted decision for this park 409'd. The dock stays up, inert and
   *  explained, until the refetch reconciles — putting the composer back on a 409 would
   *  suggest the run had moved on, which is exactly what nobody yet knows. */
  stale: boolean;
}

/**
 * What the operator has said to the park so far, before they submit it.
 *
 * **Held by the controller, not by the dock**, and that is the whole point: the dock is
 * mounted by a `Show` over `park()`, so anything kept in its own signals is destroyed by
 * any re-render that momentarily drops the park — and the run being steered mid-park does
 * exactly that. Several minutes of reading and half a written answer went with it, with
 * nothing on screen to say why. The controller outlives every remount of the dock.
 *
 * It is unsent input — the same class of thing as a composer draft — so keeping it here
 * does not make the client the authority on anything: the backend re-validates the body
 * and is what settles the park.
 */
export interface ParkDraft {
  /** One per parked approval, as the cards have collected them. */
  decisions: ApprovalDecision[];
  /** Whether every approval in the park has a verdict — the cards' own answer, which
   *  they know and a count here would only approximate. */
  allDecided: boolean;
  /** Replies per parked `ask_user` call, positional within the call. */
  replies: Record<string, QuestionReply[]>;
  /** Which page of the dock the operator is on. Part of the draft rather than the
   *  dock's own state for the same reason the answers are: losing their place in a
   *  five-question park is losing the same work in a smaller way. */
  page: number;
}

const emptyDraft = (): ParkDraft => ({
  decisions: [],
  allDecided: false,
  replies: {},
  page: 0,
});

export interface ApprovalOps {
  /** The kind of the room's live, unanswered park, or null. */
  awaitingKind: Accessor<ActionTone | null>;
  /** What the operator has entered into the dock but not yet submitted. */
  parkDraft: Accessor<ParkDraft>;
  patchParkDraft: (next: Partial<ParkDraft>) => void;
  /** Throw away what was entered — on a stop, and once a park has settled. */
  clearParkDraft: () => void;
  /** What the live turn is parked on, or `null`. The dock renders this. */
  park: Accessor<Park | null>;
  /** Settle everything a park is waiting on — decisions and answers in one call,
   *  because the run resumes on one body covering all of it. The dock clears once
   *  submitted. */
  resolvePark: (
    messageId: string,
    settlement: {
      decisions?: ApprovalDecision[];
      answers?: QuestionAnswer[];
    },
  ) => Promise<void>;
  /** Decide a message's host-command approvals. Approved commands begin running
   *  and denied ones close out optimistically; the stream confirms the outcome. */
  resolveHostCommands: (
    messageId: string,
    decisions: ApprovalDecision[],
  ) => Promise<void>;
}

/**
 * Which message holds the live turn's park.
 *
 * It used to be "the last message still streaming", and that is wrong for one reason
 * nothing on screen explains: **a turn can be split into two assistant messages while it
 * is parked.** `message.injected` closes the current bubble and opens a fresh one, so the
 * blocks the operator is answering end up on a message that is no longer streaming, the
 * park reads as gone, the dock unmounts with their half-written answers in it, and the
 * composer comes back — over a run that is still waiting. Anything then typed there
 * queues into that same parked run, so the message takes the question's place.
 *
 * The run is the honest discriminator. A parked call belongs to the turn whose run is
 * still attached, wherever the flow has since put it; a call on an *earlier* run is an
 * old turn's and answers for nobody. A message still streaming or detached counts even
 * with no run id, which is the moment before the first event lands.
 */
const inLiveRun =
  (runId: string | null) =>
  (m: ChatMessage): boolean =>
    m.runId !== undefined
      ? m.runId === runId
      : Boolean(m.streaming || m.detached);

/**
 * The run the transcript is attached to, **read reactively**.
 *
 * The controller's own `activeRunId` is a field on a plain object, not a signal — so a
 * memo that read it alone would not re-run when it changed, and would go on answering
 * from whichever run happened to be live the last time the messages moved. The messages
 * carry the same fact and are a store: the turn in flight is the last message still
 * streaming or detached, and its `runId` is the run. The controller's value is the
 * fallback for the one moment the messages cannot answer — the placeholder pushed before
 * `run.started` has patched an id onto it.
 */
const liveRunIdOf = (deps: ApprovalDeps): string | null =>
  deps.messages.findLast((m) => m.streaming || m.detached)?.runId ??
  deps.activeRunId();

const parkedIn =
  (runId: string | null) =>
  (m: ChatMessage): boolean =>
    inLiveRun(runId)(m) &&
    Boolean(
      m.blocks?.some((b) => b.kind === "approval" || b.kind === "question"),
    );

export function createApprovalOps(deps: ApprovalDeps): ApprovalOps {
  const [draft, setDraft] = createSignal<ParkDraft>(emptyDraft());
  const patchParkDraft = (next: Partial<ParkDraft>): void => {
    setDraft((current) => ({ ...current, ...next }));
  };
  const clearParkDraft = (): void => {
    setDraft(emptyDraft());
  };

  // What the live turn stopped on, read straight off its blocks — folded there by
  // `approval.required` and `question.asked`. A derived memo rather than its own
  // set/clear pair: the blocks are already the single source of truth for "is something
  // still pending", and a flag kept beside them is the one that goes stale.
  //
  // Gated on `sending()` so it clears the moment the run stops being in flight, whether
  // by resolution (the block is filtered out of `messages` on submit — see
  // `resolvePark`), a cancel, or the run ending.
  //
  // Scoped to the turn in flight, and not out of tidiness: a park is by definition the
  // *live* turn waiting, since a turn cannot end with a call still undecided — so every
  // earlier turn in the transcript is a message × block walk that can only ever answer
  // no, re-run on every block the run pushes. A detached turn counts as live for the
  // same reason `sending` stays true through one: the run may still be parked
  // server-side.
  const park = createMemo<Park | null>(() => {
    if (!deps.sending()) return null;
    const live = deps.messages.findLast(parkedIn(liveRunIdOf(deps)));
    if (!live) return null;
    const approvals: Approval[] = [];
    const questions: QuestionBlock["question"][] = [];
    for (const b of live.blocks ?? []) {
      if (b.kind === "approval") approvals.push(b.approval);
      // An answered question is a row in the transcript, not something the dock is
      // still collecting — see `clearPark`. Length, not presence: an `ask_user` whose
      // arguments parsed to no questions at all answers to an empty list, and an empty
      // list is not an answer.
      else if (b.kind === "question" && !isAnswered(b.question))
        questions.push(b.question);
    }
    if (!approvals.length && !questions.length) return null;
    // Marked, not removed — see `Park.planApproval`. In practice a plan is the whole park
    // (plan mode withholds every other tool that could defer), but nothing here depends on
    // that: the dock decides `approvals` as one batch whatever it holds.
    const plan = approvals.find((a) => a.name === PLAN_SUBMIT_TOOL) ?? null;
    return {
      messageId: live.id,
      approvals,
      planApproval: plan,
      questions,
      // One flag for the park, not one per call: the whole batch resumes on one
      // submission, so a 409 stales all of it at once. Read off `approvals` before the
      // split, which is every call either way.
      stale: approvals.some((a) => a.stale) || questions.some((q) => q.stale),
    };
  });

  // The host-command terminal keeps its own pending phase on the rail — it is a running
  // terminal, not a prompt, and only its first moment is a decision. It still counts as
  // the run waiting on the operator.
  const awaitingKind = createMemo(() => {
    // A *stale* park doesn't need them: the run already resumed elsewhere, so the
    // attention echo should clear even though the dock stays up to say so.
    const p = park();
    if (p || !deps.sending()) return awaitingKindOf(p, false);
    // Keyed to the run for the same reason the park is: a terminal awaiting its first
    // decision does not stop waiting because a steering message split the flow under it.
    const runId = liveRunIdOf(deps);
    return awaitingKindOf(
      null,
      deps.messages.some(
        (m) =>
          inLiveRun(runId)(m) &&
          (m.blocks?.some(
            (b) => b.kind === "host_command" && b.command.phase === "pending",
          ) ??
            false),
      ),
    );
  });

  /** POST everything a message's parked run is waiting on, then apply an optimistic
   *  patch. The open run stream resumes with the results — the parked run requires a
   *  body covering *every* pending call, approvals and questions alike, which is why
   *  each surface batches its whole set into one POST. */
  async function submitDecisions(
    messageId: string,
    body: { decisions?: ApprovalDecision[]; answers?: QuestionAnswer[] },
    optimistic: (m: ChatMessage) => void,
  ): Promise<void> {
    const msg = deps.messages.find((m) => m.id === messageId);
    if (!msg?.runId) return;
    const decisions = body.decisions ?? [];
    try {
      const outcome = await api.post<ApprovalOutcomeDTO>(
        `/runs/${msg.runId}/approve`,
        { decisions, answers: body.answers ?? [] },
      );
      deps.patchById(messageId, optimistic);
      // A recorded conversation grant must show on the strip now, not on the next
      // stream toggle — nudge the grants resource to refetch. Tested as "anything but
      // once", not against the standing scopes by name: a third width was added and a
      // check listing them one by one is a check that silently stops covering the newest
      // one, which is the width most worth seeing a chip for.
      if (decisions.some((d) => d.scope && d.scope !== "once")) {
        bumpGrantsRevision();
        // ...and say so when the backend recorded nothing. It refuses a standing yes to a
        // command no scope could stand for — one it cannot read, or one reaching outside
        // the worktree — which is the right call and the wrong thing to do in silence: the
        // operator ticked a box, and the only other evidence is a chip that never appears.
        const refused = outcome?.unscoped?.length ?? 0;
        if (refused > 0) {
          toast.info(
            refused === 1
              ? "Approved. This command can't be auto-approved on its own, so the agent will ask again."
              : `Approved. ${refused} of these commands can't be auto-approved on their own, so the agent will ask again.`,
          );
        }
      }
    } catch (err) {
      if (isApiError(err) && err.status === 409) {
        // The decision was already made elsewhere (a second tab, a retried
        // request that landed after the run resumed) — resubmitting would just
        // 409 forever. Mark the pending cards stale (non-interactive, with a
        // note) instead of leaving them re-clickable, then refetch so the
        // transcript catches up to whatever actually happened.
        deps.patchById(messageId, (m) => {
          for (const b of m.blocks ?? []) {
            if (b.kind === "approval") b.approval.stale = true;
            else if (b.kind === "question") b.question.stale = true;
            else if (b.kind === "host_command" && b.command.phase === "pending")
              b.command.phase = "stale";
          }
        });
        toast.error(`This was already answered elsewhere: ${err.detail}`);
        deps.reconcileStaleDecision().catch(() => {
          toast.error(
            "Couldn't reload this chat. Switch away and back to refresh it.",
          );
        });
        return;
      }
      // A transient failure (network blip, 5xx): the decision may not have
      // landed at all, so keep the card interactive and let the operator retry.
      toast.error(
        (err as { detail?: string })?.detail ??
          "Unable to submit the decision.",
      );
    }
  }

  return {
    awaitingKind,
    park,
    parkDraft: draft,
    patchParkDraft,
    clearParkDraft,
    resolvePark: (messageId, settlement) =>
      submitDecisions(messageId, settlement, (m) => {
        // One submission settled the whole park, so nothing may be left behind that
        // would keep the dock up over a run that has already resumed. An approval goes;
        // a question *stays*, now carrying what was said, because that is the row the
        // transcript shows. The optimistic answers are the labels the operator was
        // offered — the same words the backend will pair its own reply against — so
        // `question.answered` lands on a card that already reads correctly.
        const answered = new Map(
          (settlement.answers ?? []).map((a) => [a.tool_call_id, a.replies]),
        );
        for (const b of m.blocks ?? [])
          if (b.kind === "question" && !isAnswered(b.question)) {
            const replies = answered.get(b.question.toolCallId);
            // Only where there is something to show: a call that parsed to no questions
            // would otherwise be kept as a card with a heading and nothing under it.
            if (replies && b.question.questions.length > 0)
              b.question.answers = b.question.questions.map((q, i) => ({
                question: q.question,
                selections: replies[i]?.selections ?? [],
                text: replies[i]?.text,
              }));
          }
        if (m.blocks)
          m.blocks = m.blocks.filter(
            (b) =>
              b.kind !== "approval" &&
              !(b.kind === "question" && !isAnswered(b.question)),
          );
        clearParkDraft();
      }),
    resolveHostCommands: (messageId, decisions) =>
      submitDecisions(messageId, { decisions }, (m) => {
        for (const d of decisions) {
          const b = m.blocks?.find(
            (x): x is HostCommandBlock =>
              x.kind === "host_command" &&
              x.command.toolCallId === d.tool_call_id,
          );
          if (b) b.command.phase = d.approved ? "running" : "denied";
        }
      }),
  };
}
