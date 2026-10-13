import { createEffect, onCleanup, Show, splitProps, type JSX } from "solid-js";
import { Portal } from "solid-js/web";
import { bytes, pad } from "~/lib/format";
import { Text } from "../primitives/Text";
import { Icon } from "../primitives/Icon";
import { ImageFrame } from "./ImageFrame";
import { RegistrationFrame } from "./RegistrationFrame";

export interface LightboxItem {
  /** Resolved object URL (consumer resolves via useAuthedBlobUrl). Undefined while loading. */
  src: string | undefined;
  /** The upstream fetch for `src` failed — renders NO DATA instead of holding LOADING…. */
  error?: boolean;
  filename: string;
  /** Pixel dimensions, rendered as "W × H" when both are known. */
  width?: number;
  height?: number;
  /** Byte size, rendered via bytes(). */
  size?: number;
  /** Diegetic asset id, e.g. "HEPH-GAL-0042". */
  assetId?: string;
}

export interface LightboxProps {
  items: LightboxItem[];
  /** Index of the active item within `items`. */
  index: number;
  open: boolean;
  onClose: () => void;
  onNavigate: (index: number) => void;
}

/**
 * Full-bleed image overlay (design §5/§7 full-screen framing). Presentational:
 * the consumer resolves each item's `src` (at least the active one, via
 * `useAuthedBlobUrl`) and owns the index. Hard-cut over a flat `bg` backdrop —
 * no scrim, blur, or fade (§8). Esc / ArrowLeft / ArrowRight mirror the
 * close / prev / next controls.
 */
export function Lightbox(props: LightboxProps): JSX.Element {
  const [local] = splitProps(props, [
    "items",
    "index",
    "open",
    "onClose",
    "onNavigate",
  ]);

  const total = (): number => local.items.length;
  const active = (): LightboxItem | undefined => local.items[local.index];
  const dims = (): string | undefined => {
    const a = active();
    return a?.width && a?.height ? `${a.width} × ${a.height}` : undefined;
  };
  const sizeLabel = (): string | undefined => {
    const a = active();
    return a?.size != null ? bytes(a.size) : undefined;
  };
  const assetId = (): string | undefined => active()?.assetId;

  const go = (delta: number): void => {
    const t = total();
    if (t <= 1) return;
    local.onNavigate((local.index + delta + t) % t);
  };
  const prev = (): void => go(-1);
  const next = (): void => go(1);

  // Keyboard maps to the on-screen controls while open; torn down on close/cleanup.
  createEffect(() => {
    if (!local.open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") local.onClose();
      else if (e.key === "ArrowLeft") prev();
      else if (e.key === "ArrowRight") next();
    };
    window.addEventListener("keydown", onKey);
    onCleanup(() => window.removeEventListener("keydown", onKey));
  });

  return (
    <Show when={local.open}>
      <Portal>
        <div
          role="dialog"
          aria-modal="true"
          class="fixed inset-0 z-50 flex flex-col bg-bg"
        >
          <RegistrationFrame class="flex min-h-0 flex-1 flex-col">
            <header class="flex items-center justify-between gap-4 px-6 py-4">
              <Text variant="label" tone="bright">
                {active()?.filename ?? "—"}
              </Text>
              <div class="flex items-center gap-4">
                <Text variant="micro" tone="dim">
                  {pad(local.index + 1, 2)} / {pad(total(), 2)}
                </Text>
                <button
                  type="button"
                  onClick={local.onClose}
                  aria-label="Close"
                  class="text-dim transition-colors hover:text-bright"
                >
                  <Icon name="close" size={16} />
                </button>
              </div>
            </header>

            <div class="relative flex min-h-0 flex-1 items-center justify-center px-6">
              <Show when={total() > 1}>
                <button
                  type="button"
                  onClick={prev}
                  aria-label="Previous"
                  class="absolute left-6 z-10 text-dim transition-colors hover:text-bright"
                >
                  <Icon name="chevron-left" size={20} />
                </button>
              </Show>
              <ImageFrame
                src={active()?.src}
                error={active()?.error}
                alt={active()?.filename ?? ""}
                fit="contain"
                class="h-full w-full"
              />
              <Show when={total() > 1}>
                <button
                  type="button"
                  onClick={next}
                  aria-label="Next"
                  class="absolute right-6 z-10 text-dim transition-colors hover:text-bright"
                >
                  <Icon name="chevron-right" size={20} />
                </button>
              </Show>
            </div>

            <footer class="flex items-center gap-4 px-6 py-4">
              <Show when={dims()}>
                {(d) => (
                  <Text variant="micro" tone="dim">
                    {d()}
                  </Text>
                )}
              </Show>
              <Show when={sizeLabel()}>
                {(s) => (
                  <Text variant="micro" tone="dim">
                    {s()}
                  </Text>
                )}
              </Show>
              <Show when={assetId()}>
                {(a) => (
                  <Text variant="micro" tone="dim">
                    {a()}
                  </Text>
                )}
              </Show>
            </footer>
          </RegistrationFrame>
        </div>
      </Portal>
    </Show>
  );
}
