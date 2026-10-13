import { untrack, type Accessor } from "solid-js";
import { settled } from "~/lib/resource";
import { isLive, useChatSession, type Subagent } from "./data";
import type { ChatMessage } from "./model";
import { createChatStream } from "./stream/chatStream";

export interface SubagentTranscript {
  /** The transcript, or undefined until the first read lands. */
  messages: Accessor<ChatMessage[] | undefined>;
  /** Whether a message is still being written, so its new rows trace in. */
  streaming: (message: ChatMessage) => boolean;
  error: Accessor<unknown>;
  loading: Accessor<boolean>;
  /** Following the run's own events rather than polling its persisted turns. */
  live: boolean;
}

/** A sub-agent's conversation, read one of two ways, chosen at mount.
 *
 *  **Opened as a thread, it follows the live stream**: the conversation is read once
 *  (again on a status change, which is how a later run on it is found) and a stream of
 *  its own reattaches to the run in flight, the way the main room does on a cold read.
 *  Closing the pane aborts the reader and leaves the run going.
 *
 *  **Otherwise it polls**, re-reading when the sub-agent's figures move. The backend
 *  persists a turn only when it ends, so a polled transcript shows a running turn as its
 *  task alone. That is the price of not holding a connection open for every card in the
 *  pane; opening the sub-agent as a thread is the operator choosing to follow it. */
export function useSubagentTranscript(
  subagent: () => Subagent,
): SubagentTranscript {
  const id = () => subagent().conversationId;
  const live = untrack(() => subagent().opened);

  if (!live) {
    const session = useChatSession(
      id,
      () => `${subagent().status}:${subagent().contextUsed ?? 0}`,
    );
    const messages = () => settled(session)?.messages;
    return {
      messages,
      streaming: (m) => isLive(subagent()) && messages()?.at(-1)?.id === m.id,
      error: () => session.error,
      loading: () => session.loading,
      live,
    };
  }

  const session = useChatSession(id, () => subagent().status);
  const stream = createChatStream(() => settled(session)?.messages, id, {
    activeRun: () => settled(session)?.activeRun,
  });
  return {
    messages: () => (settled(session) ? stream.messages : undefined),
    streaming: (m) => m.streaming === true,
    error: () => session.error,
    loading: () => session.loading,
    live,
  };
}
