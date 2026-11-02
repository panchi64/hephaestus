import {
  createMemo,
  createResource,
  Match,
  Show,
  Switch,
  type JSX,
} from "solid-js";
import { settled } from "~/lib/resource";
import { FramedOverlay, Icon, Panel, Tabs, Text } from "~/ui";
import type { ConversationStats } from "~/features/chat/model";
import { fetchConversationTurns } from "../data";
import type { TurnSummary } from "../model";
import {
  closeStatsDialog,
  selectStatsTab,
  statsDialog,
  type StatsTab,
} from "../dialogState";
import { BackgroundTab } from "./BackgroundTab";
import { OverviewTab } from "./OverviewTab";
import { TurnsTab } from "./TurnsTab";

const TITLE_ID = "stats-dialog-title";

const TABS: { value: StatsTab; label: string }[] = [
  { value: "overview", label: "Overview" },
  { value: "turns", label: "Turns" },
  { value: "background", label: "Background" },
];

/** The conversation's figures, each turn's call trace, and the workspace's background
 *  work, in one overlay. Mounted once beside the composer; `openStatsDialog` opens it
 *  from wherever the operator asked. */
export function StatsDialog(props: {
  conversationId: () => string | null;
  stats: () => ConversationStats | null | undefined;
  /** The run streaming in this thread right now, so its turn is listed before saved. */
  liveRunId: () => string | null;
  grantsRevalidate: () => unknown;
}): JSX.Element {
  // Open, not the whole dialog state, so switching tab or turn doesn't refetch.
  const open = createMemo(() => statsDialog() !== null);
  // Keyed on the stats too: a turn's rollup lands when its run reports its metrics.
  const [turnsResource] = createResource(
    () => {
      const id = props.conversationId();
      return open() && id ? { id, stats: props.stats() } : null;
    },
    ({ id }) => fetchConversationTurns(id),
  );
  const turns = (): TurnSummary[] => settled(turnsResource) ?? [];
  const tab = (): StatsTab => statsDialog()?.tab ?? "overview";

  return (
    <FramedOverlay
      open={open()}
      onClose={closeStatsDialog}
      labelledBy={TITLE_ID}
      class="h-[80vh] max-w-5xl"
    >
      <Panel bare flush fill class="h-full">
        <div class="flex h-full min-h-0 flex-col">
          <header class="flex items-start justify-between gap-4 px-5 pt-4 pb-1">
            <span id={TITLE_ID}>
              <Text variant="meta" tone="dim">
                STATS · THIS CONVERSATION
              </Text>
            </span>
            <button
              type="button"
              onClick={closeStatsDialog}
              aria-label="Close stats"
              class="text-dim transition-colors hover:text-bright"
            >
              <Icon name="close" size={16} />
            </button>
          </header>
          <Tabs
            items={TABS}
            value={tab()}
            onChange={(value) => {
              const next = TABS.find((t) => t.value === value);
              if (next) selectStatsTab(next.value);
            }}
            fill
            gutter="md"
          />
          <div class="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-5 pt-4 pb-5">
            <Switch>
              <Match when={tab() === "overview"}>
                <OverviewTab
                  conversationId={props.conversationId}
                  stats={props.stats() ?? null}
                  turns={turns()}
                  grantsRevalidate={props.grantsRevalidate}
                  onOpenTurn={(runId) => selectStatsTab("turns", runId)}
                />
              </Match>
              <Match when={tab() === "turns"}>
                <Show
                  when={props.conversationId()}
                  fallback={
                    <Text variant="body" tone="dim">
                      Start the conversation to see its turns.
                    </Text>
                  }
                >
                  <TurnsTab
                    turns={turns()}
                    liveRunId={props.liveRunId()}
                    target={{
                      runId: statsDialog()?.runId ?? null,
                      at: statsDialog()?.at ?? null,
                    }}
                    onSelect={(runId) => selectStatsTab("turns", runId)}
                  />
                </Show>
              </Match>
              <Match when={tab() === "background"}>
                <BackgroundTab />
              </Match>
            </Switch>
          </div>
        </div>
      </Panel>
    </FramedOverlay>
  );
}
