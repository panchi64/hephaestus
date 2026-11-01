import { For, Show, createUniqueId, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";

/** The kind of ask, which is all the colour says: `approval` (amber — a sensitive
 *  action is parked), `question` (blue — the agent asked something), `alert` (red —
 *  a conflict or a stop that needs a decision). */
export type ActionTone = "approval" | "question" | "alert";

export interface ActionToneSpec {
  /** The accent the kind is reported in. */
  status: "warn" | "info" | "alert";
  /** The same accent as a text colour class. */
  text: "text-warn" | "text-info" | "text-alert";
  /** The one machine-voice label for a run parked on this kind of ask. */
  readout: string;
  /** The rail row's short tag. */
  tag: string;
  /** The composer's waiting strip. */
  strip: string;
  /** What a screen reader hears. */
  spoken: string;
}

/** Every echo of an ask outside its own thread reads its words and accent from here. */
export const ACTION_TONES: Record<ActionTone, ActionToneSpec> = {
  approval: {
    status: "warn",
    text: "text-warn",
    readout: "NEEDS APPROVAL",
    tag: "Waiting",
    strip: "Waiting on you",
    spoken: "awaiting approval",
  },
  question: {
    status: "info",
    text: "text-info",
    readout: "QUESTION",
    tag: "Question",
    strip: "Question for you",
    spoken: "asking a question",
  },
  alert: {
    status: "alert",
    text: "text-alert",
    readout: "NEEDS DECISION",
    tag: "Decision",
    strip: "Decision needed",
    spoken: "needs a decision",
  },
};

export interface ActionFrameProps {
  tone: ActionTone;
  /** The band's legend — what is being asked, in a few words. Set in the band's
   *  inverse mono plate voice. */
  title: JSX.Element;
  /** Optional content at the band's right, before the live marker (a count, a
   *  page index). Same inverse voice. */
  aside?: JSX.Element;
  /** Show the blinking square. Default `true` — the frame exists because the run
   *  is waiting; pass `false` for a settled record of an ask. */
  live?: boolean;
  /** Layout glue only. */
  class?: string;
  /** The body. The frame adds NO padding: a question card sits flush under the
   *  band, an approval pads its own. */
  children?: JSX.Element;
}

/**
 * The action tier: the one frame for anything the run is waiting on the operator
 * for. A solid plate band in the tone's colour with inverse text and a blinking
 * square, over a body tinted a few percent toward the same colour, inside a 1px
 * border of it. Square corners, no shadow.
 *
 * Presentation only. It decides nothing about what is being asked or what an
 * answer does; consumers put their content and their callbacks inside it.
 */
export function ActionFrame(props: ActionFrameProps): JSX.Element {
  const titleId = createUniqueId();
  return (
    <section
      class={cx("heph-action", props.class)}
      data-tone={props.tone}
      aria-labelledby={titleId}
    >
      <div class="heph-action-band">
        <Text
          variant="meta"
          tone="inverse"
          id={titleId}
          class="min-w-0 truncate"
        >
          {props.title}
        </Text>
        <span class="flex shrink-0 items-center gap-2">
          <Show when={props.aside}>
            <Text variant="meta" tone="inverse">
              {props.aside}
            </Text>
          </Show>
          <Show when={props.live ?? true}>
            <span class="heph-action-pulse" aria-hidden="true" />
          </Show>
        </span>
      </div>
      {props.children}
    </section>
  );
}

export interface ActionButtonSpec {
  label: string;
  onClick: () => void;
  /** The one filled cell. At most one per row — the brightest thing in the frame. */
  primary?: boolean;
  /** Keyboard hint printed after the label at reduced opacity (`↵`, `esc`). It
   *  only prints the key; binding it is the consumer's. */
  kbd?: string;
  disabled?: boolean;
  /** Accessible name when the visible label is not enough. */
  ariaLabel?: string;
}

export interface ActionButtonsProps {
  buttons: ActionButtonSpec[];
  /** Layout glue only. */
  class?: string;
}

/** A row of square action cells with joined borders: the primary filled
 *  (`text-bright` on the ground), the rest outlined. Wraps on a narrow frame. */
export function ActionButtons(props: ActionButtonsProps): JSX.Element {
  return (
    <div class={cx("heph-action-btns", props.class)}>
      <For each={props.buttons}>
        {(b) => (
          <button
            type="button"
            class="heph-action-btn"
            data-primary={b.primary ? "" : undefined}
            disabled={b.disabled}
            aria-label={b.ariaLabel}
            onClick={() => b.onClick()}
          >
            {b.label}
            <Show when={b.kbd}>
              <kbd>{b.kbd}</kbd>
            </Show>
          </button>
        )}
      </For>
    </div>
  );
}
