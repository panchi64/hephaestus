/**
 * Editing a message that is still queued on a live run — the rule, without the markup.
 *
 * Three surfaces edit the same thing: the queued bubble at the foot of the thread, the
 * row the dock lists, and the composer's recall. They look nothing alike, so they are not
 * one component with a variant. What they must not differ on is the *protocol*, which is
 * two rules and easy to half-implement:
 *
 *   1. **Take the hold before the first keystroke.** The race is with the inbox's next
 *      delivery, not with the operator's typing: by the time they have written anything
 *      the message may already have been injected and the edit lost.
 *   2. **Release it on every way out** — saved, cancelled, or the surface unmounted under
 *      them. The hold stops delivery, so a forgotten one stalls the queue with nothing on
 *      screen to explain the stall.
 *
 * It is state only: no fetch, no timer, no DOM. What `onEdit`/`onHold` actually do is the
 * consuming layer's business.
 */

import { createSignal, onCleanup, type Accessor } from "solid-js";

export interface QueuedEditDeps {
  /** The backend id of the queued message, or `undefined` for a message that is not
   *  queued — a delivered turn being re-asked has no queue entry to hold. */
  messageId: Accessor<string | undefined>;
  /** Whether it is still queued *right now*. Read at the moment of each act rather than
   *  captured, because a message can be injected while its editor is open. */
  queued: Accessor<boolean>;
  /** The text the editor opens on. */
  initial: Accessor<string>;
  /** Commit. Called with the trimmed text, never with an empty one. */
  onSave: (text: string) => void;
  /** Ask the run to hold this message back, or to stop. */
  onHold?: (held: boolean) => void;
}

export interface QueuedEdit {
  editing: Accessor<boolean>;
  draft: Accessor<string>;
  /** A keystroke. */
  write: (text: string) => void;
  start: () => void;
  /** Leave without committing — and leave the queue moving again. */
  cancel: () => void;
  save: () => void;
}

export function createQueuedEdit(deps: QueuedEditDeps): QueuedEdit {
  const [editing, setEditing] = createSignal(false);
  const [draft, setDraft] = createSignal("");

  const write = (text: string): void => {
    setDraft(text);
  };

  const close = (): void => {
    setEditing(false);
    if (deps.messageId() && deps.queued()) deps.onHold?.(false);
  };

  const start = (): void => {
    write(deps.initial());
    setEditing(true);
    if (deps.queued()) deps.onHold?.(true);
  };

  const save = (): void => {
    const text = draft().trim();
    if (text) deps.onSave(text);
    close();
  };

  onCleanup(() => {
    if (editing()) close();
  });

  return { editing, draft, write, start, cancel: close, save };
}
