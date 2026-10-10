import { splitProps, type JSX, type ValidComponent } from "solid-js";
import { Dynamic } from "solid-js/web";
import { cx } from "../cx";

export type TextVariant =
  | "micro"
  | "plate"
  | "meta"
  | "code"
  | "label"
  | "body"
  | "reading"
  | "readout"
  | "readout-lg"
  | "display";

export type TextTone =
  | "dim"
  | "default"
  | "bright"
  | "accent"
  | "nominal"
  | "warn"
  | "alert"
  | "info"
  /** The ground colour, for text set ON a filled accent — the action tier's
   *  band. Never on the page's own ground, where it is invisible. */
  | "inverse";

/* The ONLY place the type scale is written, and the place the two voices (§2)
   are encoded.

   SANS is the interface speaking to the operator — labels, body, readouts,
   titles. Sentence case, no tracking, and it eases (the default register).

   MONO is the machine showing its work — ids, states, counters, durations,
   paths. Small, uppercase where it is a machine *label* (`meta`), and it snaps:
   `transition-none` plus the global `.font-mono` rule in theme.css put every
   mono element in the machine register, because a computer does not ease.

   `reading` is the odd one out, and deliberately so: it is the only variant on
   the READING scale (`--prose-*`) rather than the chrome scale. Use it for
   conversation CONTENT that isn't markdown — the operator's own turn in a
   transcript. That text sits inches from a rendered answer, so sizing it as
   chrome makes the operator's words look like a caption on the model's. Not for
   anything else; `body` remains the interface's default. */
const variantClass: Record<TextVariant, string> = {
  // ---- machine voice ----
  micro: "text-micro font-mono transition-none",
  // An engraved panel legend. `plate` NAMES a region, `meta` REPORTS a state —
  // the legend on the panel versus the value the panel is showing. The wider
  // tracking is load-bearing: at the same measure as its own readout it stops
  // reading as a legend.
  plate:
    "text-plate font-mono font-medium uppercase tracking-plate transition-none",
  meta: "text-meta font-mono font-medium uppercase tracking-label transition-none",
  // The one readable, un-uppercased mono step. §4 has specified it since the scale was
  // written and the token has been in `tokens.css` just as long; only the variant was
  // missing, so every surface wanting mono body text had to choose between 10px `micro`
  // and the sans `body` that gives up the machine voice entirely.
  code: "text-code font-mono transition-none",
  // ---- interface voice ----
  label: "text-label font-sans font-medium",
  body: "text-body font-sans",
  reading: "text-prose font-sans",
  readout: "text-readout font-sans font-medium",
  "readout-lg": "text-readout-lg font-sans font-medium",
  display: "text-display font-sans font-semibold tracking-tight",
};

const toneClass: Record<TextTone, string> = {
  dim: "text-dim",
  default: "text-text",
  bright: "text-bright",
  accent: "text-accent",
  nominal: "text-nominal",
  warn: "text-warn",
  alert: "text-alert",
  info: "text-info",
  inverse: "text-bg",
};

/** Element attributes pass through to the rendered element — an `id`, a `ref`, a
 *  scroll handler, an inline `style` for a size the caller computes (a viewer's zoom
 *  step). Colour and type still come from `variant`/`tone`. */
export interface TextProps extends Omit<
  JSX.HTMLAttributes<HTMLElement>,
  "children" | "class"
> {
  variant?: TextVariant;
  tone?: TextTone;
  /** Element to render. Defaults to <span>. */
  as?: ValidComponent;
  class?: string;
  children: JSX.Element;
}

/** Typographic primitive — the type-scale authority for the whole system. */
export function Text(props: TextProps): JSX.Element {
  const [local, rest] = splitProps(props, [
    "variant",
    "tone",
    "as",
    "class",
    "children",
  ]);
  return (
    <Dynamic
      component={local.as ?? "span"}
      class={cx(
        variantClass[local.variant ?? "body"],
        toneClass[local.tone ?? "default"],
        local.class,
      )}
      {...rest}
    >
      {local.children}
    </Dynamic>
  );
}
