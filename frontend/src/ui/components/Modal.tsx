import { Show, splitProps, type JSX } from "solid-js";
import { Portal } from "solid-js/web";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  /** Footer actions, right-aligned. */
  footer?: JSX.Element;
  /** Max-width utility class override (default narrow column). */
  class?: string;
  /** Drop the body inset. For content that owns its own edges — a list whose
   *  rows should meet the border, a palette whose field sits flush at the top. */
  padded?: boolean;
  children: JSX.Element;
}

/** Centered dialog over a dim backdrop. Appears instantly (no fade). */
export function Modal(props: ModalProps): JSX.Element {
  const [local] = splitProps(props, [
    "open",
    "onClose",
    "title",
    "footer",
    "class",
    "padded",
    "children",
  ]);
  return (
    <Show when={local.open}>
      <Portal>
        <div
          class="heph-fade-in fixed inset-0 z-50 flex items-center justify-center bg-bg/70 p-4 backdrop-blur-[2px]"
          onClick={local.onClose}
        >
          {/* Overlay: smoothed corners, `shadow-2` (which carries its own
              hairline ring), and the eased rise of the human register (§8).
              The header/footer rules are gone — the padding already separates
              them from the body. */}
          <div
            role="dialog"
            aria-modal="true"
            class={cx(
              "heph-rise flex max-h-[85vh] w-full max-w-md flex-col bg-surface shadow-2",
              local.class,
            )}
            onClick={(e) => e.stopPropagation()}
          >
            <Show when={local.title}>
              <header class="flex items-center justify-between gap-4 px-4 pt-4 pb-1">
                <Text variant="body" tone="bright" class="font-medium">
                  {local.title}
                </Text>
                <button
                  type="button"
                  onClick={local.onClose}
                  aria-label="Close"
                  class="text-dim transition-colors hover:text-bright"
                >
                  <Icon name="close" size={16} />
                </button>
              </header>
            </Show>
            <div
              class={cx(
                "min-h-0 flex-1 overflow-auto",
                local.padded !== false && "p-4",
              )}
            >
              {local.children}
            </div>
            <Show when={local.footer}>
              <footer class="flex items-center justify-end gap-2 px-4 pt-1 pb-4">
                {local.footer}
              </footer>
            </Show>
          </div>
        </div>
      </Portal>
    </Show>
  );
}
