import { For, Show, splitProps, type JSX } from "solid-js";
import { cx } from "../cx";
import { Icon } from "../primitives/Icon";

export interface RegistrationFrameProps {
  /** Show the corner registration marks — a `+` just inside each corner. Default true. */
  corners?: boolean;
  /** The marks' tone. `dim` at rest; `info` while the framed thing is live — the same
   *  blue the live rail uses for a streaming run, so the frame joins that state rather
   *  than announcing a second vocabulary for it. Default `dim`. */
  tone?: "dim" | "info";
  /** Optional diegetic ID printed bottom-left (e.g. "RCM-OB-01.3"). */
  assetId?: string;
  class?: string;
  children: JSX.Element;
}

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
        <For each={CROSSES}>
          {(at) => (
            <Icon
              name="cross"
              size={10}
              class={cx("pointer-events-none absolute", at, toneClass())}
            />
          )}
        </For>
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
