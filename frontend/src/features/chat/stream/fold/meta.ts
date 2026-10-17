/**
 * Everything else an event can say: the model's text and the context placed ahead of it,
 * citations, the conversation-scoped plan and task list, a moved permission level, the
 * View's live head and versions, and the thread's name and links.
 */

import { produce } from "solid-js/store";
import type { RunEvent } from "~/lib/stream";
import {
  toVersionChipBlock,
  toViewSnapshotRef,
} from "../../data/viewSnapshots";
import { refreshSessions } from "../../data/sessions";
import { revealTitle } from "../../data/titleReveals";
import { ENGAGEMENT_ORDER, permissionLevel } from "../../model";
import type { Citation } from "../../model";
import { appendDelta, nextId, retractDelta } from "../patch";
import type { FoldContext } from "./types";

/** Fold one content or conversation-scoped event; false when it is not in this family. */
export function foldMeta(ev: RunEvent, c: FoldContext): boolean {
  const { deps, assistantId } = c;
  const { state, patchById, setMessages } = deps;
  switch (ev.type) {
    case "thinking.delta":
      patchById(assistantId, (m) => appendDelta(m, "thinking", ev.text));
      return true;
    case "answer.delta":
      patchById(assistantId, (m) => appendDelta(m, "text", ev.text));
      return true;
    case "answer.retracted":
      patchById(assistantId, (m) => {
        retractDelta(m, "text", ev.text_chars);
        retractDelta(m, "thinking", ev.thinking_chars);
      });
      return true;
    case "context.injected":
      // A block the chassis put in front of the model. It lands on the rail in the
      // order it happened — which is ahead of the work it shaped, since the turn's
      // context is assembled before the model sees any of it. Keyed by `seq` because
      // the same contributor can legitimately inject twice in one turn (a plan that
      // grew a task between steps is a new injection, not a repeat), and `seq` is the
      // only identifier on the wire that is unique per event and stable across a
      // replay.
      patchById(assistantId, (m) => {
        (m.blocks ?? (m.blocks = [])).push({
          kind: "context",
          id: `ctx-${ev.seq}`,
          injection: {
            contributor: ev.contributor,
            placement: ev.placement,
            tokens: ev.tokens,
            text: ev.text,
            truncated: ev.truncated,
          },
        });
      });
      return true;
    case "tasks.updated":
      // Whole-list replace, not a merge: the event is full state, which is what makes
      // it idempotent when the stream is replayed from an earlier seq on reconnect.
      state.tasksRevision += 1;
      deps.setTasks(ev.items);
      return true;
    case "plan.updated":
      // The document, not the checklist. Same full-state reasoning; the counter is
      // separate because the two arrive independently and a shared one would make each
      // backfill think the other had overtaken it.
      state.planRevision += 1;
      deps.setPlan({
        title: ev.title,
        body: ev.body,
        steps: ev.steps,
        status: ev.status,
        revision: ev.revision,
      });
      return true;
    case "permission.changed":
      // The level moved from inside the run. Seated as though it had been read off the
      // thread, because that is what it now is: the row says so, and the composer must
      // send this level rather than the one it was holding.
      //
      // Validated rather than cast, and that matters *because* it is sent back: a level
      // this build has no rule for would be seated, ridden on the next message, and
      // refused at the edge — the operator's message failing for a reason nothing on
      // screen explains. `permissionLevel` degrades an unreadable value to the
      // strictest one, which is the only reading that cannot widen a thread.
      deps.setPermission(permissionLevel(ev.level));
      return true;
    case "view.live": {
      // One live head per *conversation*, not per turn: clear any prior live
      // block (it may sit on an earlier turn) before marking this turn's, so a
      // replaced or stopped server never lingers as a stale LIVE head once the
      // viewport aggregates view items across the whole transcript.
      const live = { url: ev.url, title: ev.title ?? undefined };
      setMessages(
        produce((list) => {
          for (const m of list)
            if (m.blocks)
              m.blocks = m.blocks.filter((b) => b.kind !== "view_live");
          const m = list.find((x) => x.id === assistantId);
          if (m)
            (m.blocks ?? (m.blocks = [])).push({
              kind: "view_live",
              id: nextId("view-live"),
              live,
            });
        }),
      );
      return true;
    }
    case "view.live.stopped":
      // The live head is conversation-scoped and close usually arrives a turn or
      // more after it started — drop it wherever it lives, not just on this run.
      setMessages(
        produce((list) => {
          for (const m of list)
            if (m.blocks)
              m.blocks = m.blocks.filter((b) => b.kind !== "view_live");
        }),
      );
      return true;
    case "view.snapshot": {
      // A version minted by `show`: append to the conversation-scoped version list
      // (the panel), deduped since a reattach replay can re-deliver the event.
      const ref = toViewSnapshotRef(ev);
      deps.setSnapshots((prev) =>
        prev.some((s) => s.snapshotId === ref.snapshotId)
          ? prev
          : [...prev, ref],
      );
      // Fold an inline transcript chip only for a *static* preview (a `show(file=…)`).
      // A live/auto version (served head) is already marked by its `view_live` chip,
      // so a second chip for the same action would just be visual duplication.
      if (ref.preview) {
        const chip = toVersionChipBlock(assistantId, {
          snapshotId: ref.snapshotId,
          title: ref.title,
          previewKind: ref.preview.kind,
        });
        patchById(assistantId, (m) => {
          const blocks = m.blocks ?? (m.blocks = []);
          if (!blocks.some((b) => b.id === chip.id)) blocks.push(chip);
        });
      }
      return true;
    }
    case "conversation.titled":
      // Conversation-level, not message-level: hand it to the typewriter reveal
      // rather than folding onto the assistant message.
      revealTitle(ev.conversation_id, ev.title);
      // And pull the list now rather than at the end of the turn: the thread exists,
      // it has a name, and the rail claiming otherwise for the length of a long run is
      // a lie about state the backend has already settled. It is also
      // what the header's static title falls back to once the reveal finishes.
      refreshSessions();
      // The throbber answers "is the backend naming this thread", and it has just
      // stopped being true. It used to be left to the run's `finally`, because the
      // reveal could not render until the room adopted the new id — so clearing it
      // here would have shown a bare "New conversation" for the rest of the turn.
      // The surfaces resolve against the live id now, so the name arrives in the
      // same beat the throbber goes.
      deps.setTitlePending(false);
      return true;
    case "browser.opened":
      if (c.announce) deps.onBrowserOpened?.();
      return true;
    case "citation.added":
      patchById(assistantId, (m) => {
        const citations = m.citations ?? (m.citations = []);
        // One source can arrive several times — listed by a search, then read by a
        // fetch — and the backend says which sighting is the stronger claim. So this
        // replaces rather than skips: keep the row's position (it is the display
        // number) and take the higher rung's detail.
        const next: Citation = {
          url: ev.url,
          title: ev.title ?? undefined,
          key: ev.key,
          kind: ev.kind,
          engagement: ev.engagement,
          snippet: ev.snippet ?? undefined,
          published: ev.published ?? undefined,
          retrievedAt: ev.retrieved_at ?? undefined,
          sourceId: ev.source_id ?? undefined,
          ref: ev.ref ?? undefined,
        };
        const at = citations.findIndex((x) => (x.key ?? x.url) === ev.key);
        if (at < 0) citations.push(next);
        else if (
          ENGAGEMENT_ORDER[ev.engagement] >
          ENGAGEMENT_ORDER[citations[at].engagement ?? "listed"]
        )
          citations[at] = next;
      });
      return true;
    default:
      return false;
  }
}
