/**
 * The compaction family: the fold a thread runs on its own history, in flight and
 * settled. `compaction.started` seats a live turn, `compaction.delta` streams the summary
 * into it, and `conversation.compacted` (or `compaction.failed`) takes it back out.
 */

import { produce } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import { toast } from "~/ui";
import { requestFoldAnchor } from "../../foldAnchor";
import type { ChatMessage } from "../../model";
import type { FoldContext } from "./types";

/** The transcript id of the fold a `compaction.started` at ``seq`` opened.
 *
 *  Derived rather than generated so a replay re-seats the same turn instead of a second
 *  one: `seq` is monotonic per run and is the only per-event identifier that survives a
 *  reattach, which replays the whole buffer from 0. */
function liveFoldId(seq: number): string {
  return `compaction-live-${seq}`;
}

/** The run kind a hand-started fold is submitted as — the backend's `LANE_BY_KIND` key.
 *  Read here to report why a fold declined, and by the drive to skip seeding an
 *  assistant turn for a run that writes none. */
export const FOLD_RUN_KIND = "compaction";

/** Remove every fold still in flight — the live turns `compaction.started` seated.
 *  Both of the frames that end one (its own settle, and the run's terminal) do this. */
export function dropLiveFolds(list: ChatMessage[]): void {
  for (let i = list.length - 1; i >= 0; i -= 1)
    if (list[i].role === "compaction" && list[i].compaction) list.splice(i, 1);
}

/** Fold one compaction event; false when the event is not in this family. */
export function foldCompaction(ev: RunEvent, c: FoldContext): boolean {
  const { deps, announce } = c;
  const { setMessages } = deps;
  switch (ev.type) {
    case "compaction.started": {
      // The thread has stopped to fold its own history, and that takes tens of seconds
      // on a local endpoint — so it becomes a turn in the transcript *now*, the way an
      // assistant turn opens on its first delta rather than once the answer is whole.
      //
      // A turn rather than a row on the assistant's rail, which is where this used to
      // live. Two things were wrong with that at once: a fold the operator started by
      // hand has no assistant turn to hang off, so that path drew nothing anywhere; and
      // where there *was* one, the same fold appeared twice — once as a rail row and
      // again as the divider it settled into.
      //
      // Keyed by `seq`, the only per-event id stable across a replay. One turn can
      // legitimately fold twice (the threshold before it ran, then an overflow retry
      // inside it), and those are two pauses rather than one repeated.
      const live: ChatMessage = {
        id: liveFoldId(ev.seq),
        role: "compaction",
        content: "",
        createdAt: ev.ts,
        compaction: {
          reason: ev.reason,
          messages: ev.messages,
          tokensEstimate: ev.tokens_estimate,
        },
      };
      setMessages(
        produce((list) => {
          if (list.some((m) => m.id === live.id)) return;
          list.push(live);
        }),
      );
      return true;
    }
    case "compaction.delta":
      // The summary as the model writes it. Appended to the live turn only — it is the
      // model's working, not the checkpoint, and what the operator keeps is the parsed
      // summary the settle below seats in its place.
      setMessages(
        produce((list) => {
          // The delta carries no id of its own, so the target is the fold still in
          // flight — searched from the end, because a turn that folds twice has an
          // older, settled one above it and the newest is always the one writing.
          for (let i = list.length - 1; i >= 0; i -= 1) {
            const live = list[i];
            if (live.role !== "compaction" || !live.compaction) continue;
            live.compaction.summary = (live.compaction.summary ?? "") + ev.text;
            return;
          }
        }),
      );
      return true;
    case "compaction.failed":
      // The fold the opening frame announced is not going to land, so the live turn it
      // seated comes back out — an unsettled fold is not a fold, and the model's raw
      // working must not stay on screen as though it were the summary. The thread is
      // otherwise untouched.
      setMessages(produce(dropLiveFolds));
      if (announce)
        toast.error(
          ev.message ||
            `Compaction (${ev.reason}) failed — the conversation was not changed.`,
        );
      return true;
    case "conversation.compacted": {
      // Conversation-level, like the title — but it *is* a message, so it goes into
      // the list. Placed after the turn the backend named rather than appended: a
      // divider at the bottom would claim to have folded the turns it kept, and a
      // reload (which places it chronologically) would then disagree with the live view.
      const divider: ChatMessage = {
        id: ev.message_id,
        role: "compaction",
        content: ev.summary,
        createdAt: ev.ts,
        foldedMessages: ev.messages_compacted,
        tokensBefore: ev.tokens_before ?? undefined,
        tokensAfter: ev.tokens_after ?? undefined,
        // Defaulted rather than left absent: an older backend that sends no reason
        // can only have meant the ordinary one, and a divider is worse for having a
        // segment that appears and disappears with the backend version.
        compactionReason: ev.reason ?? "threshold",
        // The backend derives these from the very `summary` on this frame, and the
        // cold read parses the same stored string with the same function — so the
        // divider seated here and the one a reload seats are the same divider.
        summarySections: ev.sections ?? undefined,
        restored: ev.restored?.length ? ev.restored : undefined,
      };
      // Idempotent on `message_id`: a reattach replays the run's whole buffer
      // (`fromSeq: 0`) over a transcript that was cold-loaded *with* this turn
      // already in it, so an unguarded splice would seat a second identical one —
      // and re-announce a fold that happened minutes ago.
      let inserted = false;
      let misplaced = false;
      setMessages(
        produce((list) => {
          // The live turn goes, whether or not the settled one is seated below: it is
          // the model's raw working and this frame carries the real thing. Dropped
          // even on the idempotent path, since a replay re-opens it from the same
          // `compaction.started` and nothing else would ever close it.
          dropLiveFolds(list);
          if (list.some((m) => m.id === divider.id)) return;
          // The backend's boundary is authoritative; a null one is its own statement
          // that the checkpoint follows everything. A boundary naming a turn this
          // transcript does not hold is not a licence to guess — appended at the
          // bottom the divider would claim to have folded turns it kept — so the
          // thread is re-read instead (below).
          if (!ev.after_message_id) {
            list.push(divider);
            inserted = true;
            return;
          }
          const at = list.findIndex((m) => m.id === ev.after_message_id);
          if (at < 0) {
            misplaced = true;
            return;
          }
          list.splice(at + 1, 0, divider);
          inserted = true;
        }),
      );
      if (misplaced && !c.refetchedFor.has(divider.id)) {
        c.refetchedFor.add(divider.id);
        deps.onFoldMisplaced?.();
      }
      if (inserted) {
        // Take the operator to it. The live turn they were watching sat at the
        // tail; this one belongs at the fold boundary, which is usually off screen
        // above — so without this the thing they were waiting for arrives by
        // disappearing.
        if (announce) requestFoldAnchor(divider.id);
        // A fold that lands mid-answer scrolls past unseen — and it changes what the
        // model can still see, which is not something to discover later by reading
        // back. The checkpoint is the durable record; this is the notification — and
        // only for a fold that just happened, not one a replay is catching up on.
        // `messages_compacted` counts messages, not exchanges — say messages.
        if (announce)
          toast.info(
            ev.messages_compacted > 0
              ? `Context compacted — ${ev.messages_compacted} earlier ${ev.messages_compacted === 1 ? "message is" : "messages are"} now a summary for the model.`
              : "Context compacted — earlier messages are now a summary for the model.",
          );
      }
      return true;
    }
    default:
      return false;
  }
}
