import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { api } from "~/lib/api";
import type { InboxDTO } from "../data/wire";
import { foldInboxEvent, type InboxState } from "../inbox";
import { createSteeringOps } from "./steering";

/**
 * The inbox ops relay to the conversation's endpoints and change the client's copy only
 * after the backend accepts, since an edit or withdraw races the run's next boundary.
 */

const dto = (ids: string[], waiting = false): InboxDTO => ({
  messages: ids.map((id) => ({
    id,
    text: `text ${id}`,
    source: "operator",
    sender: null,
    held: false,
    turn: false,
    notice: null,
    queued_at: "t",
  })),
  waiting_for_operator: waiting,
  agents_paused: false,
  run_id: null,
});

function harness(initial: string | null = "A") {
  let conversationId: string | null = initial;
  const released: boolean[] = [];
  const ops = createSteeringOps({
    conversationId: () => conversationId,
    afterRelease: (due) => released.push(due),
  });
  return {
    ops,
    released,
    switchTo: (id: string | null) => {
      conversationId = id;
    },
    seed: (ids: string[]) => {
      let s: InboxState = ops.inbox();
      for (const id of ids)
        s = foldInboxEvent(s, {
          type: "message.queued",
          seq: 1,
          ts: "t",
          message_id: id,
          text: `text ${id}`,
        });
      ops.setInbox(s);
    },
    ids: () => ops.inbox().items.map((i) => i.id),
  };
}

const spies: { mockRestore: () => void }[] = [];
afterEach(() => {
  while (spies.length) spies.pop()?.mockRestore();
});
const spy = <T extends { mockRestore: () => void }>(s: T): T => {
  spies.push(s);
  return s;
};

describe("reading the inbox", () => {
  test("refresh replaces the client's copy with the backend's", async () => {
    const h = harness();
    h.seed(["stale"]);
    spy(spyOn(api, "get")).mockResolvedValue(dto(["a", "b"], true));
    await h.ops.refresh();
    expect(h.ids()).toEqual(["a", "b"]);
    expect(h.ops.inbox().waiting).toBe(true);
  });

  test("a read that lands after the thread changed is dropped", async () => {
    const h = harness();
    spy(spyOn(api, "get")).mockResolvedValue(dto(["a"]));
    const read = h.ops.refresh();
    h.switchTo("B");
    await read;
    expect(h.ids()).toEqual([]);
  });

  test("resume adopts what the backend returns", async () => {
    const h = harness();
    h.seed(["a"]);
    const post = spy(spyOn(api, "post")).mockResolvedValue(dto(["a"], false));
    await h.ops.resume();
    expect(post).toHaveBeenCalledWith("/conversations/A/inbox/resume");
    expect(h.ops.inbox().waiting).toBe(false);
    // The resume started a turn this client did not submit; it goes looking for it.
    expect(h.released).toEqual([true]);
  });

  test("settle reads the inbox once its turn has started, with that run", async () => {
    const h = harness();
    const post = spy(spyOn(api, "post")).mockResolvedValue({
      ...dto([]),
      run_id: "r2",
    });
    const read = await h.ops.settle();
    expect(post).toHaveBeenCalledWith("/conversations/A/inbox/settle");
    expect(read?.run_id).toBe("r2");
  });

  test("settle on a thread the room has left reads nothing", async () => {
    const h = harness();
    spy(spyOn(api, "post")).mockResolvedValue(dto(["a"]));
    const read = h.ops.settle();
    h.switchTo("B");
    expect(await read).toBeNull();
    expect(h.ids()).toEqual([]);
  });
});

describe("acting on a queued message", () => {
  test("withdraw drops the item only once the backend accepted", async () => {
    const h = harness();
    h.seed(["a", "b"]);
    const del = spy(spyOn(api, "del")).mockResolvedValue(undefined);
    await h.ops.withdraw("a");
    expect(del).toHaveBeenCalledWith("/conversations/A/inbox/a");
    expect(h.ids()).toEqual(["b"]);
    expect(h.released).toEqual([false]);
  });

  test("a withdraw the backend refuses keeps the item", async () => {
    const h = harness();
    h.seed(["a"]);
    spy(spyOn(api, "del")).mockRejectedValue({ status: 403, detail: "no" });
    spy(spyOn(api, "get")).mockResolvedValue(dto(["a"]));
    await h.ops.withdraw("a");
    expect(h.ids()).toEqual(["a"]);
  });

  test("edit rewrites the text in place", async () => {
    const h = harness();
    h.seed(["a"]);
    const patch = spy(spyOn(api, "patch")).mockResolvedValue(undefined);
    await h.ops.edit("a", "new words");
    expect(patch).toHaveBeenCalledWith("/conversations/A/inbox/a", {
      text: "new words",
    });
    expect(h.ops.inbox().items[0]?.text).toBe("new words");
  });

  test("an edit that lost the race hands the typing back to the composer", async () => {
    const h = harness();
    h.seed(["a"]);
    spy(spyOn(api, "patch")).mockRejectedValue({ status: 404, detail: "gone" });
    spy(spyOn(api, "get")).mockResolvedValue(dto([]));
    await h.ops.edit("a", "my rewrite");
    expect(h.ops.undeliveredDraft()).toBe("my rewrite");
  });

  test("hold marks the item held", async () => {
    const h = harness();
    h.seed(["a"]);
    const post = spy(spyOn(api, "post")).mockResolvedValue(undefined);
    await h.ops.hold("a", true);
    expect(post).toHaveBeenCalledWith("/conversations/A/inbox/a/hold", {
      held: true,
    });
    expect(h.ops.inbox().items[0]?.held).toBe(true);
  });
});

describe("text the operator typed is never dropped", () => {
  test("stash appends rather than overwrites, and clearing consumes it", () => {
    const h = harness();
    h.ops.stash("one");
    h.ops.stash("two");
    expect(h.ops.undeliveredDraft()).toBe("one\ntwo");
    h.ops.clearUndeliveredDraft();
    expect(h.ops.undeliveredDraft()).toBeNull();
  });

  test("an unsaved thread has no inbox to act on", async () => {
    const h = harness(null);
    const del = spy(spyOn(api, "del"));
    await h.ops.withdraw("a");
    expect(del).not.toHaveBeenCalled();
  });
});
