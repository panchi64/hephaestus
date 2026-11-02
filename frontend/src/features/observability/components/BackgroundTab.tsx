import { createResource, createSignal, For, Show, type JSX } from "solid-js";
import { date, duration, plural, relativeTime, usd } from "~/lib/format";
import { Segmented, Text } from "~/ui";
import { fetchBackground } from "../data";
import { agentLabel } from "../model";
import { CostColumns } from "./CostColumns";

type Days = "7" | "30";

/** What the utility model did across the workspace — titles, triage, summaries,
 *  distills — none of which the chat totals count. */
export function BackgroundTab(): JSX.Element {
  const [days, setDays] = createSignal<Days>("7");
  const [activity] = createResource(() => Number(days()), fetchBackground);

  return (
    <div class="flex flex-col gap-5">
      <div class="flex items-center justify-between gap-4">
        <Text variant="body" tone="dim">
          Work by every agent other than the chat agent, across all threads.
        </Text>
        <Segmented
          aria-label="Period"
          fill={false}
          value={days()}
          onChange={setDays}
          options={[
            { value: "7", label: "7 days" },
            { value: "30", label: "30 days" },
          ]}
        />
      </div>

      <Show when={activity()} fallback={<Text tone="dim">Loading…</Text>}>
        {(a) => (
          <Show
            when={a().agents.length > 0}
            fallback={
              <Text variant="body" tone="dim">
                No background work in this period.
              </Text>
            }
          >
            <Show when={a().perDay.length > 0}>
              <section class="flex flex-col gap-2">
                <Text variant="meta" tone="dim">
                  COST PER DAY
                </Text>
                <CostColumns
                  aria-label="Background cost per day"
                  columns={a().perDay.map((d) => ({
                    key: d.day,
                    title: `${date(d.day)}: ${usd(d.costUsd)}`,
                    totalUsd: d.costUsd,
                  }))}
                />
              </section>
            </Show>

            <section class="flex flex-col gap-1">
              <Text variant="meta" tone="dim">
                BY AGENT
              </Text>
              <table class="w-full text-left">
                <thead>
                  <tr>
                    <For
                      each={["Agent", "Calls", "Avg time", "Cost", "Last run"]}
                    >
                      {(h) => (
                        <th class="py-1 font-normal">
                          <Text variant="micro" tone="dim">
                            {h}
                          </Text>
                        </th>
                      )}
                    </For>
                  </tr>
                </thead>
                <tbody>
                  <For each={a().agents}>
                    {(agent) => (
                      <tr class="border-t border-line">
                        <td class="py-1">
                          <Text variant="label">
                            {agentLabel(agent.agentName)}
                          </Text>
                        </td>
                        <td class="tabular-nums">
                          <Text variant="micro">{agent.calls}</Text>
                        </td>
                        <td class="tabular-nums">
                          <Text variant="micro">
                            {duration(agent.avgDurationMs)}
                          </Text>
                        </td>
                        <td class="tabular-nums">
                          <Text variant="micro" tone="bright">
                            {agent.costUsd !== null ? usd(agent.costUsd) : "—"}
                          </Text>
                        </td>
                        <td>
                          <Text variant="micro" tone="dim">
                            {relativeTime(agent.lastRunAt)}
                          </Text>
                        </td>
                      </tr>
                    )}
                  </For>
                </tbody>
              </table>
            </section>

            <section class="flex flex-col gap-1">
              <Text variant="meta" tone="dim">
                RECENT CALLS
              </Text>
              <ul class="flex flex-col">
                <For each={a().recent}>
                  {(call) => (
                    <li class="flex items-baseline justify-between gap-3 border-t border-line py-1">
                      <Text
                        variant="micro"
                        tone={call.errorType ? "alert" : "default"}
                        class="min-w-0 truncate"
                      >
                        {agentLabel(call.agentName)}
                        <Show when={call.model}> · {call.model}</Show>
                        <Show when={call.errorType}> · {call.errorType}</Show>
                      </Text>
                      <Text
                        variant="micro"
                        tone="dim"
                        class="shrink-0 tabular-nums"
                      >
                        {relativeTime(call.startedAt)} ·{" "}
                        {duration(call.durationMs)}
                        <Show when={call.costUsd !== null}>
                          {" "}
                          · {usd(call.costUsd!)}
                        </Show>
                      </Text>
                    </li>
                  )}
                </For>
              </ul>
              <Text variant="micro" tone="dim">
                {plural(a().recent.length, "call")} shown
              </Text>
            </section>
          </Show>
        )}
      </Show>
    </div>
  );
}
