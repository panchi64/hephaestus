import { For, Show, type JSX, type Resource as SolidResource } from "solid-js";
import { Dynamic } from "solid-js/web";
import { Marquee, Resource, StatusDot, Text, Tooltip } from "~/ui";
import type { CapabilityHealth, Overview, SystemStat } from "../model";

/** Ambient facts and a nominal count, then only the degraded capabilities, so the
 *  footer is grayscale unless something is wrong (§10.15). */
export function SystemFooter(props: {
  overview: SolidResource<Overview>;
  /** The degraded capabilities, alerts first, as the screen already derived them. */
  degraded: CapabilityHealth[];
  onRetry: () => void;
  band: (o: Overview) => SystemStat[];
}): JSX.Element {
  return (
    <footer class="px-4">
      <Resource
        data={props.overview}
        onRetry={props.onRetry}
        errorMessage="Telemetry unavailable"
        loading={<span class="block h-4" />}
      >
        {(o) => {
          const lit = () => props.degraded;
          return (
            <div class="flex flex-col gap-2">
              <div class="flex min-w-0 items-center justify-between gap-6">
                <Marquee class="min-w-0 flex-1" speed={32}>
                  <div class="flex items-center gap-4">
                    <For each={props.band(o())}>
                      {(stat) => (
                        <Text
                          variant="micro"
                          tone="dim"
                          class="whitespace-nowrap"
                        >
                          {`${stat.label} ${stat.value}`}
                        </Text>
                      )}
                    </For>
                  </div>
                </Marquee>
                <Text variant="micro" tone="dim" class="shrink-0 uppercase">
                  {`${o().capabilities.length - lit().length} of ${o().capabilities.length} nominal`}
                </Text>
              </div>
              {/* Each degraded capability is a name, its reason on hover and its
                  remediation on click: a wall of reasons would outweigh the page. */}
              <Show when={lit().length}>
                <ul class="flex flex-wrap gap-x-4 gap-y-1">
                  <For each={lit()}>
                    {(cap) => (
                      <li>
                        <Tooltip label={cap.detail}>
                          <Dynamic
                            component={cap.remediationHref ? "a" : "span"}
                            href={cap.remediationHref}
                            class="flex items-center gap-2"
                          >
                            <StatusDot status={cap.status} />
                            <Text
                              variant="micro"
                              tone={cap.status === "alert" ? "alert" : "warn"}
                              class="uppercase"
                            >
                              {cap.label}
                            </Text>
                          </Dynamic>
                        </Tooltip>
                      </li>
                    )}
                  </For>
                </ul>
              </Show>
            </div>
          );
        }}
      </Resource>
    </footer>
  );
}
