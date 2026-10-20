import { createEffect, onCleanup, onMount, Show, type JSX } from "solid-js";
import {
  Button,
  confirm,
  LoadingText,
  Panel,
  Stack,
  StatusFlag,
  toast,
} from "~/ui";
import { SettingRow } from "./SettingRow";
import {
  forgetBrowserProfile,
  refreshVmState,
  resetVm,
  useVmState,
} from "../data";
import { vmNeedsPolling, vmStateLabel, vmStateTone } from "../vmDto";

const POLL_MS = 3_000;

/* The backend owns the VM's lifecycle, so this is a live read: one fetch on open, then
   a poll only while the VM is starting. */
export function PlatformVmSection(): JSX.Element {
  const vm = useVmState();
  onMount(() => void refreshVmState());
  createEffect(() => {
    if (!vmNeedsPolling(vm()?.state)) return;
    const id = setInterval(() => void refreshVmState(), POLL_MS);
    onCleanup(() => clearInterval(id));
  });

  const reset = async (): Promise<void> => {
    const ok = await confirm({
      title: "Reset the platform VM?",
      detail:
        "Running commands are interrupted; nothing is lost, since workspaces are restored on next use.",
      confirmLabel: "Reset VM",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await resetVm();
    } catch {
      toast.error("Couldn't reset the VM.");
    }
  };

  const forgetLogins = async (): Promise<void> => {
    const ok = await confirm({
      title: "Log the agent's browser out of every site?",
      detail:
        "Every conversation's browser closes and the shared logins are deleted. You will need to log in again.",
      confirmLabel: "Log out everywhere",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await forgetBrowserProfile();
      toast.success("The agent's browser is logged out of every site.");
    } catch {
      toast.error("Couldn't clear the browser's logins.");
    }
  };

  return (
    <Panel label="Platform VM">
      <Show when={vm()} fallback={<LoadingText />}>
        {(state) => (
          <Stack gap={3}>
            <SettingRow
              label="Status"
              description={
                state().detail ??
                "Runs agent code, previews, web search and web fetch."
              }
            >
              <StatusFlag
                dot
                pulse={state().state === "starting"}
                status={vmStateTone(state().state)}
              >
                {vmStateLabel(state().state)}
              </StatusFlag>
            </SettingRow>
            <SettingRow
              label="Reset VM"
              description="Rebuilds the VM. Web services restart on their own."
            >
              <Button
                variant="danger"
                size="sm"
                disabled={state().state === "starting"}
                onClick={() => void reset()}
              >
                Reset VM
              </Button>
            </SettingRow>
            <SettingRow
              label="Browser logins"
              description="One profile, shared by every conversation's browser."
            >
              <Button
                variant="danger"
                size="sm"
                onClick={() => void forgetLogins()}
              >
                Log out everywhere
              </Button>
            </SettingRow>
          </Stack>
        )}
      </Show>
    </Panel>
  );
}
