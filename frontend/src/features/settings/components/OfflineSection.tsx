import { onCleanup, onMount, Show, type JSX } from "solid-js";
import {
  LoadingText,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  Toggle,
  toast,
} from "~/ui";
import {
  refreshOfflineState,
  setOfflineAutoDetect,
  setOfflineManual,
  useOfflineState,
} from "../data";
import type { OfflineState } from "../model";

/* The backend owns the offline decision (and can flip it on its own when
   connectivity drops), so this is a live read — poll it while the section is open
   so an auto-toggle shows up here. The two switches relay straight to the backend,
   which returns the fresh state. */
export function OfflineSection(): JSX.Element {
  const offline = useOfflineState();
  onMount(() => {
    void refreshOfflineState();
    const id = setInterval(() => void refreshOfflineState(), 10_000);
    onCleanup(() => clearInterval(id));
  });
  const offlineLabel = (s: OfflineState): string =>
    !s.effectiveOffline
      ? "Online"
      : s.manualOffline
        ? "OFFLINE · MANUAL"
        : "OFFLINE · NO CONNECTIVITY";
  const toggleOffline = async (action: () => Promise<void>, label: string) => {
    try {
      await action();
    } catch {
      toast.error(`Couldn't update ${label}.`);
    }
  };

  return (
    <Panel label="Offline mode">
      <Show when={offline()} fallback={<LoadingText />}>
        {(state) => (
          <Stack gap={3}>
            <Row align="center" justify="between">
              <Stack gap={1}>
                <Text variant="label" tone="default">
                  Status
                </Text>
                <Text variant="micro" tone="dim">
                  When connectivity is lost the web search + fetch services are
                  suspended to save resources and the agent's web tools are
                  hidden. They return automatically when you're back online.
                </Text>
              </Stack>
              <StatusFlag
                status={state().effectiveOffline ? "warn" : "nominal"}
              >
                {offlineLabel(state())}
              </StatusFlag>
            </Row>
            <Row align="center" justify="between" class="pt-3">
              <Stack gap={1}>
                <Text variant="label" tone="default">
                  Offline now
                </Text>
                <Text variant="micro" tone="dim">
                  Force offline immediately — stops the web services regardless
                  of connectivity.
                </Text>
              </Stack>
              <Toggle
                checked={state().manualOffline}
                onChange={() =>
                  void toggleOffline(
                    () => setOfflineManual(!state().manualOffline),
                    "offline mode",
                  )
                }
              />
            </Row>
            <Row align="center" justify="between">
              <Stack gap={1}>
                <Text variant="label" tone="default">
                  Auto-detect
                </Text>
                <Text variant="micro" tone="dim">
                  Go offline on its own when the internet connection drops, and
                  come back when it returns.
                </Text>
              </Stack>
              <Toggle
                checked={state().autoDetect}
                onChange={() =>
                  void toggleOffline(
                    () => setOfflineAutoDetect(!state().autoDetect),
                    "auto-detect",
                  )
                }
              />
            </Row>
          </Stack>
        )}
      </Show>
    </Panel>
  );
}
