/**
 * The inbox family: messages waiting in the conversation's inbox, and the moment one
 * reaches the model.
 *
 * Everything but the last is inbox bookkeeping and touches no transcript row. Only
 * `message.injected` does, because from there the message is part of the turn.
 */

import { produce } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import { foldInboxEvent, type InboxItem } from "../../inbox";
import type { ChatMessage } from "../../model";
import { nextId } from "../patch";
import type { FoldContext } from "./types";

/** The row an injected item becomes. Nothing for a turn item — it opens its own turn,
 *  whose prompt arrives with that turn. A system item becomes a notice row. */
function injectedRow(item: InboxItem, ts: string): ChatMessage | null {
  if (item.turn) return null;
  if (item.source === "system")
    return {
      id: nextId("n"),
      role: "notice",
      content: item.text,
      notice: item.notice ?? undefined,
      createdAt: ts,
    };
  const fromAgent = item.source === "subagent" || item.source === "agent";
  return {
    id: nextId(fromAgent ? "s" : "u"),
    role: fromAgent ? "subagent" : "user",
    content: item.text,
    createdAt: ts,
    author: item.source === "agent" ? (item.sender ?? undefined) : undefined,
    reporter:
      item.source === "subagent" ? (item.sender ?? undefined) : undefined,
  };
}

/** Fold one inbox event; false when the event is not in this family. */
export function foldMessages(ev: RunEvent, c: FoldContext): boolean {
  const { deps, assistantId } = c;
  const { state, setMessages, inbox } = deps;
  switch (ev.type) {
    case "message.queued":
    case "message.edited":
    case "message.held":
    case "message.withdrawn":
    case "inbox.waiting":
      if (inbox) inbox.set(foldInboxEvent(inbox.get(), ev));
      return true;
    case "message.injected": {
      const item = inbox?.get().items.find((i) => i.id === ev.message_id);
      if (inbox) inbox.set(foldInboxEvent(inbox.get(), ev));
      const bubble = item ? injectedRow(item, ev.ts) : null;
      if (!bubble) return true;
      // Split the assistant flow around the message, mirroring how the backend
      // persists the steered turn (…assistant segment, user message, next assistant
      // segment…) so the live transcript and a reload agree.
      setMessages(
        produce((list) => {
          const target = list.find((m) => m.id === assistantId);
          const targetIsFresh =
            target && !target.blocks?.length && !target.content;
          if (targetIsFresh && list[list.length - 1] === target) {
            // A batch of injections at one boundary shares one fresh segment:
            // slot this message before the placeholder a prior injection opened.
            list.splice(list.length - 1, 0, bubble);
          } else {
            if (target) target.streaming = false;
            list.push(bubble);
            const fresh: ChatMessage = {
              id: nextId("a"),
              role: "assistant",
              model: target?.model,
              content: "",
              blocks: [],
              streaming: true,
              runId: state.activeRunId ?? undefined,
              createdAt: new Date().toISOString(),
            };
            list.push(fresh);
            state.foldTarget = fresh.id;
          }
        }),
      );
      return true;
    }
    default:
      return false;
  }
}
