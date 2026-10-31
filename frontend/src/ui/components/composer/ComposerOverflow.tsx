import { onCleanup, onMount, type JSX } from "solid-js";
import { cx } from "../../cx";
import { Icon } from "../../primitives/Icon";
import { Popover } from "../Popover";
import { STATUS_CELL_ACTION_CLASS, STATUS_CELL_CLASS } from "../StatusBar";

/** The secondary status-bar cells: inline while the composer is wide, folded behind a
 *  `more` cell once it is narrow. `cells` is called once per place it mounts, and the
 *  menu's copy exists only while the menu is open. The width rule is the
 *  `@container/composer` query, kept in step with `Composer`. */
export function ComposerOverflow(props: {
  cells: () => JSX.Element;
}): JSX.Element {
  let fold: HTMLDivElement | undefined;
  let close: (() => void) | undefined;

  // A composer widening past the fold hides the trigger, which would leave an open
  // panel anchored to an empty rect while the same cells reappear inline.
  onMount(() => {
    if (!fold) return;
    const ro = new ResizeObserver(([entry]) => {
      if (entry && entry.contentRect.width === 0) close?.();
    });
    ro.observe(fold);
    onCleanup(() => ro.disconnect());
  });

  return (
    <>
      <div class="flex min-w-0 items-stretch @max-composer-fold/composer:hidden">
        {props.cells()}
      </div>
      {/* The width rule sits on a wrapper: Popover's own `inline-flex` would beat `hidden`. */}
      <div ref={fold} class="hidden @max-composer-fold/composer:flex">
        <Popover
          variant="console"
          trigger={(api) => {
            close = api.close;
            return (
              <button
                type="button"
                aria-label="More controls"
                aria-expanded={api.open()}
                class={cx(
                  STATUS_CELL_CLASS,
                  STATUS_CELL_ACTION_CLASS,
                  api.open() ? "bg-raised text-bright" : "text-text",
                )}
                onClick={() => api.setOpen(!api.open())}
              >
                more
                <Icon name="chevron-down" size={10} class="shrink-0" />
              </button>
            );
          }}
          panel={() => (
            <div class="flex flex-col items-stretch py-1">{props.cells()}</div>
          )}
        />
      </div>
    </>
  );
}
