import { For, Show, type JSX } from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { ActionButtons, type ActionButtonSpec } from "./ActionFrame";
import {
  NUMBER_KEYS,
  choiceHints,
  numberKeyIndex,
  stepIndex,
} from "./choiceKeys";

export interface ChoiceOption {
  /** Stable identity — what `value` and `onChange` speak in. */
  id: string;
  label: JSX.Element;
  /** What picking it means, right-aligned on the same line ("All 214 projects ·
   *  about 6 min"). */
  hint?: JSX.Element;
}

export interface ChoiceListProps {
  options: ChoiceOption[];
  /** The picked option's id, or `null` when none is (free text is being typed).
   *  Ignored when `multiple` is set. */
  value: string | null;
  /** A row was picked — by click, number key or arrow. `null` when typing into
   *  the free-text row deselects the options. With `multiple`, a row was toggled:
   *  the consumer flips it in its own `selected`. */
  onChange: (id: string | null) => void;
  /** Any number of rows may be on at once — a `group` of checkboxes rather than a
   *  radiogroup. The arrows then move focus without toggling, and typing free text
   *  keeps the ticked rows (the words qualify the picks rather than replacing one). */
  multiple?: boolean;
  /** The ticked ids, with `multiple`. */
  selected?: string[];
  /** Adds the free-text row (dashed keycap). Omit for a closed set of answers. */
  freeText?: {
    value: string;
    onInput: (text: string) => void;
    placeholder?: string;
    /** Accessible name for the input. Default "Answer in your own words". */
    label?: string;
  };
  /** The footer's single filled primary. Enter on a row or in the free text also
   *  submits. */
  onSubmit: () => void;
  /** Default "Answer", or "Send answer" while the free text holds the answer. */
  submitLabel?: string;
  submitDisabled?: boolean;
  /** Further cells in the footer, before the primary (Stop, Back) — so a host with
   *  its own way out shares the one footer instead of stacking a second under it. */
  secondary?: ActionButtonSpec[];
  /** Disable every row and the input (an answer is in flight). */
  disabled?: boolean;
  /** Id of the element that states the question — the radiogroup's name. */
  labelledBy?: string;
  /** Layout glue only. */
  class?: string;
}

/**
 * A question's answers as a radiogroup of edge-to-edge ruled rows — never side by
 * side. Each row is a square keycap with its number, the answer, and its
 * consequence right-aligned on the same line; the picked row has a filled keycap
 * and a tinted row. An optional free-text row (dashed keycap) deselects the options
 * when typed into. One footer holds the key hints and a single filled ANSWER.
 *
 * Keyboard: a number key picks that row, ↑/↓ move the pick (roving tab stop, as a
 * radiogroup does), Enter submits.
 *
 * Presentation only: it reports picks, input and submit through callbacks and holds
 * no state of its own. Sits flush — it brings its own row padding, so it can be
 * placed straight inside an `ActionFrame` (whose tone it takes through `--action`).
 */
export function ChoiceList(props: ChoiceListProps): JSX.Element {
  const rows: HTMLButtonElement[] = [];

  const pickAt = (index: number): void => {
    const option = props.options[index];
    if (!option || props.disabled) return;
    props.onChange(option.id);
    rows[index]?.focus();
  };

  const isChecked = (id: string): boolean =>
    props.multiple ? (props.selected ?? []).includes(id) : id === props.value;

  const pickedIndex = (): number =>
    props.options.findIndex((o) => isChecked(o.id));

  const onRowKeyDown = (e: KeyboardEvent, index: number): void => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const dir = e.key === "ArrowDown" ? 1 : -1;
      const next = stepIndex(index, dir, props.options.length);
      // A checkbox group moves focus; a radiogroup moves the pick with it.
      if (props.multiple) rows[next]?.focus();
      else pickAt(next);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (!props.submitDisabled) props.onSubmit();
    }
  };

  const onRootKeyDown = (e: KeyboardEvent): void => {
    if (e.target instanceof HTMLInputElement) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const index = numberKeyIndex(e.key, props.options.length);
    if (index === null) return;
    e.preventDefault();
    pickAt(index);
  };

  const typing = (): boolean => Boolean(props.freeText?.value.trim());
  const hints = (): string =>
    choiceHints(props.options.length, Boolean(props.freeText));

  return (
    <div class={cx("flex flex-col", props.class)} onKeyDown={onRootKeyDown}>
      <div
        role={props.multiple ? "group" : "radiogroup"}
        aria-labelledby={props.labelledBy}
        class="ody-choices"
      >
        <For each={props.options}>
          {(option, i) => {
            const checked = (): boolean => isChecked(option.id);
            // Roving tab stop: the picked row, else the first, is the one Tab
            // lands on; the arrows move within the group. A checkbox group keeps
            // every row tabbable, as checkboxes are.
            const tabStop = (): boolean =>
              props.multiple ||
              checked() ||
              (pickedIndex() === -1 && i() === 0);
            return (
              <button
                ref={(el) => (rows[i()] = el)}
                type="button"
                role={props.multiple ? "checkbox" : "radio"}
                aria-checked={checked()}
                tabindex={tabStop() ? 0 : -1}
                disabled={props.disabled}
                class="ody-choice"
                onClick={() => pickAt(i())}
                onKeyDown={(e) => onRowKeyDown(e, i())}
              >
                <span class="ody-keycap" aria-hidden="true">
                  {i() < NUMBER_KEYS ? i() + 1 : ""}
                </span>
                <Text variant="body" tone="bright" class="min-w-0">
                  {option.label}
                </Text>
                <Show when={option.hint}>
                  <Text variant="label" tone="dim" class="ody-choice-hint">
                    {option.hint}
                  </Text>
                </Show>
              </button>
            );
          }}
        </For>
        <Show when={props.freeText}>
          {(free) => (
            <label class="ody-choice-free">
              <span class="ody-keycap" aria-hidden="true">
                ✎
              </span>
              <input
                type="text"
                value={free().value}
                placeholder={
                  free().placeholder ?? "Or answer in your own words…"
                }
                aria-label={free().label ?? "Answer in your own words"}
                disabled={props.disabled}
                class="min-w-0 bg-transparent py-1 text-body text-bright outline-none placeholder:text-dim"
                onInput={(e) => {
                  const text = e.currentTarget.value;
                  free().onInput(text);
                  if (text.trim() && !props.multiple && props.value !== null)
                    props.onChange(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.isComposing) {
                    e.preventDefault();
                    if (!props.submitDisabled) props.onSubmit();
                  }
                }}
              />
            </label>
          )}
        </Show>
      </div>
      <div class="ody-choice-foot">
        <Text variant="micro" tone="dim">
          {hints()}
        </Text>
        <ActionButtons
          buttons={[
            ...(props.secondary ?? []),
            {
              label: props.submitLabel ?? (typing() ? "Send answer" : "Answer"),
              kbd: "↵",
              primary: true,
              disabled: props.submitDisabled || props.disabled,
              onClick: () => props.onSubmit(),
            },
          ]}
        />
      </div>
    </div>
  );
}
