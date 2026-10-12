import { createEffect, onCleanup, onMount, Show, type JSX } from "solid-js";
import {
  Button,
  confirm,
  LoadingText,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  toast,
} from "~/ui";
import { refreshVmState, resetVm, useVmState } from "../data";
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

  return (
    <Panel label="Platform VM">
      <Show when={vm()} fallback={<LoadingText />}>
        {(state) => (
          <Stack gap={3}>
            <Row align="center" justify="between">
              <Stack gap={1}>
                <Text variant="label" tone="default">
                  Status
                </Text>
                <Text variant="micro" tone="dim">
                  {state().detail ??
                    "Runs agent code, previews, web search and web fetch."}
                </Text>
              </Stack>
              <StatusFlag
                dot
                pulse={state().state === "starting"}
                status={vmStateTone(state().state)}
              >
                {vmStateLabel(state().state)}
              </StatusFlag>
            </Row>
            <Row align="center" justify="between">
              <Stack gap={1}>
                <Text variant="label" tone="default">
                  Reset VM
                </Text>
                <Text variant="micro" tone="dim">
                  Rebuilds the VM. Web services restart on their own.
                </Text>
              </Stack>
              <Button
                variant="danger"
                size="sm"
                disabled={state().state === "starting"}
                onClick={() => void reset()}
              >
                Reset VM
              </Button>
            </Row>
          </Stack>
        )}
      </Show>
    </Panel>
  );
}
