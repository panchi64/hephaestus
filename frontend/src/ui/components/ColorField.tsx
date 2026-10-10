import { Show, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Button } from "./Button";
import {
  ACCENT_CONTRAST_FLOOR,
  accentContrast,
  meetsAccentFloor,
} from "../theme/contrast";
import type { ThemeMode } from "../theme/theme-store";

export interface ColorFieldProps {
  /** Sentence case — the interface naming the thing (§2). */
  label: string;
  /** What the colour means, not what it looks like. */
  description?: string;
  /** Current value, `#rrggbb`. */
  value: string;
  /** Fires continuously while the OS picker is dragged — repaint, don't persist. */
  onInput: (hex: string) => void;
  /** Fires when the picker settles. This is the one to write to storage. */
  onChange: (hex: string) => void;
  /** Which mode's background the contrast warning measures against. */
  mode: ThemeMode;
  /** Shows the reset control. Omit for a field with nothing to reset to. */
  onReset?: () => void;
  class?: string;
}

/**
 * A colour swatch that opens the OS picker, with the design system's contrast
 * floor reported underneath it.
 *
 * The warning is the reason this is a component rather than a bare
 * `<input type="color">`. The system's accents used to clear 4.5:1 by
 * construction — they were fixed values tuned once (§12). An operator who can
 * set them can also set an alert red that vanishes into black, and the only
 * honest way to allow that is to allow it *and say so*. So the field warns and
 * never blocks: it is the operator's interface, and a contrast ratio is
 * information, not a permission.
 *
 * The hex sits in mono `meta` and the label in sans, because they are the two
 * voices (§2) — the name is the interface speaking, the value is a number the
 * machine will hand to the cascade.
 *
 * The native input is the picker, but it is not the swatch: it is stretched
 * invisibly over a `<label>` that draws the colour itself, so the visible
 * control keeps the house corner radius and the hairline ring that is the only
 * thing separating a white swatch from Paper's white page.
 */
export function ColorField(props: ColorFieldProps): JSX.Element {
  const ratio = () => accentContrast(props.value, props.mode);
  const passes = () => meetsAccentFloor(props.value, props.mode);

  return (
    <div class={cx("flex flex-col gap-1", props.class)}>
      <div class="flex items-center justify-between gap-4">
        <div class="flex min-w-0 flex-col gap-0.5">
          <Text variant="label" tone="default">
            {props.label}
          </Text>
          <Show when={props.description}>
            <Text variant="micro" tone="dim">
              {props.description}
            </Text>
          </Show>
        </div>

        <div class="flex shrink-0 items-center gap-2">
          <Text variant="meta" tone="dim">
            {props.value}
          </Text>
          <Show when={props.onReset}>
            <Button
              variant="ghost"
              size="sm"
              leading="refresh"
              aria-label={`Reset ${props.label.toLowerCase()} to its default`}
              onClick={() => props.onReset?.()}
            />
          </Show>
          {/* `shadow-1` carries the hairline ring, which is load-bearing here:
              in Paper a near-white swatch on a white panel has no other edge. */}
          <label
            class="relative block h-7 w-10 cursor-pointer overflow-hidden border border-line-strong focus-within:shadow-focus"
            style={{ "background-color": props.value }}
          >
            <span class="sr-only">{props.label}</span>
            <input
              type="color"
              value={props.value}
              onInput={(e) => props.onInput(e.currentTarget.value)}
              onChange={(e) => props.onChange(e.currentTarget.value)}
              class="absolute inset-0 h-full w-full cursor-pointer opacity-0"
            />
          </label>
        </div>
      </div>

      <Show when={ratio() !== null && !passes()}>
        <Text variant="micro" tone="warn">
          {ratio()}:1 on the background — below the {ACCENT_CONTRAST_FLOOR}:1
          floor. Text and icons in this colour will be hard to read.
        </Text>
      </Show>
    </div>
  );
}
