import {
  createEffect,
  createSignal,
  untrack,
  type Accessor,
  type Setter,
} from "solid-js";
import { attachmentGate } from "~/lib/stores/sendGate";
import type { ComposerAttachment } from "../AttachmentChip";
import { loadDraft, saveDraft } from "./composerDraft";
import type { ComposerProps } from "./types";

export interface ComposerSend {
  /** A send is held, waiting for uploads to land. */
  awaiting: Accessor<boolean>;
  /** The inline note when a held file fails. */
  uploadFailed: Accessor<boolean>;
  canSend: () => boolean;
  submit: () => void;
  cancelAwaiting: () => void;
  /** STOP, carrying the draft when there is one. */
  stop: () => void;
}

/**
 * Sending, and the hold-then-send around it. SEND with files still in flight holds the
 * message and fires it the moment they land. Sending without them silently dropped the
 * file; refusing would make the operator watch a chip and press SEND a second time. The
 * field stays editable while it waits, and a second press (or Escape) calls the wait off.
 */
export function createComposerSend(deps: {
  props: ComposerProps;
  text: Accessor<string>;
  setText: Setter<string>;
  items: Accessor<ComposerAttachment[]>;
  field: () => HTMLTextAreaElement | undefined;
  announce: (message: string) => void;
}): ComposerSend {
  const { props, text, setText, items, field, announce } = deps;

  const readyIds = () =>
    items()
      .filter((a) => a.status === "ready")
      .map((a) => a.id);

  // A chip that is uploading or extracting counts toward "something to send": the
  // send is then held (below) rather than refused. A failed one doesn't — it can't go.
  const hasAttachment = () => items().some((a) => a.status !== "error");
  const canSend = () =>
    !props.disabled &&
    !props.sendBlocked &&
    (Boolean(text().trim()) || hasAttachment());

  const [awaiting, setAwaiting] = createSignal(false);
  // The inline note when a held file fails — the chip says which, this says why the
  // message didn't go.
  const [uploadFailed, setUploadFailed] = createSignal(false);
  const gate = () => attachmentGate(items());

  createEffect(() => {
    // A failure that has been retried or removed takes its note with it.
    if (gate() !== "failed") setUploadFailed(false);
  });

  createEffect(() => {
    if (!awaiting()) return;
    const state = gate();
    if (state === "pending") return;
    if (state === "failed") {
      setAwaiting(false);
      setUploadFailed(true);
      return;
    }
    // Everything landed. If the message can no longer go — a blocker appeared, the
    // field was emptied — the wait simply ends; the draft is still in the field.
    untrack(() => (canSend() ? submit() : setAwaiting(false)));
  });

  // A held send belongs to the conversation it was pressed in. Switching away calls
  // it off, or the uploads landing later would send the *new* conversation's draft.
  createEffect(() => {
    void props.storageKey;
    setAwaiting(false);
  });

  const cancelAwaiting = (): void => {
    setAwaiting(false);
    field()?.focus();
  };

  /** Restore a refused send under the key it was sent from — a refusal that lands
   *  after a conversation switch goes back into *that* conversation's draft rather
   *  than into whichever one is open now. Put **above** anything written since, the
   *  way a prefill joins a draft: the operator may already be on the next message,
   *  and neither text is theirs to lose. */
  const restoreRefused = (
    draft: string,
    key: string | undefined,
    saved: ComposerAttachment[],
  ): void => {
    const join = (current: string) =>
      current ? `${draft}\n${current}` : draft;
    if (key === untrack(() => props.storageKey)) {
      setText(join);
      if (saved.length && !untrack(items).length)
        props.attachments?.restore(saved);
      field()?.focus();
    } else if (key) saveDraft(key, join(loadDraft(key)));
    announce("Message not sent — restored");
  };

  function submit(): void {
    if (!canSend()) return;
    const state = gate();
    if (state === "failed") {
      setUploadFailed(true);
      return;
    }
    if (state === "pending") {
      setAwaiting(true);
      announce("Waiting for uploads");
      return;
    }
    setAwaiting(false);
    const draft = text();
    const key = props.storageKey;
    const saved = props.attachments?.snapshot() ?? [];
    const result = props.onSend(draft.trim(), readyIds());
    setText(""); // clears the persisted draft via the draft effect
    props.attachments?.clear();
    field()?.focus();
    if (result instanceof Promise)
      void result.then(
        (accepted) => {
          if (accepted === false) restoreRefused(draft, key, saved);
        },
        // A send that threw was not accepted either.
        () => restoreRefused(draft, key, saved),
      );
  }

  // Stop, carrying the draft when there is one.
  const stop = (): void => {
    setAwaiting(false);
    // Nothing is generating, so there is nothing to correct: a plain stop, and the
    // draft and its attachments stay in the field.
    if (!props.streaming) return props.onStop?.(undefined);
    const correction = text().trim();
    props.onStop?.(correction || undefined);
    if (correction) {
      setText(""); // clears the persisted draft via the draft effect
      props.attachments?.clear();
    } else field()?.focus(); // nothing to say yet — put the caret where saying it happens
  };

  return { awaiting, uploadFailed, canSend, submit, cancelAwaiting, stop };
}
