import { describe, expect, test } from "bun:test";
import type { AssistantBlock, ChatMessage } from "../model";
import { adoptLiveIdentity } from "./reseatIdentity";

const msg = (
  id: string,
  role: ChatMessage["role"],
  blocks?: AssistantBlock[],
): ChatMessage => ({
  id,
  role,
  content: "",
  createdAt: "2026-10-17T00:00:00Z",
  blocks,
});

const thinking = (id: string): AssistantBlock => ({
  kind: "thinking",
  id,
  text: "t",
});
const text = (id: string): AssistantBlock => ({ kind: "text", id, text: "x" });

describe("adoptLiveIdentity", () => {
  test("renames the live turn and keeps its block ids", () => {
    const live = [
      msg("m1", "user"),
      msg("u-live-1", "user"),
      msg("a-live-2", "assistant", [
        thinking("thinking-live-3"),
        text("text-live-4"),
      ]),
    ];
    const next = [
      msg("m1", "user"),
      msg("U9", "user"),
      msg("A9", "assistant", [thinking("A9-t0"), text("A9-x1")]),
    ];
    expect(adoptLiveIdentity(live, next)).toEqual([
      [1, "U9"],
      [2, "A9"],
    ]);
    expect(next[2].blocks?.map((b) => b.id)).toEqual([
      "thinking-live-3",
      "text-live-4",
    ]);
  });

  test("skips live-only blocks when pairing by kind", () => {
    const context: AssistantBlock = {
      kind: "context",
      id: "ctx-4",
      injection: {
        contributor: "plan",
        placement: "prompt",
        tokens: 1,
        text: "",
        truncated: false,
      },
    };
    const live = [
      msg("a-live-2", "assistant", [context, thinking("t1"), text("x1")]),
    ];
    const next = [
      msg("A9", "assistant", [thinking("A9-t0"), text("A9-x1"), text("A9-x2")]),
    ];
    expect(adoptLiveIdentity(live, next)).toEqual([[0, "A9"]]);
    expect(next[0].blocks?.map((b) => b.id)).toEqual(["t1", "x1", "A9-x2"]);
  });

  test("tool calls pair by call id, not by position", () => {
    const tool = (id: string, callId: string): AssistantBlock => ({
      kind: "tool",
      id,
      tool: { id: callId, name: "web_search", args: "", status: "ok" },
    });
    const live = [
      msg("a-live-2", "assistant", [
        tool("tool-live-1", "call-x"),
        tool("tool-live-2", "call-y"),
      ]),
    ];
    const next = [
      msg("A9", "assistant", [tool("A9-0", "call-y"), tool("A9-1", "call-z")]),
    ];
    adoptLiveIdentity(live, next);
    expect(next[0].blocks?.map((b) => b.id)).toEqual(["tool-live-2", "A9-1"]);
  });

  test("adopts nothing when the paths do not line up", () => {
    expect(
      adoptLiveIdentity(
        [msg("u-live-1", "user")],
        [msg("U9", "user"), msg("A9", "assistant")],
      ),
    ).toEqual([]);
    expect(
      adoptLiveIdentity([msg("u-live-1", "user")], [msg("A9", "assistant")]),
    ).toEqual([]);
    expect(adoptLiveIdentity([msg("m1", "user")], [msg("m2", "user")])).toEqual(
      [],
    );
  });

  test("an unchanged transcript renames nothing", () => {
    const live = [msg("m1", "user"), msg("m2", "assistant", [text("b")])];
    const next = [msg("m1", "user"), msg("m2", "assistant", [text("b")])];
    expect(adoptLiveIdentity(live, next)).toEqual([]);
  });
});
