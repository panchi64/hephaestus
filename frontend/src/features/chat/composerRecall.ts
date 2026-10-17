/**
 * The composer as the third surface that edits a queued message.
 *
 * ArrowUp in an empty composer opens the newest queued operator message in the field
 * itself — the way a shell recalls its last command — so correcting what was just sent
 * does not mean reaching for the bubble up-thread. It is the same act the bubble and
 * the dock's list offer, so it follows the same protocol: one `createQueuedEdit`, whose
 * target is swapped as the arrows walk the queue. The hold is taken before the first
 * keystroke and released on every way out, exactly as on the other two surfaces — none
 * of it restated here.
 *
 * What this adds is only what is particular to a field that is *borrowed*: which message
 * the arrows land on (`queuedRecall.ts`), and leaving when that message stops being
 * queued underneath the operator.
 */

import { createEffect, createSignal, untrack } from "solid-js";
import type { ComposerRecallApi } from "~/ui";
import { isOperatorItem, type InboxItem } from "./inbox";
import { createQueuedEdit } from "./queuedEdit";
import { recallStep, type RecallDirection } from "./queuedRecall";

export interface ComposerRecallDeps {
  /** What is in the inbox, in delivery order. */
  items: () => readonly InboxItem[];
  editQueued: (queuedMessageId: string, text: string) => void;
  holdQueued: (queuedMessageId: string, held: boolean) => void;
  /** Hand text back to the composer's own draft. */
  stash: (text: string) => void;
}

/** The operator's queued message ids, oldest first. Never a report or a notice, which
 *  are neither theirs to rewrite nor to take back. */
const operatorIds = (items: readonly InboxItem[]): string[] =>
  items.filter(isOperatorItem).map((i) => i.id);

export function createComposerRecall(
  deps: ComposerRecallDeps,
): ComposerRecallApi {
  const [target, setTarget] = createSignal<string | null>(null);
  // The text the open edit started from, kept because the item itself can be gone by the
  // time the arrows are asked what changed.
  let opened = "";
  const item = () => {
    const id = target();
    return id === null ? undefined : deps.items().find((i) => i.id === id);
  };

  const edit = createQueuedEdit({
    messageId: () => target() ?? undefined,
    queued: () => item() !== undefined,
    initial: () => item()?.text ?? "",
    onHold: (held) => {
      const id = target();
      if (id) deps.holdQueued(id, held);
    },
    onSave: (text) => {
      const id = target();
      if (id) deps.editQueued(id, text);
    },
  });

  /** Leave without committing. Guarded, because closing an edit that is not open would
   *  still tell the inbox to stop holding a message nobody asked it to hold. */
  const cancel = (): void => {
    if (edit.editing()) edit.cancel();
    setTarget(null);
  };

  const open = (id: string): void => {
    setTarget(id);
    opened = item()?.text ?? "";
    edit.start();
  };

  const step = (direction: RecallDirection): boolean => {
    const current = edit.editing() ? target() : null;
    // An edit with changes in it is not walked away from: the arrows at the field's
    // edges are also plain caret keys, and one pressed on the way to the first line
    // would otherwise throw the rewrite away. Save or Escape leave it; the arrows
    // resume once it matches what was sent.
    if (current !== null && edit.draft() !== opened) return false;
    const next = recallStep(operatorIds(deps.items()), current, direction);
    // Nothing older: stay on the one that is open. Nothing newer: step out, which is a
    // move — the field goes back to the operator's own draft.
    if (next === null && direction === "older") return false;
    if (next === null && current === null) return false;
    cancel();
    if (next !== null) open(next);
    return true;
  };

  // The message left the inbox underneath the edit: injected before the hold landed,
  // withdrawn from another surface, or turned into a turn. Whatever was typed over it
  // has no message to land on, so it goes into the composer's own draft.
  createEffect(() => {
    if (!edit.editing() || item()) return;
    untrack(() => {
      const typed = edit.draft().trim();
      cancel();
      if (typed && typed !== opened.trim()) deps.stash(typed);
    });
  });

  // No cleanup of its own: `createQueuedEdit` releases the hold when this owner goes, and
  // it reads the target *as* it closes — resetting the target here first would leave it
  // closing an edit on no message, and the hold would outlive the room.

  return {
    draft: () => (edit.editing() ? edit.draft() : null),
    label: "Editing queued message",
    step,
    write: edit.write,
    save: () => {
      edit.save();
      setTarget(null);
    },
    cancel,
  };
}
