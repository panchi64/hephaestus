import { describe, expect, test } from "bun:test";
import type { RunEvent } from "~/lib/stream";
import type { InboxDTO } from "./data/wire";
import {
  deliveryDue,
  emptyInbox,
  foldInboxEvent,
  idleReason,
  inboxFromDto,
  isOperatorItem,
  waitingLabel,
  type InboxState,
} from "./inbox";

let seq = 0;
const ev = (e: Record<string, unknown>): RunEvent =>
  ({ seq: ++seq, ts: "t", ...e }) as unknown as RunEvent;
const queued = (id: string, extra: Record<string, unknown> = {}) =>
  ev({ type: "message.queued", message_id: id, text: `text ${id}`, ...extra });

const fold = (state: InboxState, ...events: RunEvent[]): InboxState =>
  events.reduce(foldInboxEvent, state);

describe("how waiting items read", () => {
  const item = (extra: Record<string, unknown>) =>
    fold(emptyInbox, queued("a", extra)).items[0]!;

  test("a report, an agent message and a notice say what they are", () => {
    expect(waitingLabel(item({ source: "subagent", sender: "explorer" }))).toBe(
      "Report from explorer",
    );
    expect(waitingLabel(item({ source: "agent", sender: "planner" }))).toBe(
      "Message from planner",
    );
    expect(waitingLabel(item({ source: "system", notice: "open-tasks" }))).toBe(
      "Open tasks",
    );
  });

  test("a held item, a stopped inbox and paused agents each explain the wait", () => {
    const mine = item({});
    const report = item({ source: "subagent", sender: "x" });
    const base = emptyInbox;
    expect(idleReason(base, mine)).toBeNull();
    expect(idleReason(base, { ...mine, held: true })).toBe("Held");
    expect(idleReason({ ...base, waiting: true }, mine)).toBe(
      "Waiting for you",
    );
    expect(idleReason({ ...base, agentsPaused: true }, report)).toBe(
      "Agents paused",
    );
    // Paused agents stop what an agent sent, never what the operator typed.
    expect(idleReason({ ...base, agentsPaused: true }, mine)).toBeNull();
  });

  test("a delivery is due only while some item is not waiting on anything", () => {
    const one = fold(emptyInbox, queued("a"));
    expect(deliveryDue(emptyInbox)).toBe(false);
    expect(deliveryDue(one)).toBe(true);
    expect(deliveryDue({ ...one, waiting: true })).toBe(false);
    expect(
      deliveryDue({ ...one, items: [{ ...one.items[0]!, held: true }] }),
    ).toBe(false);
  });
});

describe("the inbox fold", () => {
  test("queued appends in order, once", () => {
    const s = fold(emptyInbox, queued("a"), queued("b"), queued("a"));
    expect(s.items.map((i) => i.id)).toEqual(["a", "b"]);
  });

  test("a frame with no source is the operator's", () => {
    const s = fold(emptyInbox, queued("a"));
    expect(s.items[0]?.source).toBe("operator");
    expect(s.items[0] && isOperatorItem(s.items[0])).toBe(true);
  });

  test("turn, notice and sender ride along", () => {
    const s = fold(
      emptyInbox,
      queued("a", { turn: true }),
      queued("b", { source: "system", notice: "open-tasks" }),
      queued("c", { source: "subagent", sender: "explorer" }),
    );
    expect(s.items.map((i) => [i.turn, i.notice, i.sender])).toEqual([
      [true, null, null],
      [false, "open-tasks", null],
      [false, null, "explorer"],
    ]);
    expect(s.items.map(isOperatorItem)).toEqual([true, false, false]);
  });

  test("injected removes the item, and so does withdrawn", () => {
    let s = fold(emptyInbox, queued("a"), queued("b"), queued("c"));
    s = fold(
      s,
      ev({ type: "message.injected", message_id: "a" }),
      ev({ type: "message.withdrawn", message_id: "c" }),
    );
    expect(s.items.map((i) => i.id)).toEqual(["b"]);
  });

  test("an event for an item that is not there changes nothing", () => {
    const s = fold(emptyInbox, queued("a"));
    expect(fold(s, ev({ type: "message.injected", message_id: "zz" }))).toEqual(
      s,
    );
    expect(
      fold(s, ev({ type: "message.edited", message_id: "zz", text: "x" })),
    ).toBe(s);
  });

  test("edit and hold change the item in place", () => {
    const s = fold(
      emptyInbox,
      queued("a"),
      ev({ type: "message.edited", message_id: "a", text: "new" }),
      ev({ type: "message.held", message_id: "a", held: true }),
    );
    expect(s.items[0]).toMatchObject({ text: "new", held: true });
  });

  test("inbox.waiting sets the Stop hold, and leaves the items", () => {
    let s = fold(emptyInbox, queued("a"));
    s = fold(s, ev({ type: "inbox.waiting", waiting: true }));
    expect(s.waiting).toBe(true);
    expect(s.items).toHaveLength(1);
    s = fold(s, ev({ type: "inbox.waiting", waiting: false }));
    expect(s.waiting).toBe(false);
  });

  test("other events are not the inbox's", () => {
    const s = fold(emptyInbox, queued("a"));
    expect(fold(s, ev({ type: "run.started" }))).toBe(s);
  });

  test("a read and a replay overlap without doubling", () => {
    const dto: InboxDTO = {
      messages: [
        {
          id: "a",
          text: "from the read",
          source: "operator",
          sender: null,
          held: false,
          turn: false,
          notice: null,
          queued_at: "t",
        },
      ],
      waiting_for_operator: true,
      agents_paused: true,
      run_id: null,
    };
    const read = inboxFromDto(dto);
    expect(read.waiting).toBe(true);
    expect(read.agentsPaused).toBe(true);
    const s = fold(read, queued("a"));
    expect(s.items).toHaveLength(1);
    expect(s.items[0]?.text).toBe("from the read");
  });
});
