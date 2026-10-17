import { describe, expect, test } from "bun:test";
import type { ChatMessage, ToolInvocation } from "./model";
import { screenshotIndex } from "./screenshots";

const png = (data: string) => ({ mediaType: "image/png", data });

const tool = (
  over: Partial<ToolInvocation> & { id: string },
): ToolInvocation => ({
  name: "browse_screenshot",
  args: "{}",
  status: "ok",
  ...over,
});

const turn = (id: string, ...tools: ToolInvocation[]): ChatMessage => ({
  id,
  role: "assistant",
  content: "",
  createdAt: "",
  blocks: tools.map((t) => ({ kind: "tool", id: `b-${t.id}`, tool: t })),
});

describe("screenshotIndex", () => {
  test("finds screenshots across turns and inside scripts", () => {
    const index = screenshotIndex([
      turn("a1", tool({ id: "c1", images: [png("one")] })),
      turn(
        "a2",
        tool({
          id: "script",
          name: "run_code",
          children: [tool({ id: "c2", images: [png("two")] })],
        }),
      ),
    ]);
    expect(index.get("c1")?.data).toBe("one");
    expect(index.get("c2")?.data).toBe("two");
  });

  test("a call without an image is not indexed", () => {
    const index = screenshotIndex([turn("a1", tool({ id: "c1" }))]);
    expect(index.has("c1")).toBe(false);
  });
});
