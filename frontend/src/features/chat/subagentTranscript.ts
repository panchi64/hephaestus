import type { Accessor } from "solid-js";
import { settled } from "~/lib/resource";
import { useChatSession, type Subagent } from "./data";
import type { ChatMessage } from "./model";
import { createChatStream } from "./stream/chatStream";

export interface SubagentTranscript {
  /** The transcript, or undefined until the first read lands. */
  messages: Accessor<ChatMessage[] | undefined>;
  /** Whether a message is still being written, so its new rows trace in. */
  streaming: (message: ChatMessage) => boolean;
  error: Accessor<unknown>;
  loading: Accessor<boolean>;
}

/** A sub-agent's conversation as it happens: read once (again when its status or run
 *  changes, which is how a later run on it is found, even one woken while still
 *  `running`), with a stream of its own reattached to the run in flight. The backend
 *  persists a turn only when it ends, so the stream is what shows a running turn.
 *  Unmounting aborts the reader and leaves the run going. */
export function useSubagentTranscript(
  subagent: () => Subagent,
): SubagentTranscript {
  const id = () => subagent().conversationId;
  const session = useChatSession(
    id,
    () => `${subagent().status}:${subagent().runId}`,
  );
  const stream = createChatStream(() => settled(session)?.messages, id, {
    activeRun: () => settled(session)?.activeRun,
  });
  return {
    messages: () => (settled(session) ? stream.messages : undefined),
    streaming: (m) => m.streaming === true,
    error: () => session.error,
    loading: () => session.loading,
  };
}
