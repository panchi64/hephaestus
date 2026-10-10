import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  createUniqueId,
  type JSX,
} from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";
import { cursorStep } from "./listCursor";
import { Popover } from "./Popover";
import { STATUS_CELL_ACTION_CLASS, STATUS_CELL_CLASS } from "./StatusBar";

export interface SelectOption {
  value: string;
  label: string;
  /** A dim line under the label, in the menu only — the trigger stays one line. */
  description?: string;
  /** A background class (`bg-warn`, …) for a small square before the label, on the
   *  trigger and in the menu, for options that carry a colour of their own. */
  swatch?: string;
}

function Swatch(props: { class: string }): JSX.Element {
  return <span aria-hidden class={cx("size-2 shrink-0", props.class)} />;
}

export interface SelectProps {
  /** Uppercase field label rendered above the control. */
  label?: string;
  options: SelectOption[];
  value?: string;
  /** Value-based change handler (consistent with Checkbox/Toggle), so callers
   *  can pass a string setter directly: `onChange={setModel}`. */
  onChange?: (value: string) => void;
  invalid?: boolean;
  /** Dim helper or error text below the control. */
  hint?: string;
  disabled?: boolean;
  /** Trigger text when no option matches the current value. */
  placeholder?: string;
  /** `sm` is the inline-strip trigger — 24px and label-sized, for a row of settings
   *  under something that matters more (the composer's controls). Default `md`. */
  size?: "sm" | "md";
  /** Render the trigger as a `StatusBar` cell — no fill, no radius, the bar's mono
   *  lowercase at the cell's height — so a picker in the composer's status bar is one
   *  more cell of it rather than a control parked in the middle of one. Overrides
   *  `size`; the menu is unchanged. */
  cell?: boolean;
  /** Layout glue (width/height/margins) merged onto the field wrapper. */
  class?: string;
  "aria-label"?: string;
}

/** Dropdown select whose option list is rendered by the frontend (the shared
 *  Popover shell + Combobox-style rows), so the menu matches the design system
 *  instead of the OS-native control. Same value/onChange contract as before. */
export function Select(props: SelectProps): JSX.Element {
  const selected = createMemo(() =>
    props.options.find((o) => o.value === props.value),
  );
  const selectedLabel = () => selected()?.label;
  /** What the trigger reads — one expression for both of its variants. */
  const triggerLabel = () => selectedLabel() ?? props.placeholder ?? "Select…";

  /* The keyboard cursor. Focus stays on the trigger the whole time — the panel is
     portalled to the body, so nothing in it could receive a key — and the trigger
     points at the active row through `aria-activedescendant`, the same arrangement
     the composer's menu uses. */
  const baseId = createUniqueId();
  const optionId = (i: number) => `${baseId}-opt-${i}`;
  const [active, setActive] = createSignal(-1);
  const selectedIndex = () =>
    props.options.findIndex((o) => o.value === props.value);

  // Keep the active row in view as the arrows walk a list longer than the panel.
  createEffect(() => {
    const i = active();
    if (i >= 0)
      document
        .getElementById(optionId(i))
        ?.scrollIntoView({ block: "nearest" });
  });

  const onTriggerKey = (
    e: KeyboardEvent,
    open: boolean,
    setOpen: (open: boolean) => void,
  ) => {
    const openKey =
      e.key === "Enter" ||
      e.key === " " ||
      e.key === "ArrowDown" ||
      e.key === "ArrowUp";
    if (!open) {
      if (!openKey) return;
      // Prevented so the button's own Enter/Space click doesn't toggle it straight
      // back shut.
      e.preventDefault();
      setActive(selectedIndex() >= 0 ? selectedIndex() : 0);
      setOpen(true);
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const opt = props.options[active()];
      if (opt) props.onChange?.(opt.value);
      setOpen(false);
      return;
    }
    if (e.key === "Tab") {
      setOpen(false);
      return;
    }
    // A cell's menu prints each row's number; the number picks it.
    const digit = props.cell ? Number.parseInt(e.key, 10) : Number.NaN;
    if (digit >= 1 && digit <= Math.min(9, props.options.length)) {
      e.preventDefault();
      props.onChange?.(props.options[digit - 1]!.value);
      setOpen(false);
      return;
    }
    const next = cursorStep(e.key, active(), props.options.length);
    if (next === null) return;
    e.preventDefault();
    setActive(next);
  };

  return (
    <div class={cx("flex flex-col gap-1", props.class)}>
      <Show when={props.label}>
        <Text variant="label" tone="dim">
          {props.label}
        </Text>
      </Show>
      <Popover
        block
        // A described row is two to three lines tall, so the cap that fits ~8 plain
        // rows would scroll a five-row described menu; `Popover` still clamps to the
        // viewport when even this doesn't fit.
        // Opened from a ruled strip, a cell's menu is square and ruled like it.
        variant={props.cell ? "console" : "default"}
        panelClass={cx(
          "overflow-y-auto",
          props.cell ? "w-72" : "py-1",
          props.options.some((o) => o.description)
            ? "max-h-[28rem]"
            : "max-h-72",
        )}
        trigger={({ open, setOpen }) => (
          <button
            type="button"
            disabled={props.disabled}
            aria-label={props["aria-label"] ?? props.label}
            aria-haspopup="listbox"
            aria-expanded={open()}
            aria-invalid={props.invalid || undefined}
            aria-activedescendant={
              open() && active() >= 0 ? optionId(active()) : undefined
            }
            onClick={() => {
              if (!open()) setActive(selectedIndex());
              setOpen(!open());
            }}
            onKeyDown={(e) => onTriggerKey(e, open(), setOpen)}
            // Space activates a button on key*up*, after the keydown above has already
            // picked or opened; letting it through would toggle the panel again.
            onKeyUp={(e) => {
              if (e.key === " ") e.preventDefault();
            }}
            class={
              props.cell
                ? cx(
                    STATUS_CELL_CLASS,
                    STATUS_CELL_ACTION_CLASS,
                    "w-full text-left text-text outline-none",
                  )
                : cx(
                    // Matches Input/Combobox: a filled control, no bright edge on
                    // focus or open. Only `invalid` draws a border, because that is
                    // the one state that has to interrupt.
                    "flex w-full items-center border bg-raised text-left outline-none transition-colors disabled:cursor-not-allowed disabled:opacity-40",
                    props.size === "sm" ? "h-6 gap-1.5 px-2" : "h-8 gap-2 px-3",
                    props.invalid ? "border-alert" : "border-transparent",
                  )
            }
          >
            <Show when={selected()?.swatch}>
              {(swatch) => <Swatch class={swatch()} />}
            </Show>
            {/* A cell carries the bar's own type, not a `Text` variant: the label is
                one more value in a line of telemetry there, not a field's content. */}
            <Show
              when={!props.cell}
              fallback={
                <span class="min-w-0 flex-1 truncate">{triggerLabel()}</span>
              }
            >
              <Text
                variant={props.size === "sm" ? "label" : "body"}
                tone={selectedLabel() ? "bright" : "dim"}
                class="min-w-0 flex-1 truncate"
              >
                {triggerLabel()}
              </Text>
            </Show>
            <Icon
              name="chevron-down"
              size={props.cell ? 10 : 12}
              class={cx("shrink-0", !props.cell && "text-dim")}
            />
          </button>
        )}
        panel={({ close }) => {
          const pick = (value: string) => {
            props.onChange?.(value);
            close();
          };
          return (
            <div role="listbox" aria-label={props["aria-label"]}>
              {/* A cell's menu names itself in a plate, since the cell that opened it
                  shows only the value. */}
              <Show when={props.cell && props["aria-label"]}>
                {(label) => (
                  <div class="border-b border-line px-2.5 py-2">
                    <Text variant="plate" tone="dim">
                      {label()}
                    </Text>
                  </div>
                )}
              </Show>
              <For each={props.options}>
                {(opt, i) =>
                  props.cell ? (
                    <button
                      type="button"
                      id={optionId(i())}
                      role="option"
                      tabIndex={-1}
                      aria-selected={opt.value === props.value}
                      onClick={() => pick(opt.value)}
                      onMouseEnter={() => setActive(i())}
                      class={cx(
                        "grid w-full grid-cols-[8px_minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-0.5 border-b border-line px-2.5 py-2 text-left last:border-b-0 hover:bg-raised",
                        (opt.value === props.value || i() === active()) &&
                          "bg-raised",
                        opt.value === props.value && "ody-selected-edge",
                      )}
                    >
                      <Swatch class={opt.swatch ?? "bg-dim"} />
                      <Text
                        variant="body"
                        tone={opt.value === props.value ? "bright" : "default"}
                        class="whitespace-nowrap"
                      >
                        {opt.label}
                      </Text>
                      <Text variant="meta" tone="dim">
                        {i() < 9 ? String(i() + 1) : ""}
                      </Text>
                      <Show when={opt.description}>
                        <span class="col-start-2 col-end-4 font-sans text-label text-dim">
                          {opt.description}
                        </span>
                      </Show>
                    </button>
                  ) : (
                    <button
                      type="button"
                      id={optionId(i())}
                      role="option"
                      // Not focusable: the trigger keeps focus and names this row
                      // through `aria-activedescendant`.
                      tabIndex={-1}
                      aria-selected={opt.value === props.value}
                      onClick={() => pick(opt.value)}
                      onMouseEnter={() => setActive(i())}
                      class={cx(
                        "flex w-full flex-col gap-0.5 px-2 py-1.5 text-left transition-colors hover:bg-raised",
                        (opt.value === props.value || i() === active()) &&
                          "bg-raised",
                        opt.value === props.value && "ody-selected-edge",
                      )}
                    >
                      <span class="flex items-center gap-2">
                        <Icon
                          name="check"
                          size={12}
                          class={cx(
                            "shrink-0",
                            opt.value === props.value
                              ? "text-nominal"
                              : "opacity-0",
                          )}
                        />
                        <Show when={opt.swatch}>
                          {(swatch) => <Swatch class={swatch()} />}
                        </Show>
                        {/* The option sets its own width — no `truncate`. The panel is
                          floored at the field's width and grows to its contents, so
                          an option clipped here would be clipped by nothing: the room
                          to render it in full is always there for the asking. The
                          trigger above still truncates, because that one *is* bounded
                          by the field. */}
                        <Text
                          variant="body"
                          tone={
                            opt.value === props.value ? "bright" : "default"
                          }
                          class="whitespace-nowrap"
                        >
                          {opt.label}
                        </Text>
                      </span>
                      {/* The description wraps where the label doesn't: it is a
                        sentence, and letting it set the panel's width would stretch
                        every menu to its longest one. Indented to sit under the
                        label — past the check, and the swatch when there is one.
                        The micro step in the sans face rather than `Text`'s mono
                        `micro`: it is fine print, but it is still a sentence. */}
                      <Show when={opt.description}>
                        <span
                          class={cx(
                            "max-w-60 font-sans text-micro text-dim",
                            opt.swatch ? "pl-9" : "pl-5",
                          )}
                        >
                          {opt.description}
                        </span>
                      </Show>
                    </button>
                  )
                }
              </For>
            </div>
          );
        }}
      />
      <Show when={props.hint}>
        <Text variant="micro" tone={props.invalid ? "alert" : "dim"}>
          {props.hint}
        </Text>
      </Show>
    </div>
  );
}
