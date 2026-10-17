import {
  createEffect,
  createSignal,
  on,
  Show,
  splitProps,
  type JSX,
} from "solid-js";
import { cx } from "../cx";
import { Text } from "../primitives/Text";
import { LoadingText } from "./LoadingText";

export interface ImageFrameProps {
  /** Already-resolved image source (e.g. a blob URL). Undefined while it resolves. */
  src: string | undefined;
  alt: string;
  /** cover crops to fill, contain letterboxes, natural sizes the frame to the image
   *  within the `max-h-*`/`max-w-*` the class sets. Default cover. */
  fit?: "cover" | "contain" | "natural";
  /** Force the LOADING state even with a src present (e.g. mid-refetch). */
  loading?: boolean;
  /** The upstream fetch failed (the auth'd blob couldn't resolve — deleted upload,
   *  5xx, expired auth). Renders NO DATA instead of holding LOADING… forever. */
  error?: boolean;
  /** Layout / sizing glue only — never colors or type. */
  class?: string;
}

/**
 * Presentational image cell: a square hairline frame over `surface`. Pure — it
 * receives an already-resolved `src` (the consumer wires the fetch/auth, e.g.
 * via `useAuthedBlobUrl`). While loading or src-less it shows LOADING…; an
 * upstream fetch `error` or a decode/load failure shows NO DATA. No spinner, no
 * shadow, no fade (design §6 states, §8 motion).
 */
export function ImageFrame(props: ImageFrameProps): JSX.Element {
  const [local] = splitProps(props, [
    "src",
    "alt",
    "fit",
    "loading",
    "error",
    "class",
  ]);
  const [errored, setErrored] = createSignal(false);

  // A new src is a fresh attempt — clear any prior decode error.
  createEffect(
    on(
      () => local.src,
      () => setErrored(false),
    ),
  );

  // Failure (upstream fetch or img decode) terminates the LOADING state, so a
  // resource that errors out never leaves the frame stuck mid-load.
  const failed = (): boolean => local.error === true || errored();

  return (
    <div
      class={cx(
        "relative flex items-center justify-center overflow-hidden border border-line bg-surface",
        local.class,
      )}
    >
      <Show
        when={!failed()}
        fallback={
          <Text variant="micro" tone="dim">
            No data
          </Text>
        }
      >
        <Show when={!local.loading && local.src} fallback={<LoadingText />}>
          {(src) => (
            <img
              src={src()}
              alt={local.alt}
              class={
                local.fit === "natural"
                  ? "block max-h-[inherit] max-w-full"
                  : cx(
                      "h-full w-full",
                      local.fit === "contain"
                        ? "object-contain"
                        : "object-cover",
                    )
              }
              onError={() => setErrored(true)}
            />
          )}
        </Show>
      </Show>
    </div>
  );
}
