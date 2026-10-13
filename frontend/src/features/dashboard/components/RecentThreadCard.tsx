import { Show, type JSX } from "solid-js";
import { StatusFlag, Text, cx } from "~/ui";
import { relativeTime } from "~/lib/format";

export interface RecentThreadCardProps {
  title: string;
  preview?: string;
  model?: string;
  updatedAt: string;
  /** The resume target — the newest still-warm thread. */
  warm?: boolean;
  onOpen: () => void;
}

/** A recent-conversation row for the overview launchpad: square, bare at rest, a
 *  flat raised fill on hover. The warm thread — "where was I" — takes the
 *  console's selection (raised fill plus a 2px bright inset edge, as a selected
 *  rail row) and a RESUME flag. */
export function RecentThreadCard(props: RecentThreadCardProps): JSX.Element {
  return (
    <button
      type="button"
      onClick={() => props.onOpen()}
      /* Square, no border and no fill at rest — these sit *behind* the interface.
         Hover is a flat raised fill and nothing else (no radius, outline or
         tint). `warm` (the resumable thread) is the console's selection. */
      class={cx(
        "flex w-full flex-col gap-1 p-3 text-left outline-none hover:bg-raised focus-visible:bg-raised",
        props.warm && "heph-selected-edge bg-raised",
      )}
    >
      <div class="flex items-center justify-between gap-2">
        <Text
          variant="label"
          tone={props.warm ? "bright" : "default"}
          class="truncate"
        >
          {props.title}
        </Text>
        <Show
          when={props.warm}
          fallback={
            <Text variant="micro" tone="dim" class="shrink-0">
              {relativeTime(props.updatedAt)}
            </Text>
          }
        >
          <StatusFlag status="info">Resume</StatusFlag>
        </Show>
      </div>
      <Show when={props.preview}>
        <Text variant="micro" tone="dim" class="truncate">
          {props.preview}
        </Text>
      </Show>
      <div class="flex items-center gap-2">
        <Show when={props.model}>
          <Text variant="micro" tone="dim">
            {props.model}
          </Text>
        </Show>
        <Show when={props.warm}>
          <Text variant="micro" tone="dim">
            {relativeTime(props.updatedAt)}
          </Text>
        </Show>
      </div>
    </button>
  );
}
