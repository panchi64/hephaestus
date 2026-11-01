import { describe, expect, test } from "bun:test";
import { buildConversationTranscript } from "./transcriptExport";
import type { ChatMessage } from "./model";

const message = (role: ChatMessage["role"], content: string): ChatMessage => ({
  id: content,
  role,
  content,
  createdAt: "t",
  blocks:
    role === "assistant" ? [{ kind: "text", id: "b", text: content }] : [],
});

describe("buildConversationTranscript", () => {
  test("labels the agent's turns with the name it is given", () => {
    const transcript = buildConversationTranscript(
      [message("user", "hello"), message("assistant", "hi")],
      "scout",
    );
    expect(transcript).toContain("Operator · t\nhello");
    expect(transcript).toContain("scout · t\n");
    expect(transcript).not.toContain("Talos");
  });
});
