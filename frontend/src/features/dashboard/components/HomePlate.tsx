import { Show, type JSX } from "solid-js";
import {
  RegistrationFrame,
  StatusFlag,
  Text,
  Tooltip,
  type Status,
} from "~/ui";

export interface HomePlateProps {
  /** `HEPH <version> · <date>`; absent until the overview has loaded. */
  eyebrow?: string;
  greeting: string;
  digest: string;
  /** Shown only for a warning or an alert: a screen at rest carries no hue (§5). */
  status?: { tone: Exclude<Status, "nominal">; label: string; reason?: string };
}

/** The launchpad's opening plate: a real machine line, the greeting, and one sentence
 *  on the state of the workspace. The registration marks bracket this object and
 *  nothing wider (§9). */
export function HomePlate(props: HomePlateProps): JSX.Element {
  return (
    <RegistrationFrame>
      {/* `px-4` puts the text on the composer's own text edge; the marks clear it
          vertically, sitting above the eyebrow and below the digest. */}
      <div class="flex items-end justify-between gap-6 px-4 py-5">
        <div class="flex min-w-0 flex-col gap-2">
          {/* The line is reserved while it loads, so the greeting never shifts. */}
          <Text variant="meta" tone="dim" class="min-h-4">
            {props.eyebrow ?? ""}
          </Text>
          <Text variant="readout-lg" tone="bright" as="h1">
            {props.greeting}
          </Text>
          <Text variant="body" tone="dim">
            {props.digest}
          </Text>
        </div>
        <Show when={props.status}>
          {(s) => (
            <Show
              when={s().reason}
              fallback={
                <StatusFlag status={s().tone} dot>
                  {s().label}
                </StatusFlag>
              }
            >
              {(reason) => (
                <Tooltip label={reason()} side="left">
                  <StatusFlag status={s().tone} dot>
                    {s().label}
                  </StatusFlag>
                </Tooltip>
              )}
            </Show>
          )}
        </Show>
      </div>
    </RegistrationFrame>
  );
}
