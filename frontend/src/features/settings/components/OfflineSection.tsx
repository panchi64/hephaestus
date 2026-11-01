import { onCleanup, onMount, Show, type JSX } from "solid-js";
import { LoadingText, Panel, Stack, StatusFlag, Toggle, toast } from "~/ui";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { SettingRow } from "./SettingRow";
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
    <Stack gap={6}>
      <SettingsPageHeader section="offline" />
      <Panel>
        <Show when={offline()} fallback={<LoadingText />}>
          {(state) => (
            <Stack gap={3}>
              <SettingRow
                label="Status"
                description="When connectivity is lost the web search + fetch services are suspended to save resources and the agent's web tools are hidden. They return automatically when you're back online."
              >
                <StatusFlag
                  status={state().effectiveOffline ? "warn" : "nominal"}
                >
                  {offlineLabel(state())}
                </StatusFlag>
              </SettingRow>
              <SettingRow
                label="Offline now"
                description="Force offline immediately — stops the web services regardless of connectivity."
                class="pt-3"
              >
                <Toggle
                  checked={state().manualOffline}
                  onChange={() =>
                    void toggleOffline(
                      () => setOfflineManual(!state().manualOffline),
                      "offline mode",
                    )
                  }
                />
              </SettingRow>
              <SettingRow
                label="Auto-detect"
                description="Go offline on its own when the internet connection drops, and come back when it returns."
              >
                <Toggle
                  checked={state().autoDetect}
                  onChange={() =>
                    void toggleOffline(
                      () => setOfflineAutoDetect(!state().autoDetect),
                      "auto-detect",
                    )
                  }
                />
              </SettingRow>
            </Stack>
          )}
        </Show>
      </Panel>
    </Stack>
  );
}
