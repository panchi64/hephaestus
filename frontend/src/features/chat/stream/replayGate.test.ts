import { describe, expect, test } from "bun:test";
import type { RunEvent } from "~/lib/stream";
import { createReplayGate } from "./replayGate";

const ev = (seq: number): RunEvent =>
  ({ type: "answer.delta", seq, ts: "", text: "x" }) as RunEvent;

const record = () => {
  const deliveries: number[][] = [];
  return {
    deliveries,
    deliver: (events: RunEvent[]) => deliveries.push(events.map((e) => e.seq)),
  };
};

describe("a reattach's backlog", () => {
  test("is held until it reaches the seq the attach was decided at", () => {
    const r = record();
    const gate = createReplayGate(3, r.deliver);
    gate.push(ev(1));
    gate.push(ev(2));
    expect(r.deliveries).toEqual([]);
    gate.push(ev(3));
    expect(r.deliveries).toEqual([[1, 2, 3]]);
  });

  test("lands with the first event past it when the backlog's end was merged away", () => {
    const r = record();
    const gate = createReplayGate(3, r.deliver);
    gate.push(ev(2));
    gate.push(ev(5));
    expect(r.deliveries).toEqual([[2, 5]]);
  });

  test("live events after it arrive one at a time", () => {
    const r = record();
    const gate = createReplayGate(1, r.deliver);
    gate.push(ev(1));
    gate.push(ev(2));
    expect(r.deliveries).toEqual([[1], [2]]);
  });

  test("a stream that ends early still delivers what it held", () => {
    const r = record();
    const gate = createReplayGate(9, r.deliver);
    gate.push(ev(1));
    gate.flush();
    gate.flush();
    expect(r.deliveries).toEqual([[1]]);
  });

  test("with nothing to catch up on, nothing is held", () => {
    const r = record();
    const gate = createReplayGate(0, r.deliver);
    gate.push(ev(4));
    expect(r.deliveries).toEqual([[4]]);
  });
});
