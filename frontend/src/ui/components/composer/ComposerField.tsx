import { For, Show, type JSX } from "solid-js";
import { cx } from "../../cx";
import { useAutosize } from "../../primitives/useAutosize";
import { tokenSpans, type ComposerTrigger } from "./composerToken";

// The field rests at one line and grows a line at a time up to this many lines or
// this share of the window, whichever is smaller, then scrolls — a long prompt gets
// room to be written and read back without the unit swallowing the conversation.
const MAX_ROWS = 10;
const MAX_VIEWPORT_SHARE = 0.4;

/** One token in the accent layer: the sigil cell welded to the name cell. A file's
 *  name splits at its last `/` so the directory can sit dim beside a bright basename. */
function Token(props: { trigger: ComposerTrigger; text: string }): JSX.Element {
  const name = () => props.text.slice(1);
  const cut = () => (props.trigger === "@" ? name().lastIndexOf("/") : -1);
  return (
    <span class="heph-tok" data-trigger={props.trigger}>
      <span class="heph-tok-sigil">{props.trigger}</span>
      <span class="heph-tok-name">
        <Show when={cut() >= 0} fallback={name()}>
          <span class="heph-tok-dir">{name().slice(0, cut() + 1)}</span>
          {name().slice(cut() + 1)}
        </Show>
      </span>
    </span>
  );
}

export interface ComposerFieldProps {
  /** What the field shows — a recalled message, else the operator's draft. */
  value: string;
  /** Whether the `/` `@` menu is wired: the field becomes a combobox over an accent layer. */
  tokens: boolean;
  recalling: boolean;
  lg: boolean;
  ariaLabel: string | undefined;
  placeholder: string | undefined;
  disabled: boolean | undefined;
  menuOpen: boolean;
  activeDescendant: string | undefined;
  /** Hands the textarea up so the composer can focus it and place the caret. */
  fieldRef: (el: HTMLTextAreaElement) => void;
  onInput: (value: string) => void;
  onKeyDown: (e: KeyboardEvent) => void;
  /** Caret moved or the text changed — the menu follows the token under it. */
  onCaret: () => void;
  onBlur: () => void;
  onPaste: ((e: ClipboardEvent) => void) | undefined;
}

/**
 * The textarea and, when `tokens` is on, the accent layer behind it. The two are one
 * control: the layer paints the words, the field owns the caret, and they must occupy
 * the same box for the colours to land on the right glyphs.
 */
export function ComposerField(props: ComposerFieldProps): JSX.Element {
  let field: HTMLTextAreaElement | undefined;
  // The accent layer behind the field. Held so its scroll can be kept in step with the
  // field's — past `MAX_ROWS` the textarea scrolls, and a layer that stayed put would
  // paint the colours of the first lines over whatever had scrolled into view.
  let highlightRef: HTMLDivElement | undefined;

  // Grow the field to fit what it shows — a recall swapping a message in included.
  // Both sizes grow: the hero is told apart by its width and padding, not by empty
  // rows.
  useAutosize(
    () => field,
    () => props.value,
    { maxRows: MAX_ROWS, maxViewportShare: MAX_VIEWPORT_SHARE },
  );

  /* The field carries no chrome of its own — no border, no fill, no focus ring.
     The card around it is the control, and it is what lights up on focus, so a
     second bordered box inside it would only add a line (§7). */
  const fieldClass = () =>
    cx(
      /* `text-prose`, the reading scale — the field produces the operator's turn,
         which renders at reading size a few pixels above it. Typing at 13px and
         watching it come back at 16px is the same mismatch, one step earlier. */
      /* `block`, because a textarea is inline-block and carries a baseline descender —
         ~5px of dead space under it inside any block container. It cost nothing in the
         flex column this used to sit in directly, and it is load-bearing now that the
         field shares a wrapper with the accent layer measured against it. */
      /* No `min-h-*`: the field rests at one row (`rows={1}`) at both sizes and
         grows from there, so an empty composer is one line of text. */
      "block w-full resize-none border-0 bg-transparent text-prose font-sans text-bright placeholder:text-dim outline-none disabled:opacity-40",
      props.lg ? "px-4 py-3" : "px-3 py-2.5",
    );

  /** The field's text split into plain runs and tokens (`Token`, `.heph-tok`).
   *
   *  A textarea cannot colour part of its own value, so the standard arrangement is used:
   *  this layer sits directly behind a text-transparent field, painting the same string
   *  with the same metrics, and the operator sees the caret and selection of the real
   *  control over the colours of this one.
   *
   *  Everything about it is therefore metric-bound to the field, which is why it shares
   *  `fieldClass()` rather than restating the typography: any difference in font, size,
   *  padding or wrapping shows up immediately as the colour drifting off the words. It
   *  also has to render a trailing newline as a space, since a `<div>` collapses one that
   *  a textarea shows — without it the last line scrolls out of step.
   */
  const highlighted = () => {
    const shown = props.value;
    // No accents on a recalled message: its tokens are not being resolved (see
    // `syncToken`), and colouring them would say they were.
    const spans = props.tokens && !props.recalling ? tokenSpans(shown) : [];
    const runs: { text: string; trigger: ComposerTrigger | null }[] = [];
    let at = 0;
    for (const span of spans) {
      if (span.start > at)
        runs.push({ text: shown.slice(at, span.start), trigger: null });
      runs.push({
        text: shown.slice(span.start, span.end),
        trigger: span.trigger,
      });
      at = span.end;
    }
    if (at < shown.length) runs.push({ text: shown.slice(at), trigger: null });
    return runs;
  };

  return (
    /* The two are one control: the layer paints the words, the field owns the
       caret, and they must occupy the same box for the colours to land on the
       right glyphs. The gap this wrapper would otherwise add is closed on the field
       itself (`block` in `fieldClass`), not here: a textarea is inline-block, so a
       block wrapper puts ~5px of baseline descender under it. The layer is
       `inset-0` of this wrapper, so those 5px made it taller than the field, and
       once it scrolled the two moved by different amounts and the colours slid off
       the words. */
    <div class="relative min-w-0">
      <Show when={props.tokens}>
        <div
          aria-hidden="true"
          ref={(el) => {
            highlightRef = el;
          }}
          class={cx(
            fieldClass(),
            // Behind the field, exactly on top of it, and inert: the real control takes
            // every click, caret move and selection. `isolate` keeps each token's box
            // (painted at `z-index: -1`) behind its text but above the card's fill.
            "pointer-events-none absolute inset-0 isolate overflow-hidden whitespace-pre-wrap break-words text-bright",
          )}
        >
          <For each={highlighted()}>
            {(run) => (
              <Show when={run.trigger} fallback={run.text}>
                {(trigger) => <Token trigger={trigger()} text={run.text} />}
              </Show>
            )}
          </For>
          {/* A textarea shows a trailing newline; a div collapses it. */}
          {props.value.endsWith("\n") ? " " : ""}
        </div>
      </Show>
      <textarea
        ref={(el) => {
          field = el;
          props.fieldRef(el);
        }}
        value={props.value}
        onInput={(e) => props.onInput(e.currentTarget.value)}
        onKeyDown={(e) => props.onKeyDown(e)}
        // Arrow keys and clicks move the caret without changing the text, and moving back
        // into a half-typed `/rev` is the same situation as having just typed it.
        onKeyUp={() => props.onCaret()}
        onClick={() => props.onCaret()}
        // The menu hangs off the field's content, so it goes when the field does. This is
        // also what replaces the backdrop `FloatingPanel` would otherwise draw over the
        // whole screen — see its `passive` prop.
        onBlur={() => props.onBlur()}
        onScroll={(e) => {
          if (highlightRef) highlightRef.scrollTop = e.currentTarget.scrollTop;
        }}
        onPaste={(e) => props.onPaste?.(e)}
        rows={1}
        // No chord here: SEND already carries it, and at a docked width the longer
        // placeholder wrapped the resting field onto a second row.
        placeholder={props.placeholder ?? "Write a message…"}
        aria-label={props.ariaLabel}
        disabled={props.disabled}
        role={props.tokens ? "combobox" : undefined}
        aria-expanded={props.tokens ? props.menuOpen : undefined}
        aria-controls={props.tokens ? "composer-menu" : undefined}
        // Focus stays here the whole time, so this is the only thing that tells a screen
        // reader which row the arrows are on.
        aria-activedescendant={props.activeDescendant}
        class={cx(
          fieldClass(),
          // The field's own glyphs go transparent so the coloured layer behind shows
          // through; the caret keeps a colour of its own, or typing would be invisible.
          // `relative` so it stacks above that layer rather than under it.
          props.tokens &&
            "relative bg-transparent text-transparent caret-bright",
        )}
      />
    </div>
  );
}
