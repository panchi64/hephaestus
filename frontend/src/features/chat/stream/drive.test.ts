import { describe, expect, test } from "bun:test";
import { toActiveRun } from "../data/summaries";
import type { ChatMessage } from "../model";
import { attachOptions, seatAfterPrompt } from "./drive";

/**
 * Where a reattached run's streaming answer goes.
 *
 * The backend writes the operator's prompt as the turn begins and names it on the run
 * (`active_run.prompt_message_id`), so a thread opened mid-run already holds the question.
 * The client used to invent a prompt-less bubble; now it seats the one thing it is missing
 * — the answer — directly after the row it answers.
 */

const rows = (...ids: string[]): ChatMessage[] =>
  ids.map((id) => ({ id }) as ChatMessage);

describe("seating a streaming answer", () => {
  test("it goes directly after the row the run names", () => {
    // Fighting the rule: the named row is not the last one, so 'append' and
    // 'after the prompt' disagree.
    const messages = rows("q0", "a0", "q1", "q2");
    expect(seatAfterPrompt(messages, "q1")).toBe(3);
  });

  test("a prompt that is the last row seats at the end", () => {
    expect(seatAfterPrompt(rows("q0", "a0", "q1"), "q1")).toBe(3);
  });

  test("a row the transcript does not hold is not guessed at", () => {
    expect(seatAfterPrompt(rows("q0", "a0"), "q1")).toBeNull();
  });

  test("a run that names no row is not guessed at either", () => {
    expect(seatAfterPrompt(rows("q0"), null)).toBeNull();
    expect(seatAfterPrompt(rows("q0"), undefined)).toBeNull();
  });
});

describe("attaching to a run the detail reports", () => {
  test("the reattach carries the row the run answers", () => {
    const run = toActiveRun({
      id: "r1",
      kind: "chat",
      status: "running",
      last_seq: 7,
      prompt_message_id: "p1",
    });
    expect(run?.promptMessageId).toBe("p1");
    expect(attachOptions(run!)).toEqual({
      fromSeq: 0,
      kind: "chat",
      replayThrough: 7,
      promptMessageId: "p1",
    });
  });

  test("a backend that names nothing maps to null, not undefined", () => {
    const run = toActiveRun({
      id: "r1",
      kind: "compaction",
      status: "running",
      last_seq: 0,
    });
    expect(run?.promptMessageId).toBeNull();
  });
});
