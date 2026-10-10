import { For, Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Icon } from "../primitives/Icon";

export interface RegistrationFrameProps {
  /** Show the corner registration marks. Default true. */
  corners?: boolean;
  /** Which marks. `cross` (default) sets a `+` just inside each corner — framing for a
   *  full-screen layout. `tick` sets an L-shaped 8px tick 6px *outside* each corner,
   *  hugging the box's own edges: framing for one bordered unit on a page, where marks
   *  inside it would sit on its content. */
  marks?: "cross" | "tick";
  /** The marks' tone. `dim` at rest; `info` while the framed thing is live — the same
   *  blue the live rail uses for a streaming run, so the frame joins that state rather
   *  than announcing a second vocabulary for it. Default `dim`. */
  tone?: "dim" | "info";
  /** Optional diegetic ID printed bottom-left (e.g. "RCM-OB-01.3"). */
  assetId?: string;
  class?: string;
  children: JSX.Element;
}

/* One tick per corner: its position outside the box, and the two edges it draws. */
const TICKS = [
  "-left-1.5 -top-1.5 border-l border-t",
  "-right-1.5 -top-1.5 border-r border-t",
  "-bottom-1.5 -left-1.5 border-b border-l",
  "-bottom-1.5 -right-1.5 border-b border-r",
];

const CROSSES = [
  "left-1 top-1",
  "right-1 top-1",
  "bottom-1 left-1",
  "bottom-1 right-1",
];

/** Diegetic framing device: corner registration marks (§5/§7). Non-interactive
 *  atmosphere — the marks never take a pointer event. */
export function RegistrationFrame(props: RegistrationFrameProps): JSX.Element {
  const [local] = splitProps(props, [
    "corners",
    "marks",
    "tone",
    "assetId",
    "class",
    "children",
  ]);
  const showCorners = () => local.corners ?? true;
  const toneClass = () => (local.tone === "info" ? "text-info" : "text-dim");
  return (
    <div class={cx("relative", local.class)}>
      <Show when={showCorners()}>
        <Show
          when={local.marks === "tick"}
          fallback={
            <For each={CROSSES}>
              {(at) => (
                <Icon
                  name="cross"
                  size={10}
                  class={cx("pointer-events-none absolute", at, toneClass())}
                />
              )}
            </For>
          }
        >
          {/* `border-current`, so the tone is one `text-*` class for both variants —
              machine state, so it snaps rather than easing (§8). */}
          <For each={TICKS}>
            {(at) => (
              <span
                aria-hidden="true"
                class={cx(
                  "pointer-events-none absolute size-2 border-current transition-none",
                  at,
                  toneClass(),
                )}
              />
            )}
          </For>
        </Show>
      </Show>
      <Show when={local.assetId}>
        <span class="pointer-events-none absolute bottom-1 left-1/2 -translate-x-1/2 text-micro uppercase tracking-label text-dim">
          {local.assetId}
        </span>
      </Show>
      {local.children}
    </div>
  );
}
