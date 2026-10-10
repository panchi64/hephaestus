import { For, Show, createSignal, type JSX } from "solid-js";
import { Portal } from "solid-js/web";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";

/** Transient action feedback. The system's answer to "did that work?" — every
 *  action should resolve to a visible success/error toast. Not a spinner; it
 *  appears instantly and auto-dismisses (§6 states, §8 motion). */
export type ToastTone = "nominal" | "alert" | "warn" | "info";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastOptions {
  tone?: ToastTone;
  /** A single inline action, e.g. an UNDO. Dismisses the toast on click. */
  action?: ToastAction;
  /** ms before auto-dismiss. 0 keeps it until dismissed manually. */
  duration?: number;
}

interface ToastItem {
  id: number;
  message: string;
  tone: ToastTone;
  action?: ToastAction;
}

const [items, setItems] = createSignal<ToastItem[]>([]);
let seq = 0;

function dismiss(id: number): void {
  setItems((list) => list.filter((t) => t.id !== id));
}

function push(message: string, opts: ToastOptions = {}): number {
  const id = ++seq;
  const duration = opts.duration ?? 4000;
  setItems((list) => [
    ...list,
    { id, message, tone: opts.tone ?? "nominal", action: opts.action },
  ]);
  if (duration > 0) setTimeout(() => dismiss(id), duration);
  return id;
}

/** Call from anywhere: `toast.success("Saved")`, `toast.error("Send failed")`,
 *  `toast.success("Deleted", { action: { label: "Undo", onClick } })`.
 *  Render <Toaster/> once at the app root. */
export const toast = {
  show: push,
  success: (m: string, o: ToastOptions = {}) =>
    push(m, { ...o, tone: "nominal" }),
  error: (m: string, o: ToastOptions = {}) => push(m, { ...o, tone: "alert" }),
  warn: (m: string, o: ToastOptions = {}) => push(m, { ...o, tone: "warn" }),
  info: (m: string, o: ToastOptions = {}) => push(m, { ...o, tone: "info" }),
  dismiss,
};

const toneText: Record<ToastTone, string> = {
  nominal: "text-nominal",
  alert: "text-alert",
  warn: "text-warn",
  info: "text-info",
};

/** Stacked toast outlet. Mount once at the root so toasts are reachable from
 *  every surface (app + auth). */
export function Toaster(): JSX.Element {
  return (
    <Portal>
      <div class="pointer-events-none fixed top-4 left-1/2 z-[60] flex w-80 max-w-[calc(100vw-2rem)] -translate-x-1/2 flex-col gap-2">
        <For each={items()}>
          {(t) => (
            <div
              role="status"
              aria-live="polite"
              class={cx(
                "ody-rise pointer-events-auto flex items-stretch bg-surface shadow-2",
                toneText[t.tone],
              )}
            >
              <span class="w-0.5 shrink-0 bg-current" aria-hidden="true" />
              <div class="flex min-w-0 flex-1 items-start gap-2 px-3 py-2">
                <Text
                  variant="body"
                  tone="bright"
                  class="min-w-0 flex-1 break-words"
                >
                  {t.message}
                </Text>
                <Show when={t.action}>
                  <button
                    type="button"
                    class="shrink-0 text-label font-sans font-medium text-current hover:text-bright"
                    onClick={() => {
                      t.action!.onClick();
                      dismiss(t.id);
                    }}
                  >
                    {t.action!.label}
                  </button>
                </Show>
                <button
                  type="button"
                  aria-label="Dismiss"
                  class="shrink-0 text-dim transition-colors hover:text-bright"
                  onClick={() => dismiss(t.id)}
                >
                  <Icon name="close" size={12} />
                </button>
              </div>
            </div>
          )}
        </For>
      </div>
    </Portal>
  );
}
