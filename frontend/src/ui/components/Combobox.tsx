import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  createUniqueId,
  onCleanup,
  type JSX,
} from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";
import { type IconName } from "../icons/registry";
import { cursorStep } from "./listCursor";
import { Popover } from "./Popover";
import { ListGroupHeader } from "./ListGroupHeader";
import { STATUS_CELL_ACTION_CLASS, STATUS_CELL_CLASS } from "./StatusBar";

export interface ComboboxOption {
  value: string;
  label: string;
}

export interface ComboboxGroup {
  label: string;
  options: ComboboxOption[];
}

export interface ComboboxProps {
  /** Options grouped under headings (e.g. one group per provider/endpoint). */
  groups: ComboboxGroup[];
  value?: string;
  onChange?: (value: string) => void;
  /** Trigger text when nothing is selected. */
  placeholder?: string;
  /** Search-field placeholder. */
  searchPlaceholder?: string;
  /** Hide the search field (short lists). Default: shown. */
  searchable?: boolean;
  /** Glyph inside the trigger (e.g. `cpu`). */
  leading?: IconName;
  /** Panel alignment relative to the trigger. Default left. */
  align?: "left" | "right";
  /** Message when discovery returned nothing. */
  emptyHint?: string;
  /** Drop the trigger's fill and its minimum width, so it reads as a line of text
   *  with a chevron rather than a control.
   *
   *  For a trigger that already sits on a raised surface — the composer's action row
   *  — where the default fill would be a fill on a fill (§7). The min-width goes with
   *  it because a bare trigger has no box whose edges need to stay put: it is sized
   *  by the model name it is showing.
   *
   *  The label drops to `dim` with the fill, for the same reason: with no box to
   *  separate it, a `bright` value reads as a heading over whatever sits beneath it.
   *  A bare trigger is a setting the operator glances at and rarely changes, not a
   *  thing to announce. */
  bare?: boolean;
  /** `sm` is the inline-strip trigger (24px), matching `Select`'s. Default `md`. */
  size?: "sm" | "md";
  /** Render the trigger as a `StatusBar` cell, as `Select`'s `cell` does — the bar's
   *  mono lowercase at the cell's height, no fill. Overrides `size` and `bare`. */
  cell?: boolean;
  /** Fired when the option list opens — for a caller whose options are worth
   *  re-fetching at the moment they're about to be looked at. */
  onOpen?: () => void;
  "aria-label"?: string;
  class?: string;
}

/** Searchable dropdown with grouped options — a Select that scales to long
 *  lists. Native `<select>` can't host a filter field, so this composes the
 *  shared Popover shell with a search box and a grouped option list. */
export function Combobox(props: ComboboxProps): JSX.Element {
  const [query, setQuery] = createSignal("");

  const selectedLabel = createMemo(() => {
    for (const g of props.groups)
      for (const o of g.options) if (o.value === props.value) return o.label;
    return undefined;
  });
  /** What the trigger reads — one expression for both of its variants. */
  const triggerLabel = () => selectedLabel() ?? props.placeholder ?? "Select";

  const filtered = createMemo(() => {
    const q = query().trim().toLowerCase();
    return props.groups
      .map((g) => ({
        label: g.label,
        options: q
          ? g.options.filter((o) => o.label.toLowerCase().includes(q))
          : g.options,
      }))
      .filter((g) => g.options.length > 0);
  });

  const searchable = () => props.searchable !== false;
  let triggerKey: ((e: KeyboardEvent) => boolean) | undefined;

  /* The keyboard cursor runs over the *filtered* rows as one flat list — the arrows
     cross group headings without stopping on them. Focus stays on the search field
     (or the trigger, when there is none), which names the active row through
     `aria-activedescendant`. */
  const baseId = createUniqueId();
  const flat = createMemo(() => filtered().flatMap((g) => g.options));
  // Each row's position in the flat list, looked up once per filter rather than
  // scanned for per row — a model list runs to hundreds.
  const indexOf = createMemo(
    () => new Map(flat().map((o, i) => [o.value, i] as const)),
  );
  const optionId = (value: string) =>
    `${baseId}-opt-${indexOf().get(value) ?? -1}`;
  const [active, setActive] = createSignal(-1);
  const activeValue = () => flat()[active()]?.value;

  // A new query re-ranks the list, so the cursor goes back to its top — that is the
  // row Enter picked before the arrows existed, and still the one it picks untouched.
  // On open with no query it starts on the current value instead.
  createEffect(() => {
    const q = query();
    const rows = flat();
    const current = q ? -1 : rows.findIndex((o) => o.value === props.value);
    setActive(current >= 0 ? current : rows.length ? 0 : -1);
  });

  createEffect(() => {
    const value = activeValue();
    if (value !== undefined)
      document
        .getElementById(optionId(value))
        ?.scrollIntoView({ block: "nearest" });
  });

  /** Arrows, Home/End and Enter over the filtered rows. Returns whether it consumed
   *  the key. Escape is `Popover`'s. */
  const listKey = (
    e: KeyboardEvent,
    pick: (value: string) => void,
  ): boolean => {
    if (e.key === "Enter") {
      e.preventDefault();
      const value = activeValue();
      if (value !== undefined) pick(value);
      return true;
    }
    // Home/End belong to the text caret while there is a query to move through.
    if ((e.key === "Home" || e.key === "End") && query()) return false;
    const next = cursorStep(e.key, active(), flat().length);
    if (next === null) return false;
    e.preventDefault();
    setActive(next);
    return true;
  };

  return (
    <Popover
      class={props.class}
      align={props.align}
      onOpen={props.onOpen}
      // Opened from a ruled strip, a cell's menu is square and ruled like it.
      variant={props.cell ? "console" : "default"}
      panelClass="flex max-h-80 w-64 flex-col"
      trigger={({ open, setOpen }) => (
        <button
          type="button"
          aria-label={props["aria-label"]}
          aria-haspopup="listbox"
          aria-expanded={open()}
          aria-activedescendant={
            open() && !searchable() && activeValue() !== undefined
              ? optionId(activeValue()!)
              : undefined
          }
          onClick={() => {
            setQuery("");
            setOpen(!open());
          }}
          onKeyDown={(e) => {
            // The search field takes the keys when there is one; this is for a
            // list short enough to go without.
            if (open() && !searchable()) triggerKey?.(e);
          }}
          class={
            props.cell
              ? cx(
                  STATUS_CELL_CLASS,
                  STATUS_CELL_ACTION_CLASS,
                  // `min-w-0`: a cell sizes to its content, but this one holds a model
                  // id that may run to forty characters, and it must truncate inside a
                  // capped wrapper rather than push the bar wider.
                  "w-full min-w-0 text-text",
                )
              : cx(
                  "flex max-w-56 items-center gap-1.5 rounded-ctl transition-colors hover:text-bright",
                  props.size === "sm" ? "h-6" : "h-8",
                  // A filled trigger's padding is inside a visible box, so it reads as
                  // the control's shape. A bare one has no box, so the same padding just
                  // reads as a gap — and then the real gap beside it lands on top,
                  // pushing whatever follows visibly further away than the next item in
                  // the row. Narrower padding keeps the bare trigger's edge close to its
                  // text, which is what lets a uniform gap actually look uniform.
                  props.bare ? "min-w-0 px-1" : "min-w-32 bg-raised px-2",
                )
          }
        >
          <Show when={props.leading}>
            <Icon name={props.leading!} size={12} class="shrink-0 text-dim" />
          </Show>
          <Show
            when={!props.cell}
            fallback={
              <span class="min-w-0 flex-1 truncate text-left">
                {triggerLabel()}
              </span>
            }
          >
            <Text
              variant="label"
              tone={selectedLabel() && !props.bare ? "bright" : "dim"}
              class="min-w-0 flex-1 truncate text-left"
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
        const onSearchKey = (e: KeyboardEvent) => void listKey(e, pick);
        // With no search field, focus stays on the trigger, so its keys drive the
        // list instead. Bound for as long as the panel is mounted.
        triggerKey = (e) => listKey(e, pick);
        onCleanup(() => (triggerKey = undefined));
        return (
          <>
            <Show when={searchable()}>
              <div class="shrink-0 p-1.5">
                <div class="relative">
                  <Icon
                    name="search"
                    size={14}
                    class="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-dim"
                  />
                  <input
                    ref={(el) => queueMicrotask(() => el.focus())}
                    value={query()}
                    onInput={(e) => setQuery(e.currentTarget.value)}
                    onKeyDown={onSearchKey}
                    role="combobox"
                    aria-expanded="true"
                    aria-activedescendant={
                      activeValue() !== undefined
                        ? optionId(activeValue()!)
                        : undefined
                    }
                    placeholder={props.searchPlaceholder ?? "Search…"}
                    class="h-7 w-full rounded-ctl bg-raised pl-8 pr-2 font-sans text-body text-bright placeholder:text-dim outline-none transition-colors"
                  />
                </div>
              </div>
            </Show>

            <div class="min-h-0 flex-1 overflow-y-auto py-1">
              <Show
                when={filtered().length > 0}
                fallback={
                  <div class="px-3 py-2">
                    <Text variant="micro" tone="dim">
                      {props.groups.length === 0
                        ? (props.emptyHint ?? "No options")
                        : "No matches"}
                    </Text>
                  </div>
                }
              >
                <For each={filtered()}>
                  {(group) => (
                    <div>
                      <ListGroupHeader label={group.label} />
                      <For each={group.options}>
                        {(opt) => (
                          <button
                            type="button"
                            id={optionId(opt.value)}
                            role="option"
                            tabIndex={-1}
                            aria-selected={opt.value === props.value}
                            onClick={() => pick(opt.value)}
                            onMouseEnter={() =>
                              setActive(indexOf().get(opt.value) ?? -1)
                            }
                            class={cx(
                              "flex w-full items-center gap-2 px-3 py-1.5 text-left transition-colors hover:bg-raised",
                              (opt.value === props.value ||
                                opt.value === activeValue()) &&
                                "bg-raised",
                            )}
                          >
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
                            <Text
                              variant="body"
                              tone={
                                opt.value === props.value ? "bright" : "default"
                              }
                              class="min-w-0 truncate"
                            >
                              {opt.label}
                            </Text>
                          </button>
                        )}
                      </For>
                    </div>
                  )}
                </For>
              </Show>
            </div>
          </>
        );
      }}
    />
  );
}
