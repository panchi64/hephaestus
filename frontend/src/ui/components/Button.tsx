import { Show, splitProps, type JSX } from "solid-js";
import { Dynamic } from "solid-js/web";
import { cx } from "../cx";
import { Icon, type IconProps } from "../primitives/Icon";
import { Frames } from "./Frames";

export type ButtonVariant = "primary" | "default" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends Omit<
  JSX.ButtonHTMLAttributes<HTMLButtonElement>,
  "type"
> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  leading?: IconProps["name"];
  trailing?: IconProps["name"];
  /** Overrides the size-derived icon px — for icon-only buttons that should
   *  fill the control height rather than sit at the default text-scale size. */
  iconSize?: number;
  /** Renders as an anchor when set (router intercepts for SPA nav). */
  href?: string;
  type?: "button" | "submit" | "reset";
  block?: boolean;
  /** Pressed/toggled state for a `variant="ghost"` control acting as a toggle
   *  (font-size step, wrap, keeper pin, …) — swaps the variant's resting `dim`
   *  tone for `bright` so brightness alone carries the state, matching the
   *  rest of the design system's color discipline. Pair with `aria-pressed`.
   *  No effect on other variants. */
  active?: boolean;
  /** An action this button started is still running. Swaps the leading icon for the
   *  throbber and disables the control, so the one thing a slow action must not do —
   *  look identical to an idle one — takes a prop rather than a hand-rolled label swap
   *  at each call site.
   *
   *  It disables rather than merely decorating, because a second press during a slow
   *  action is never what the operator meant: at best it is a wasted round trip, at
   *  worst a duplicate write. Callers that want a different label while working still
   *  pass one; this is about the affordance, not the word. */
  pending?: boolean;
}

/* `primary` is an inverted Swiss-modernist slab — the brightest thing in its
   region, which is the whole signal. It deliberately carries NO accent halo:
   attention is drawn by luminance, not hue (§1.3, §5), and a green glow on the
   Send button would have been the loudest thing on a chat screen whose composer
   is intentionally neutral. Every other variant is quiet — no fill, and a
   hairline only where the control's edge is its affordance. */
const variantClass: Record<ButtonVariant, string> = {
  primary: "bg-bright text-bg hover:opacity-90",
  default: "border border-line text-text hover:bg-raised hover:text-bright",
  ghost: "text-dim hover:bg-raised hover:text-bright",
  danger:
    "border border-alert/40 text-alert hover:bg-raised hover:border-alert",
};

/** `variant="ghost"` while `active` — the whole class string is swapped rather
 *  than appending `text-bright` alongside `text-dim`, so the two never fight
 *  over cascade order. */
const GHOST_ACTIVE_CLASS = "bg-raised text-bright hover:text-bright";

/* A button label is the interface speaking to the operator, so it is sans and
   sentence case (§2) — the old mono uppercase made every control shout. (The
   composer's commit keys are the machine's, and are `.ody-send`, not a size here.)

   The type family lives here rather than in the shared base, so a size never sets a
   font the base also sets — two `font-*` utilities on one node resolve by stylesheet
   order, not by which was meant. */
const sizeClass: Record<ButtonSize, string> = {
  sm: "h-6 px-2 gap-1 text-label font-sans font-medium",
  md: "h-8 px-3 gap-2 text-body font-sans font-medium",
  lg: "h-10 px-4 gap-2 text-body font-sans font-medium",
};

// Icons scale with the button so a larger control reads as larger, not padded.
const iconSize: Record<ButtonSize, number> = {
  sm: 12,
  md: 12,
  lg: 16,
};

/** Foundational control. Cosmetic differences are the `variant`/`size` props —
 *  never a forked component. */
export function Button(props: ButtonProps): JSX.Element {
  const [local, rest] = splitProps(props, [
    "variant",
    "size",
    "leading",
    "trailing",
    "iconSize",
    "href",
    "type",
    "block",
    "active",
    "pending",
    "disabled",
    "class",
    "children",
  ]);
  const variant = local.variant ?? "default";
  // Pulled out of `rest` on purpose: `rest` is spread *after* these attributes, so a
  // `disabled` left in it would overwrite the one `pending` derives — with `undefined`,
  // re-enabling the control mid-action.
  const disabled = () => local.pending || local.disabled || undefined;
  return (
    <Dynamic
      component={local.href ? "a" : "button"}
      href={local.href}
      type={local.href ? undefined : (local.type ?? "button")}
      disabled={local.href ? undefined : disabled()}
      aria-disabled={local.href && disabled() ? "true" : undefined}
      aria-busy={local.pending || undefined}
      class={cx(
        "inline-flex items-center justify-center whitespace-nowrap transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-40",
        local.active && variant === "ghost"
          ? GHOST_ACTIVE_CLASS
          : variantClass[variant],
        sizeClass[local.size ?? "md"],
        local.block && "w-full",
        local.class,
      )}
      {...rest}
    >
      {/* The throbber takes the leading icon's place rather than sitting beside it: a
          control that grew a glyph while working would shift its own label, and the
          leading icon is exactly the slot whose job is "what this button is about". */}
      <Show when={!local.pending} fallback={<Frames />}>
        <Show when={local.leading}>
          <Icon
            name={local.leading!}
            size={local.iconSize ?? iconSize[local.size ?? "md"]}
            stroke={local.iconSize ? 24 / local.iconSize : undefined}
          />
        </Show>
      </Show>
      {local.children}
      <Show when={local.trailing}>
        <Icon
          name={local.trailing!}
          size={local.iconSize ?? iconSize[local.size ?? "md"]}
          stroke={local.iconSize ? 24 / local.iconSize : undefined}
        />
      </Show>
    </Dynamic>
  );
}
