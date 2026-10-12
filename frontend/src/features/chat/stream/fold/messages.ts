/**
 * The steering family: messages the operator queues into a live run, and what becomes of
 * them — tagged, edited, held, withdrawn, or injected into the turn.
 */

import { produce } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import type { ChatMessage } from "../../model";
import { nextId } from "../patch";
import type { FoldContext } from "./types";

/** Fold one steering event; false when the event is not in this family. */
export function foldMessages(ev: RunEvent, c: FoldContext): boolean {
  const { deps, assistantId } = c;
  const { state, setMessages } = deps;
  switch (ev.type) {
    case "message.queued": {
      // A message the backend accepted into this run. Usually it tags the optimistic
      // bubble `send` already pushed (matched by text, first untagged wins so duplicate
      // texts pair off in order); on a reattach replay there is no optimistic bubble, so
      // rebuild it from the event.
      //
      // A sub-agent's report rides the same road and must not read the same. It gets no
      // optimistic bubble to tag — nobody typed it — and it lands as a `subagent` row, so
      // the live transcript says what a reload says rather than attributing to the
      // operator words they have not even seen. It also means the report is not offered
      // the edit/withdraw affordances, which belong to a message its author can still
      // take back.
      const fromSubagent = ev.source === "subagent" || ev.source === "agent";
      setMessages(
        produce((list) => {
          if (list.some((m) => m.queuedMessageId === ev.message_id)) return;
          const untagged = fromSubagent
            ? undefined
            : list.find(
                (m) =>
                  m.queuedPending &&
                  !m.queuedMessageId &&
                  m.content === ev.text,
              );
          if (untagged) untagged.queuedMessageId = ev.message_id;
          else
            list.push({
              id: nextId(fromSubagent ? "s" : "u"),
              role: fromSubagent ? "subagent" : "user",
              content: ev.text,
              queuedPending: true,
              queuedMessageId: ev.message_id,
              createdAt: ev.ts,
              author: ev.sender ?? undefined,
            });
        }),
      );
      return true;
    }
    case "message.edited":
      // The operator rewrote a still-pending bubble. Usually `editQueued`
      // already applied the text optimistically-on-success; this fold makes a
      // reattach replay (and any second tab) converge on the same content. An
      // already-injected message is part of the turn and never changes.
      setMessages(
        produce((list) => {
          const bubble = list.find(
            (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
          );
          if (bubble) bubble.content = ev.text;
        }),
      );
      return true;
    case "message.held":
      // The run is holding this one back (or has stopped). Folded rather than left to
      // the request that asked for it, so a second tab — and a reattaching client
      // replaying the stream — see a stalled queue for what it is.
      setMessages(
        produce((list) => {
          const bubble = list.find(
            (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
          );
          if (bubble) bubble.queuedHeld = ev.held;
        }),
      );
      return true;
    case "message.withdrawn":
      // Only a still-pending bubble is removable — an already-injected message
      // is part of the turn and must never vanish from the transcript.
      setMessages(
        produce((list) => {
          const i = list.findIndex(
            (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
          );
          if (i >= 0) list.splice(i, 1);
        }),
      );
      return true;
    case "message.injected":
      // The queued message reached the model: promote its bubble to a normal
      // user turn and split the assistant flow around it, mirroring how the
      // backend persists the steered turn (…assistant segment, user message,
      // next assistant segment…) so the live transcript and a reload agree.
      setMessages(
        produce((list) => {
          const qi = list.findIndex(
            (m) => m.queuedMessageId === ev.message_id && m.queuedPending,
          );
          if (qi < 0) return;
          const [bubble] = list.splice(qi, 1);
          bubble.queuedPending = false;
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
    default:
      return false;
  }
}
