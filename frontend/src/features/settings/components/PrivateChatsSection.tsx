import { Show, onMount, type JSX } from "solid-js";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import {
  lockPrivate,
  pinSet,
  privateStatus,
  privateUnlocked,
  refreshPrivateStatus,
  requestPin,
  setRelockMinutes,
} from "~/features/private";
import { Button, LoadingText, Panel, Stack, StatusFlag, toast } from "~/ui";
import { NumberSetting } from "./NumberSetting";
import { SettingRow } from "./SettingRow";

const RELOCK_MAX = 720;

export function PrivateChatsSection(): JSX.Element {
  onMount(
    () =>
      void refreshPrivateStatus().catch(() =>
        toast.error("Couldn't read the Private area's status."),
      ),
  );

  const lockNow = async (): Promise<void> => {
    try {
      await lockPrivate();
    } catch {
      toast.error("Couldn't lock private chats.");
    }
  };

  return (
    <Stack gap={6}>
      <SettingsPageHeader section="private-chats" />
      <Panel>
        <Show when={privateStatus()} fallback={<LoadingText />}>
          <Stack gap={3}>
            <SettingRow
              label="Status"
              description="Locked, private chats are hidden from the sidebar and from every agent. Open, an agent may find one, but each read asks you first."
            >
              <StatusFlag status={privateUnlocked() ? "warn" : "idle"} dot>
                {privateUnlocked() ? "OPEN" : "LOCKED"}
              </StatusFlag>
            </SettingRow>
            <SettingRow
              label="PIN"
              description="Opens the Private area. Forgotten, it can be reset with your account password."
              class="pt-3"
            >
              <Button
                onClick={() => void requestPin(pinSet() ? "change" : "set")}
              >
                {pinSet() ? "Change PIN" : "Set PIN"}
              </Button>
            </SettingRow>
            <SettingRow
              label="Lock now"
              description="Shut the area before it locks on its own."
            >
              <Button
                disabled={!privateUnlocked()}
                onClick={() => void lockNow()}
              >
                Lock
              </Button>
            </SettingRow>
          </Stack>
        </Show>
      </Panel>
      <Panel>
        <NumberSetting
          title="Lock after"
          stored={privateStatus()?.relock_minutes ?? null}
          placeholder="15"
          min={1}
          max={RELOCK_MAX}
          invalid={`Enter a number of minutes from 1 to ${RELOCK_MAX}.`}
          noun="the relock time"
          save={async (minutes) => {
            await setRelockMinutes(minutes);
            return privateStatus()?.relock_minutes ?? null;
          }}
        >
          Minutes with no private chat opened or read before the area locks
          itself. It also locks when you lock the app or sign out.
        </NumberSetting>
      </Panel>
    </Stack>
  );
}
