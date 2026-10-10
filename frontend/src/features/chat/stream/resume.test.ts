import { describe, expect, test } from "bun:test";
import type { ConversationDetailDTO } from "../data/wire";
import type { ChatMessage } from "../model";
import { createResumeOps, type ResumeDeps } from "./resume";

/**
 * The stale-thread guard, which is the whole reason these reconcilers live in one file.
 *
 * Each of them has an `await` in the middle, and the store on the other side of it
 * belongs to whichever thread is open *then*. Reseating one thread's history over
 * another's replaces the transcript of a turn that is still streaming and freezes it —
 * so "the operator switched threads while the read was in flight" is the case that has
 * to be written down, and it was the one written three different ways and forgotten in
 * the fourth.
 *
 * `fetchDetail` is a dependency, so the read resolves exactly when this says it does.
 */

function detailFor(id: string): ConversationDetailDTO {
  return {
    id,
    messages: [],
    active_run: null,
  } as unknown as ConversationDetailDTO;
}

function harness(
  options: {
    openAfterFetch?: string | null;
    messages?: ChatMessage[];
    detail?: (id: string) => ConversationDetailDTO;
  } = {},
) {
  let open: string | null = "A";
  const reseated: string[] = [];
  const messagesReseated: string[] = [];
  const reattached: string[] = [];
  const messages = options.messages ?? ([] as ChatMessage[]);
  const deps: ResumeDeps = {
    conversationId: () => open,
    fetchDetail: async (id) => {
      // The operator moves while the request is in flight.
      if (options.openAfterFetch !== undefined) open = options.openAfterFetch;
      return (options.detail ?? detailFor)(id);
    },
    messages,
    reseat: (d) => reseated.push(d.id),
    reseatMessages: (d) => messagesReseated.push(d.id),
    reattachRun: async (runId) => {
      reattached.push(runId);
    },
  };
  return { ops: createResumeOps(deps), reseated, messagesReseated, reattached };
}

describe("an answer about a thread the operator has left is dropped", () => {
  test("the lost-run recovery does not reseat over the new thread", async () => {
    // This is the one that had no guard at all: `reattachRun`'s fallback read the
    // detail and reseated with no second look at which thread was open.
    const h = harness({ openAfterFetch: "B" });
    await h.ops.recoverLostRun();
    expect(h.reseated).toEqual([]);
  });

  test("a stale decision does not reseat over the new thread", async () => {
    const h = harness({ openAfterFetch: "B" });
    await h.ops.reconcileStaleDecision();
    expect(h.reseated).toEqual([]);
    expect(h.reattached).toEqual([]);
  });

  test("a 409 re-attach does not contaminate the new thread with the old run", async () => {
    const h = harness({ openAfterFetch: "B" });
    await h.ops.reattachToLiveRun("A");
    expect(h.reattached).toEqual([]);
  });

  test("and the reseat at a run's end does not either", async () => {
    const h = harness({ openAfterFetch: "B" });
    await h.ops.reseatAfterRun();
    expect(h.messagesReseated).toEqual([]);
  });

  test("nor does the read that finds a run's prompt row", async () => {
    const h = harness({ openAfterFetch: "B" });
    expect(await h.ops.refetchRun("r1")).toBeNull();
    expect(h.reseated).toEqual([]);
  });
});

describe("staying on the thread lets the answer through", () => {
  test("the lost-run recovery reseats", async () => {
    const h = harness();
    await h.ops.recoverLostRun();
    expect(h.reseated).toEqual(["A"]);
  });

  test("a stale decision reseats", async () => {
    const h = harness();
    await h.ops.reconcileStaleDecision();
    expect(h.reseated).toEqual(["A"]);
  });
});

describe("a run's end takes the backend's rows", () => {
  test("the transcript is reseated from the detail, not patched by position", async () => {
    // The optimistic transcript holds a client-id echo and a streamed bubble; the
    // backend's rows are what replace them. Nothing is matched up by index.
    const h = harness({
      messages: [{ id: "u-1" }, { id: "a-1" }] as ChatMessage[],
    });
    await h.ops.reseatAfterRun();
    expect(h.messagesReseated).toEqual(["A"]);
    expect(h.reseated).toEqual([]);
  });

  test("a cancel that left only the prompt reseats too", async () => {
    // The backend keeps the operator's prompt when a run is cancelled before it answered,
    // so there is no 'persisted nothing' case left to protect the optimistic store from.
    const h = harness({
      messages: [{ id: "u-1" }, { id: "a-1" }] as ChatMessage[],
      detail: (id) =>
        ({
          id,
          messages: [{ id: "m1", role: "user", content: "hi" }],
          active_run: null,
        }) as unknown as ConversationDetailDTO,
    });
    await h.ops.reseatAfterRun();
    expect(h.messagesReseated).toEqual(["A"]);
  });

  test("a turn started while the read was out is left to reseat itself", async () => {
    // The composer re-enables the instant streaming stops; reseating now would write
    // over the new turn's optimistic messages and freeze its stream.
    const messages = [{ id: "u-1" }] as ChatMessage[];
    const h = harness({
      messages,
      detail: (id) => {
        messages.push(
          { id: "u-2" } as ChatMessage,
          { id: "a-2" } as ChatMessage,
        );
        return detailFor(id);
      },
    });
    await h.ops.reseatAfterRun();
    expect(h.messagesReseated).toEqual([]);
  });
});

describe("finding the run that is answering", () => {
  const withRun = (id: string, prompt: string | null) => (thread: string) =>
    ({
      id: thread,
      messages: [],
      active_run: {
        id,
        kind: "chat",
        status: "running",
        last_seq: 4,
        prompt_message_id: prompt,
      },
    }) as unknown as ConversationDetailDTO;

  test("it reseats and hands back the run with the prompt row it names", async () => {
    const h = harness({ detail: withRun("r1", "p1") });
    const found = await h.ops.refetchRun("r1");
    expect(h.reseated).toEqual(["A"]);
    expect(found?.promptMessageId).toBe("p1");
  });

  test("a different run is not the one asked about", async () => {
    const h = harness({ detail: withRun("r2", "p2") });
    expect(await h.ops.refetchRun("r1")).toBeNull();
  });
});

test("a re-attach says what kind of run it is attaching to, and what it answers", async () => {
  // A fold has no assistant turn, and only the kind lets the drive know not to seed
  // one — a reload mid-fold otherwise draws an empty streaming bubble above it. A chat
  // run names the persisted row its answer belongs after.
  const seen: { kind?: string; prompt?: string | null }[] = [];
  const ops = createResumeOps({
    conversationId: () => "A",
    fetchDetail: async () =>
      ({
        id: "A",
        messages: [],
        active_run: {
          id: "r1",
          kind: "compaction",
          status: "running",
          last_seq: 3,
          prompt_message_id: "p9",
        },
      }) as unknown as ConversationDetailDTO,
    messages: [],
    reseat: () => {},
    reseatMessages: () => {},
    reattachRun: async (_runId, opts) => {
      seen.push({ kind: opts.kind, prompt: opts.promptMessageId });
    },
  });
  await ops.reattachToLiveRun("A");
  expect(seen).toEqual([{ kind: "compaction", prompt: "p9" }]);
});

test("an unsaved thread has nothing to read", async () => {
  let fetched = false;
  const ops = createResumeOps({
    conversationId: () => null,
    fetchDetail: async () => {
      fetched = true;
      return detailFor("nope");
    },
    messages: [],
    reseat: () => {},
    reseatMessages: () => {},
    reattachRun: async () => {},
  });
  await ops.recoverLostRun();
  await ops.reseatAfterRun();
  expect(fetched).toBe(false);
});
