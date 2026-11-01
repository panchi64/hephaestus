import { Show, type JSX } from "solid-js";
import { Tooltip } from "../Tooltip";
import { IS_MAC, SEND_CHORD } from "./sendChord";

/** A commit key's chord, quieter than its label. Hidden from assistive tech, which gets `aria-keyshortcuts`. */
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
 * The commit keys, at the strip's end, as one cell of the strip.
 *
 * While a run streams, STOP joins SEND. A caller that keeps the field enabled keeps SEND
 * beside it (it then queues into the live run, steering); one that disables the field
 * shows STOP alone.
 */
export function ComposerActions(props: ComposerActionsProps): JSX.Element {
  // Mid-run the key reads QUEUE (the message waits for the run's next step); recalled, SAVE.
  const sendLabel = () =>
    props.recalling
      ? "Save"
      : props.streaming
        ? "Queue"
        : (props.sendLabel ?? "Send");

  // A component, not a shared JSX value: one DOM node used in both arms of a `Show` makes
  // Solid throw ("the new child element contains the parent") when the state flips.
  //
  // While a send is held the button stays enabled and a press calls the wait off; the
  // chord hint is dropped since it no longer sends. While recalled it commits the edit,
  // and the send gate is neither consulted nor shown.
  //
  // Outlined, never a slab (`.heph-send`): dim while nothing can be sent, bright once
  // there is, filled only under the pointer.
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
  // Wrapped only when there is a reason to show; a tooltip on every SEND says nothing.
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
  // With a correction typed, STOP also sends it, so the label and tip must say so; Escape
  // is the stop that never carries the draft.
  const stopTip = () =>
    props.hasDraft
      ? "Stop the run and send this correction (Esc stops without sending)"
      : "Stop the run (Esc)";

  // Alert: it ends work in flight and may send a correction, which can't be taken back.
  // A component for the same reason as `SendButton`.
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
