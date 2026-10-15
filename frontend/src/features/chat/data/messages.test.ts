import { describe, expect, test } from "bun:test";
import { toMessage } from "./messages";
import type { MessageDTO, ToolCallDTO } from "./wire";

function divider(overrides: Partial<MessageDTO> = {}): MessageDTO {
  return {
    id: "chk-1",
    role: "compaction",
    content: "the story so far",
    tools: [],
    messages_compacted: 12,
    tokens_before: 62_000,
    tokens_after: 4_000,
    ...overrides,
  };
}

describe("toMessage decodes a compaction divider", () => {
  // The cold read is the *second* producer of this row — the live `conversation.compacted`
  // fold is the first — and the two have to agree, or a reload contradicts what the
  // operator just watched happen.
  test("the fold's cost and its reason both come across", () => {
    const m = toMessage(divider({ compaction_reason: "overflow" }));
    expect(m.foldedMessages).toBe(12);
    expect(m.tokensBefore).toBe(62_000);
    expect(m.tokensAfter).toBe(4_000);
    expect(m.compactionReason).toBe("overflow");
  });

  test("the backend's parsed sections come across as they are", () => {
    // Parsed there, not here: the roster that decides where a section ends is a security
    // boundary, so this side carries the result rather than re-deriving it.
    const m = toMessage(
      divider({
        sections: [
          {
            key: "Goal",
            body: "ship the fold",
            untrusted: false,
            voice: "prose",
          },
          {
            key: "From tools and documents",
            body: "the page said the build is green",
            untrusted: true,
            voice: "prose",
          },
        ],
      }),
    );
    expect(m.summarySections?.map((s) => s.key)).toEqual([
      "Goal",
      "From tools and documents",
    ]);
    expect(m.summarySections?.[1].untrusted).toBe(true);
  });

  test("a checkpoint with no parsed sections falls back rather than showing none", () => {
    // An older backend sends no `sections` at all; the divider renders `content` then,
    // so this must decode to absent rather than to an empty list it would render as
    // "this checkpoint said nothing".
    expect(toMessage(divider()).summarySections).toBeUndefined();
    expect(
      toMessage(divider({ sections: null })).summarySections,
    ).toBeUndefined();
  });

  test("an empty section list decodes to absent, not to an empty list", () => {
    // The case `?? undefined` alone would miss, and the one the backend actually sends:
    // it defaults `sections` to `[]` on *every* row, and an empty array is not nullish.
    // Passing it through would hang a list off every turn that the divider would render
    // as "this checkpoint said nothing" — absent-not-empty is the rule here.
    expect(
      toMessage(divider({ sections: [] })).summarySections,
    ).toBeUndefined();
  });

  test("a checkpoint folded before reasons were stored decodes to none", () => {
    // Null is what the backend sends for a fold recorded before it wrote the reason onto
    // the checkpoint. It must not become a default: telling the operator the provider
    // forced a fold they asked for is worse than saying nothing.
    expect(
      toMessage(divider({ compaction_reason: null })).compactionReason,
    ).toBeUndefined();
    expect(toMessage(divider()).compactionReason).toBeUndefined();
  });

  test("a reason this build has no words for is dropped", () => {
    // The wire type is a plain string, so a newer backend naming a fourth trigger reaches
    // the mapper. Better an absent segment than a raw enum id in the divider's label.
    expect(
      toMessage(divider({ compaction_reason: "pressure" })).compactionReason,
    ).toBeUndefined();
  });

  test("an ordinary user turn carries no fold facts", () => {
    const m = toMessage({
      id: "m-1",
      role: "user",
      content: "hello",
      tools: [],
      messages_compacted: 0,
      tokens_before: 0,
      tokens_after: 0,
      compaction_reason: null,
    });
    expect(m.compactionReason).toBeUndefined();
    // The backend sends 0 rather than omitting these, which is why every consumer guards
    // on `> 0` — the mapper passes the zeros through rather than inventing absence.
    expect(m.foldedMessages).toBe(0);
  });
});

describe("toMessage replays an assistant turn in emission order", () => {
  const call = (id: string, over: Partial<ToolCallDTO> = {}): ToolCallDTO => ({
    id,
    name: "builtin_now",
    args: {},
    status: "ok",
    ...over,
  });
  const turn = (over: Partial<MessageDTO>): MessageDTO => ({
    id: "a1",
    role: "assistant",
    content: "",
    tools: [],
    ...over,
  });
  const shape = (dto: MessageDTO) =>
    (toMessage(dto).blocks ?? []).map((b) => {
      switch (b.kind) {
        case "thinking":
        case "text":
          return `${b.kind}:${b.text}`;
        case "tool":
          return `tool:${b.tool.id}[${(b.tool.children ?? []).map((c) => c.id).join(",")}]`;
        case "view_version":
          return `chip:${b.snapshotId}`;
        case "question":
          return `question:${b.question.toolCallId}`;
        default:
          return b.kind;
      }
    });

  test("a platform failure keeps its mark across a reload", () => {
    const tools = (dto: MessageDTO) =>
      (toMessage(dto).blocks ?? []).flatMap((b) =>
        b.kind === "tool" ? [b.tool] : [],
      );
    const [failed, plain] = tools(
      turn({
        tools: [
          call("c1", {
            status: "error",
            error: "error[internal]: x",
            platform: true,
          }),
          call("c2", { status: "error", error: "error: y" }),
        ],
        segments: [
          { kind: "tool", tool_call_id: "c1" },
          { kind: "tool", tool_call_id: "c2" },
        ],
      }),
    );
    expect(failed?.platform).toBe(true);
    expect(plain?.platform).toBeUndefined();
  });

  test("passages and calls interleave as the segments say", () => {
    expect(
      shape(
        turn({
          tools: [call("c1"), call("c2")],
          segments: [
            { kind: "thinking", text: "plan" },
            { kind: "text", text: "Checking." },
            { kind: "tool", tool_call_id: "c1" },
            { kind: "thinking", text: "again" },
            { kind: "tool", tool_call_id: "c2" },
            { kind: "text", text: "Done." },
          ],
        }),
      ),
    ).toEqual([
      "thinking:plan",
      "text:Checking.",
      "tool:c1[]",
      "thinking:again",
      "tool:c2[]",
      "text:Done.",
    ]);
  });

  test("a chip follows the call that minted it, and an answered question its call", () => {
    expect(
      shape(
        turn({
          tools: [
            call("show", { name: "view_show" }),
            call("ask", {
              name: "builtin_ask_user",
              answers: [{ question: "Which?", selections: ["A"] }],
            }),
          ],
          versions: [
            {
              snapshot_id: "v1",
              title: null,
              preview_kind: "html",
              tool_call_id: "show",
            },
          ],
          segments: [
            { kind: "tool", tool_call_id: "show" },
            { kind: "tool", tool_call_id: "ask" },
            { kind: "text", text: "Done." },
          ],
        }),
      ),
    ).toEqual([
      "tool:show[]",
      "chip:v1",
      "tool:ask[]",
      "question:ask",
      "text:Done.",
    ]);
  });

  test("a script's calls nest on it, and a chip one of them minted follows the script", () => {
    expect(
      shape(
        turn({
          tools: [
            call("script", { name: "run_code" }),
            call("inner", { name: "view_show", parent_tool_call_id: "script" }),
          ],
          versions: [
            {
              snapshot_id: "v1",
              title: null,
              preview_kind: "html",
              tool_call_id: "inner",
            },
          ],
          segments: [
            { kind: "tool", tool_call_id: "script" },
            { kind: "text", text: "Done." },
          ],
        }),
      ),
    ).toEqual(["tool:script[inner]", "chip:v1", "text:Done."]);
  });

  test("a chip whose call is unknown still renders, at the end", () => {
    expect(
      shape(
        turn({
          versions: [
            {
              snapshot_id: "lost",
              title: null,
              preview_kind: "html",
              tool_call_id: "gone",
            },
            { snapshot_id: "bare", title: null, preview_kind: null },
          ],
          segments: [{ kind: "text", text: "Done." }],
        }),
      ),
    ).toEqual(["text:Done.", "chip:lost", "chip:bare"]);
  });
});
