import { For, Show, type JSX } from "solid-js";
import { ConsoleGroup, Frames, Text } from "~/ui";

export interface StatusLine {
  label: string;
  value: JSX.Element;
  /** A probe still running: the value slot shows the throbber beside it. */
  live?: boolean;
  tone?: "dim" | "nominal" | "alert";
  /** A control at the line's end (Edit, Retest). */
  action?: JSX.Element;
}

/** The step's machine voice: one mono line per probe that actually ran, and nothing for
 *  one that hasn't. Values snap; there is no transition on anything here. */
export function StatusBlock(props: {
  label: string;
  lines: StatusLine[];
}): JSX.Element {
  return (
    <Show when={props.lines.length > 0}>
      <ConsoleGroup label={props.label} flush>
        <div class="divide-y divide-line">
          <For each={props.lines}>
            {(line) => (
              <div class="flex min-w-0 items-center gap-3 px-2 py-1.5">
                <Text
                  variant="micro"
                  tone="dim"
                  class="shrink-0 font-mono uppercase"
                >
                  {line.label}
                </Text>
                <Text
                  variant="micro"
                  tone={line.tone ?? "dim"}
                  class="ml-auto min-w-0 truncate text-right font-mono uppercase"
                >
                  {line.value}
                </Text>
                <Show when={line.live}>
                  <Frames />
                </Show>
                {line.action}
              </div>
            )}
          </For>
        </div>
      </ConsoleGroup>
    </Show>
  );
}
