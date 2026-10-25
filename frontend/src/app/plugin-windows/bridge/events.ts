/** What a plugin window hears without asking, each source behind the permission a
 *  plugin declares for it. A new kind of event is one entry here. */
import { createEffect, createRoot, on } from "solid-js";
import {
  awaitingInput,
  chatBusy,
  chatThreadId,
  runErrored,
} from "~/lib/stores/chatActivity";
import type { BridgeEvent } from "./protocol";

type Post = (event: BridgeEvent) => void;

interface EventSource {
  permission: string;
  /** Starts posting; returns what stops it. */
  subscribe: (post: Post) => () => void;
}

function agentState(): "idle" | "working" | "awaiting_input" {
  if (awaitingInput()) return "awaiting_input";
  return chatBusy() ? "working" : "idle";
}

const agentActivity: EventSource = {
  permission: "chat.read",
  subscribe: (post) =>
    createRoot((dispose) => {
      createEffect(() =>
        post({
          event: "agent.status",
          data: { state: agentState(), conversation_id: chatThreadId() },
        }),
      );
      createEffect(
        on(
          chatBusy,
          (busy, wasBusy) => {
            if (wasBusy && !busy)
              post({
                event: "agent.turn_finished",
                data: {
                  outcome: runErrored() ? "error" : "done",
                  conversation_id: chatThreadId(),
                },
              });
          },
          { defer: true },
        ),
      );
      return dispose;
    }),
};

const SOURCES: readonly EventSource[] = [agentActivity];

/** Subscribes a window to every source its plugin may hear. */
export function subscribeEvents(
  permissions: readonly string[],
  post: Post,
): () => void {
  const stops = SOURCES.filter((source) =>
    permissions.includes(source.permission),
  ).map((source) => source.subscribe(post));
  return () => stops.forEach((stop) => stop());
}
