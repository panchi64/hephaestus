import { children, For, Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";

export interface TabItem {
  value: string;
  /** Usually a plain string; accepts JSX for a tab that carries a glyph/dot
   *  alongside its label (e.g. a live-status indicator). */
  label: string | JSX.Element;
}

export interface TabsProps {
  items: TabItem[];
  value: string;
  onChange: (value: string) => void;
  /** Tabs share the width equally and the strip becomes a band of its own.
   *
   *  For a strip that *is* the boundary between a region's header and its
   *  content rather than a control sitting inside the header — the viewport's
   *  PREVIEW/CODE. A full-width set of two reads as a division; the same two
   *  tabs sized to their labels read as a pair of buttons that happen to be
   *  there. */
  fill?: boolean;
  /** Controls parked at the end of the strip — a close for the pane the tabs
   *  name, a count. For a strip that *is* a region's header: the actions belong
   *  on the same rule as the tabs, and a row of their own under it would be a
   *  second band of chrome for one button. */
  trailing?: JSX.Element;
  /** A filling strip's side inset. `md` matches a region whose other rows sit at
   *  12px — a strip standing in for a pane header should start where the header
   *  would. A prop rather than a `class`, since `cx` does not resolve two paddings. */
  gutter?: "sm" | "md";
  class?: string;
}

/** Horizontal tab strip. The selected tab lifts to `surface-raised` and is the
 *  only one at medium weight. */
export function Tabs(props: TabsProps): JSX.Element {
  const [local] = splitProps(props, [
    "items",
    "value",
    "onChange",
    "fill",
    "trailing",
    "gutter",
    "class",
  ]);
  const trailing = children(() => local.trailing);
  return (
    // The strip, which is the tablist *and* whatever is parked at its end. Those are
    // two elements rather than one because `role="tablist"` may hold tabs and nothing
    // else — a close button inside it is announced as part of the tab set and counted
    // in "tab N of M". Without a `trailing` the extra box changes nothing: the rule and
    // the padding stay out here, the scrolling and the gap stay on the tabs.
    <div
      class={cx(
        "flex items-stretch",
        // A filling strip draws the one rule §7 sanctions: it is not decoration
        // between two regions that space already separates, it is the edge of
        // the header itself, and the content below starts against it.
        local.fill && "border-b border-line py-1.5",
        local.fill && (local.gutter === "md" ? "px-3" : "px-2"),
        local.class,
      )}
    >
      {/* Scrolls rather than wrapping or clipping when the labels outgrow the
          container — a tab strip that silently hides its last tab is worse than one
          with a scrollbar.
          No rule under the strip (§7): the selected tab's own fill marks the set, and
          the old border-b drew a line across every screen that had tabs whether or not
          anything needed dividing there. */}
      <div
        class="scrollbar-thin flex min-w-0 flex-1 items-stretch gap-1 overflow-x-auto"
        role="tablist"
      >
        <For each={local.items}>
          {(tab) => {
            const active = () => tab.value === local.value;
            return (
              <button
                type="button"
                role="tab"
                aria-selected={active()}
                onClick={() => local.onChange(tab.value)}
                class={cx(
                  "px-3 py-1.5 text-body font-sans whitespace-nowrap transition-colors",
                  "outline-none focus-visible:shadow-focus",
                  local.fill && "min-w-0 flex-1",
                  // Selected is the console's selection: raised fill, a 2px bright
                  // inset edge under the tab, and the only medium weight in the
                  // strip. No shadow — a tab is a cell, not a layer.
                  active()
                    ? "ody-selected-edge-b bg-raised text-bright font-medium"
                    : "font-normal text-dim hover:bg-raised hover:text-text",
                )}
              >
                {tab.label}
              </button>
            );
          }}
        </For>
      </div>
      {/* `items-center` because the strip itself stretches its tabs, and `shrink-0`
          so the controls hold their size while the tablist beside them scrolls.

          Resolved through `children()` rather than read twice: a slot prop is a
          getter, and asking for it in the guard *and* in the body builds the
          elements twice — once only to be thrown away. */}
      <Show when={trailing()}>
        <div class="flex shrink-0 items-center gap-2 pl-2">{trailing()}</div>
      </Show>
    </div>
  );
}
