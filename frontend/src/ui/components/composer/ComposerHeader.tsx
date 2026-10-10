import { Show, children, type JSX } from "solid-js";
import { Icon } from "../../primitives/Icon";
import { SEND_CHORD } from "./sendChord";

export interface ComposerHeaderProps {
  recalling: boolean;
  recallLabel: string | undefined;
  waiting: { label: string; onJump?: () => void } | null | undefined;
  title: string | undefined;
  headerStart: JSX.Element;
  headerEnd: JSX.Element;
  storageKey: string | undefined;
  /** The field holds something worth saying is kept. */
  hasDraft: boolean;
}

/**
 * One line of engraved mono across the top of the card: what the input is doing on
 * the left, what is waiting on the operator on the right. The caller fills both —
 * except while a message is recalled, when the Composer takes the whole line over.
 *
 * That takeover is the one line that says the field is not the operator's draft right
 * now, and it replaces the caller's line rather than stacking under it: the tell has
 * to be where the eye already is, and two header lines would make the unit taller at
 * exactly the moment its content is a single message being touched up. Dim, not an
 * accent: a mode is a state of the input, not a signal (§5), and the pen glyph plus
 * the SAVE key already carry it.
 *
 * While the thread waits on the operator, the plate is the action tier's amber strip —
 * never under it, one line either way. A recalled message keeps its own plate: the edit
 * is what the operator is doing right now.
 */
export function ComposerHeader(props: ComposerHeaderProps): JSX.Element {
  // Each slot resolved once — a JSX prop is a getter that rebuilds its elements on
  // every read, and a slot tested for emptiness and then rendered would mount twice.
  const headerStart = children(() => props.headerStart);
  const headerEnd = children(() => props.headerEnd);
  const headerLeft = () => headerStart() ?? props.title;
  // With nothing of the caller's to say on the right, the line says the draft is kept:
  // the words in the field are persisted under `storageKey` as they are typed, and an
  // operator about to navigate away is owed knowing that. Only on a composer that
  // already has a header — one that grew a line on the first keystroke would jump.
  const headerRight = () =>
    headerEnd() ??
    (headerLeft() && props.storageKey && props.hasDraft
      ? "Draft saved"
      : undefined);
  // The plate: the 7px square, what the thread is doing, and its aside. Its tone is
  // the unit's (`.ody-composer[data-live]` turns it info while a run streams); a piece
  // the caller hands in may carry its own.
  const plate = (
    <Show when={props.recalling || headerLeft() || headerRight()}>
      <div class="ody-composer-plate">
        <span class="ody-composer-plate-sq" aria-hidden="true" />
        <Show
          when={props.recalling}
          fallback={
            <>
              <span class="min-w-0 truncate">{headerLeft()}</span>
              <span class="ody-composer-plate-end">{headerRight()}</span>
            </>
          }
        >
          <span class="inline-flex min-w-0 items-center gap-1.5 truncate">
            <Icon name="pen" size={10} />
            {props.recallLabel}
          </span>
          <span class="ody-composer-plate-end">
            {SEND_CHORD} save · Esc cancel
          </span>
        </Show>
      </div>
    </Show>
  );

  return (
    <Show when={!props.recalling && props.waiting} fallback={plate}>
      {(waiting) => (
        <div class="ody-composer-wait" role="status">
          <span class="ody-action-pulse" aria-hidden="true" />
          <span class="min-w-0 truncate">{waiting().label}</span>
          <Show when={waiting().onJump}>
            {(jump) => (
              <button type="button" onClick={() => jump()()}>
                Jump ↑
              </button>
            )}
          </Show>
        </div>
      )}
    </Show>
  );
}
