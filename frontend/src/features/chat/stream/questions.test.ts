import { describe, expect, test } from "bun:test";
import { createRoot, createSignal } from "solid-js";
import { createStore } from "solid-js/store";
import { groupBlocks } from "../blocks";
import type { AssistantBlock, ChatMessage } from "../model";
import { createApprovalOps } from "./approvals";
import { createFolder } from "./fold";

/**
 * A question parks a run the same way an approval does, and is answered in the same
 * place. What is worth pinning is the seam between those two facts: the block is folded
 * into the transcript (so a reconnect's replay rebuilds the dock), but never *rendered*
 * there (so one park is never two surfaces with two submit buttons for a run that
 * resumes once).
 */

function ask(over: Partial<ChatMessage> = {}, ...blocks: AssistantBlock[]) {
  return {
    id: "a1",
    role: "assistant",
    content: "",
    blocks,
    createdAt: "",
    ...over,
  } as ChatMessage;
}

const question = (stale = false): AssistantBlock => ({
  kind: "question",
  id: "q1",
  question: {
    toolCallId: "t1",
    stale,
    questions: [
      {
        question: "Which database?",
        multiSelect: false,
        options: [{ label: "Postgres" }, { label: "SQLite" }],
      },
    ],
  },
});

const approval = (stale = false): AssistantBlock => ({
  kind: "approval",
  id: "ap1",
  approval: {
    toolCallId: "t2",
    name: "shell_run_command",
    args: {},
    summary: "Runs ls",
    stale,
  },
});

/** A turn whose park is the live run's. Every fixture below carries a `runId`, because
 *  that is what the rule now turns on — a fixture relying on `streaming` alone would pass
 *  under the derivation this replaced. */
function ops(messages: ChatMessage[], sending = true) {
  return createRoot((dispose) => {
    const [store] = createStore<ChatMessage[]>(messages);
    const [live] = createSignal(sending);
    const built = createApprovalOps({
      messages: store,
      patchById: () => {},
      sending: live,
      activeRunId: () => "run-1",
      reconcileStaleDecision: async () => {},
    });
    const answer = {
      park: built.park(),
      waiting: built.awaitingKind() !== null,
      kind: built.awaitingKind(),
    };
    dispose();
    return answer;
  });
}

describe("what the dock is handed", () => {
  test("an unanswered question is a park, and the run is waiting", () => {
    const { park, waiting, kind } = ops([
      ask({ streaming: true, runId: "run-1" }, question()),
    ]);
    expect(waiting).toBe(true);
    expect(kind).toBe("question");
    expect(park?.questions).toHaveLength(1);
    expect(park?.questions[0].questions[0].question).toBe("Which database?");
  });

  test("a park holding both kinds hands over both, under one message", () => {
    // The run resumes on one body covering everything, so the dock has to be able to
    // collect both halves before it submits either.
    const { park } = ops([
      ask({ streaming: true, runId: "run-1" }, question(), approval()),
    ]);
    expect(park?.questions).toHaveLength(1);
    expect(park?.approvals).toHaveLength(1);
    expect(park?.messageId).toBe("a1");
  });

  test("a stale park stays up to explain itself, but stops asking for attention", () => {
    // Putting the composer back on a 409 would claim the run had moved on — which is
    // exactly what is not yet known until the refetch lands.
    const { park, waiting } = ops([
      ask({ streaming: true, runId: "run-1" }, question(true)),
    ]);
    expect(park).not.toBeNull();
    expect(park?.stale).toBe(true);
    expect(waiting).toBe(false);
  });

  test("one stale half stales the whole park — it settles as one submission", () => {
    const { park } = ops([
      ask({ streaming: true, runId: "run-1" }, question(), approval(true)),
    ]);
    expect(park?.stale).toBe(true);
  });

  test("nothing is parked once the run is no longer in flight", () => {
    expect(
      ops([ask({ streaming: true, runId: "run-1" }, question())], false).park,
    ).toBeNull();
  });

  test("a question from an earlier run does not answer for the live one", () => {
    const { park } = ops([
      ask({ id: "a0", runId: "run-0" }, question()),
      ask({ id: "a1", streaming: true, runId: "run-1" }),
    ]);
    expect(park).toBeNull();
  });

  test("a steering message splitting the turn does not take the park with it", () => {
    // The reported bug. `message.injected` closes the streaming bubble and opens a
    // fresh one, so the question ends up on a message that is no longer streaming —
    // and the dock, derived from "the last streaming message", vanished mid-answer
    // and handed the composer back over a run that was still parked. The run is the
    // discriminator precisely because the flow can split under it.
    const { park, waiting } = ops([
      ask({ id: "a0", runId: "run-1", streaming: false }, question()),
      {
        id: "u1",
        role: "user",
        content: "steered",
        createdAt: "",
      } as ChatMessage,
      ask({ id: "a1", runId: "run-1", streaming: true }),
    ]);
    expect(park?.messageId).toBe("a0");
    expect(waiting).toBe(true);
  });

  test("a call that parsed to no questions is not answered by an empty list", () => {
    // `answers` is an array, and an empty one is truthy — so a malformed `ask_user`
    // would have retired a park nobody answered, and rendered a card with a heading
    // and nothing under it.
    const empty: AssistantBlock = {
      kind: "question",
      id: "q1",
      question: { toolCallId: "t1", questions: [], answers: [] },
    };
    const { park } = ops([ask({ streaming: true, runId: "run-1" }, empty)]);
    expect(park?.questions).toHaveLength(1);
    expect(groupBlocks([empty])).toEqual([]);
  });

  test("the park follows the run even when the controller's id is stale", () => {
    // `activeRunId` is a field on a plain object, so a memo reading it alone would not
    // re-run when it moved. The live run is read off the messages, which are a store —
    // here the controller still reports the *previous* run and the park must not.
    const park = createRoot((dispose) => {
      const [store] = createStore<ChatMessage[]>([
        ask({ id: "a1", runId: "run-2", streaming: true }, question()),
      ]);
      const built = createApprovalOps({
        messages: store,
        patchById: () => {},
        sending: () => true,
        activeRunId: () => "run-1",
        reconcileStaleDecision: async () => {},
      });
      const answer = built.park();
      dispose();
      return answer;
    });
    expect(park?.messageId).toBe("a1");
  });

  test("an answered question is a transcript row, not a park", () => {
    // It stays in `blocks` so the exchange can be rendered; what it stops being is
    // something the dock has to collect.
    const answered: AssistantBlock = {
      kind: "question",
      id: "q1",
      question: {
        toolCallId: "t1",
        questions: [],
        answers: [{ question: "Which database?", selections: ["Postgres"] }],
      },
    };
    const { park, waiting } = ops([
      ask({ streaming: true, runId: "run-1" }, answered),
    ]);
    expect(park).toBeNull();
    expect(waiting).toBe(false);
  });
});

describe("the transcript's side of the seam", () => {
  test("a waiting park is folded but never grouped for rendering", () => {
    // Both halves matter: dropping them from the fold would lose the dock on a
    // reconnect's replay, and grouping them would draw the park twice.
    const groups = groupBlocks([
      { kind: "text", id: "t", text: "hello" },
      question(),
      approval(),
    ]);
    expect(groups.map((g) => g.kind)).toEqual(["text"]);
  });

  test("an answered question is rendered, and its approval neighbour still is not", () => {
    // Docking is a phase, not a kind. Once answered, the question is the one thing the
    // transcript most owes the operator — what they were asked and what they said —
    // and it was previously reachable only as prose inside a collapsed work log. An
    // approval has no such second life: the call that ran is its outcome.
    const answered: AssistantBlock = {
      kind: "question",
      id: "q1",
      question: {
        toolCallId: "t1",
        questions: [],
        answers: [{ question: "Which database?", selections: ["Postgres"] }],
      },
    };
    const groups = groupBlocks([
      { kind: "text", id: "t", text: "hello" },
      answered,
      approval(),
    ]);
    expect(groups.map((g) => g.kind)).toEqual(["text", "question"]);
  });
});

describe("folding question.asked", () => {
  function fold(...events: Record<string, unknown>[]) {
    const message = ask({ streaming: true });
    const foldEvent = createFolder({
      state: {
        maxFoldedSeq: 0,
        foldTarget: null,
        tasksRevision: 0,
        planRevision: 0,
        activeRunId: null,
        runKind: null,
        epoch: 0,
        replayThrough: 0,
      },
      patchById: (_id, mutate) => mutate(message),
      setMessages: (() => {}) as never,
      setSnapshots: () => {},
      setTasks: () => {},
      setPlan: () => {},
      setPermission: () => {},
      setUsage: () => {},
      setStats: () => {},
      setErrored: () => {},
      setTitlePending: () => {},
    });
    events.forEach((event, i) =>
      foldEvent("a1", { seq: i + 1, ...event } as never),
    );
    return message;
  }

  test("questions arrive shaped for the panel, with the wire's snake_case gone", () => {
    const folded = fold({
      type: "question.asked",
      tool_call_id: "t9",
      questions: [
        {
          question: "Which extras?",
          multi_select: true,
          options: [{ label: "Auth", description: "Sign-in" }],
        },
      ],
    });
    const block = folded.blocks?.[0];
    expect(block?.kind).toBe("question");
    if (block?.kind !== "question") throw new Error("not a question block");
    expect(block.question.toolCallId).toBe("t9");
    expect(block.question.questions[0].multiSelect).toBe(true);
    expect(block.question.questions[0].options[0].description).toBe("Sign-in");
  });

  test("an answered question stops being a park when its result replays", () => {
    // Re-entering the room replays from seq 0, where the result is the only thing that
    // knows the question was answered — otherwise the dock reopens over a live run.
    const folded = fold(
      { type: "question.asked", tool_call_id: "t9", questions: [] },
      {
        type: "tool.completed",
        tool_call_id: "t9",
        name: "builtin_ask_user",
        result: "Q: Which database?\nA: Postgres",
      },
    );
    expect(folded.blocks?.some((b) => b.kind === "question")).toBe(false);
  });

  test("a decided approval stops being a park too, whether it ran or was denied", () => {
    // A denial comes back as the call's result, not as a failure — both paths retire it.
    const settled = (result: Record<string, unknown>) =>
      fold(
        {
          type: "approval.required",
          tool_call_id: "t2",
          name: "mail_send_email",
          summary: "Sends mail",
          args: {},
        },
        { tool_call_id: "t2", name: "mail_send_email", ...result },
      );
    for (const outcome of [
      { type: "tool.completed", result: "sent" },
      { type: "tool.completed", result: "The operator denied this." },
      { type: "tool.failed", error: "boom" },
    ])
      expect(settled(outcome).blocks?.some((b) => b.kind === "approval")).toBe(
        false,
      );
  });

  test("one settled call does not retire the park beside it", () => {
    // An approval can grant and run while the question beside it is still waiting.
    const folded = fold(
      { type: "question.asked", tool_call_id: "t9", questions: [] },
      {
        type: "approval.required",
        tool_call_id: "t2",
        name: "mail_send_email",
        summary: "Sends mail",
        args: {},
      },
      { type: "tool.completed", tool_call_id: "t2", name: "mail_send_email" },
    );
    expect(folded.blocks?.some((b) => b.kind === "approval")).toBe(false);
    expect(folded.blocks?.some((b) => b.kind === "question")).toBe(true);
  });

  test("a call that arrives with nothing in it still folds", () => {
    // The payload is untrusted JSON; a missing list must default in the mapper rather
    // than throw somewhere downstream that has no idea why it is empty.
    const folded = fold({ type: "question.asked", tool_call_id: "t9" });
    const block = folded.blocks?.[0];
    if (block?.kind !== "question") throw new Error("not a question block");
    expect(block.question.questions).toEqual([]);
  });
});
