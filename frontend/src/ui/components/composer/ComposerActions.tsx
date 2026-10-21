import { Show, type JSX } from "solid-js";
import { Tooltip } from "../Tooltip";
import { IS_MAC, SEND_CHORD } from "./sendChord";

/** A commit key's chord, after its label at reduced weight — on screen so the rule is
 *  read rather than remembered, and quieter than the word so the word is what's read
 *  first. Hidden from assistive tech: the button's `aria-keyshortcuts` says it. */
function KeyHint(props: { children: string }): JSX.Element {
  return <kbd aria-hidden="true">{props.children}</kbd>;
}

export interface ComposerActionsProps {
  recalling: boolean;
  /** The recalled text has something in it to save. */
  recallSavable: boolean;
  awaiting: boolean;
  canSend: boolean;
  streaming: boolean | undefined;
  /** STOP is offered with nothing streaming; SEND keeps its plain meaning. */
  stoppable?: boolean;
  disabled: boolean | undefined;
  sendBlocked: string | null | undefined;
  /** What SEND is called outside a recall or a run. */
  sendLabel: string | undefined;
  /** The field holds a correction that STOP would carry. */
  hasDraft: boolean;
  onSave: () => void;
  onSubmit: () => void;
  onCancelAwaiting: () => void;
  onStop: () => void;
}

/**
 * The commit keys, at the strip's end. `flex` so the cluster is one cell of the strip,
 * its keys standing inside it with their own outline.
 *
 * While a run streams, STOP joins SEND so the interrupt sits where the user's focus
 * already is. A caller that keeps the field enabled mid-stream keeps SEND beside it
 * (⌘↵/SEND then queues into the live run — steering); one that disables the field shows
 * STOP alone.
 */
export function ComposerActions(props: ComposerActionsProps): JSX.Element {
  // Mid-run the same key reads QUEUE, because that is what it does: the message waits
  // for the run's next step rather than starting a turn. A label, not a second button.
  // While a message is recalled it reads SAVE.
  const sendLabel = () =>
    props.recalling
      ? "Save"
      : props.streaming
        ? "Queue"
        : (props.sendLabel ?? "Send");

  // A component, not a JSX value held in a variable: each `<SendButton />` below builds
  // its own element. Sharing one across both arms of the `Show` shares the same DOM
  // node, and Solid tears the tree apart trying to move it between them ("the new child
  // element contains the parent") the first time the block state flips.
  //
  // While a send is held the button reads what it is doing and a press calls it off —
  // enabled even though nothing new can be sent, because cancelling is the one thing
  // left to do with it. The chord hint is dropped then: it would say "press this to
  // send" over a button that no longer sends.
  //
  // While a message is recalled the same button commits the edit: it is the same chord,
  // and the same "this is done, let it go" — only what it does differs. A send gate says
  // nothing about an edit to a message already sent, so it is neither consulted nor
  // shown then.
  //
  // Outlined, never a white slab under the words (`.heph-send`): dim while there is
  // nothing to send — which is what `disabled` already says — bright once there is,
  // filled only under the pointer.
  const SendButton = () => (
    <button
      type="button"
      class="heph-send"
      disabled={
        props.recalling
          ? !props.recallSavable
          : !props.awaiting && !props.canSend
      }
      onClick={() =>
        props.recalling
          ? props.onSave()
          : props.awaiting
            ? props.onCancelAwaiting()
            : props.onSubmit()
      }
      aria-keyshortcuts={IS_MAC ? "Meta+Enter" : "Control+Enter"}
    >
      <Show
        when={props.recalling || !props.awaiting}
        fallback="Waiting for upload…"
      >
        {sendLabel()}
        <KeyHint>{SEND_CHORD}</KeyHint>
      </Show>
    </button>
  );
  // Wrapped only when there is something to say. A tooltip on every SEND would fire on
  // the one control the operator uses most, to tell them nothing.
  const sendBtn = () => (
    <Show
      when={!props.recalling && props.sendBlocked}
      fallback={<SendButton />}
    >
      {(reason) => (
        <Tooltip label={reason()} side="top">
          <SendButton />
        </Tooltip>
      )}
    </Show>
  );
  // The label says which of the two STOP is about to be, because "Stop" over a field
  // with a correction in it would give no warning that pressing it also sends the
  // correction — and the operator who typed one and then wanted a plain interrupt would
  // have no way to tell. The tip names Escape, the stop that never carries the draft —
  // which is also how an operator with a correction typed can still interrupt without
  // sending it.
  const stopTip = () =>
    props.hasDraft
      ? "Stop the run and send this correction (Esc stops without sending)"
      : "Stop the run (Esc)";

  // Alert: it ends work in flight, and with a correction typed it also sends it — the
  // one key on the unit with a consequence the operator cannot take back. A component
  // for the same reason as `SendButton`.
  const StopButton = () => (
    <Tooltip
      label={props.streaming ? stopTip() : "Stop the thread's running agents"}
      side="top"
    >
      <button
        type="button"
        class="heph-send"
        data-tone="stop"
        onClick={() => props.onStop()}
      >
        <span class="heph-send-sq" aria-hidden="true" />
        {props.hasDraft && props.streaming ? "Stop & correct" : "Stop"}
        <Show when={props.streaming}>
          <KeyHint>esc</KeyHint>
        </Show>
      </button>
    </Tooltip>
  );

  return (
    <span class="flex shrink-0 items-stretch">
      <Show
        when={props.streaming}
        fallback={
          <>
            {sendBtn()}
            <Show when={props.stoppable}>
              <StopButton />
            </Show>
          </>
        }
      >
        <Show when={!props.disabled}>{sendBtn()}</Show>
        <StopButton />
      </Show>
    </span>
  );
}
