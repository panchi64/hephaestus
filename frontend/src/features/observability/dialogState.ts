import { createSignal } from "solid-js";

export type StatsTab = "overview" | "turns" | "background";

/** Which turn the Turns tab opens on: a run id when the client knows it (a turn that
 *  streamed here), else the instant a saved message was written. Neither picks the newest. */
export interface TurnTarget {
  runId: string | null;
  at: string | null;
}

export interface StatsDialogState extends TurnTarget {
  tab: StatsTab;
}

const NO_TARGET: TurnTarget = { runId: null, at: null };

/** Module-level because what opens the dialog (the composer's `Stats` cell, a work
 *  log's `Trace`) and the one mounted dialog are far apart in the tree. */
const [state, setState] = createSignal<StatsDialogState | null>(null);

export const statsDialog = state;

export function openStatsDialog(
  tab: StatsTab,
  target: TurnTarget = NO_TARGET,
): void {
  setState({ tab, ...target });
}

export function closeStatsDialog(): void {
  setState(null);
}

/** Switch tab, keeping the turn target unless a run is named. */
export function selectStatsTab(
  tab: StatsTab,
  runId: string | null = null,
): void {
  setState((prev) =>
    prev ? { ...prev, tab, ...(runId ? { runId, at: null } : {}) } : prev,
  );
}
