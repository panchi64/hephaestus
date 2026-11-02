import { Show, type JSX } from "solid-js";
import { Button, Drawer, Field, Stack, StatusFlag, Text } from "~/ui";
import { timestamp } from "~/lib/format";
import { useSettingsRoute } from "~/app/settings-dialog/useSettingsRoute";
import type { ServiceHealth } from "../model";
import { healthFlag } from "./status";

/** One service's backend-decided status, and where to fix it when there is a fix. */
export function ServiceDrawer(props: {
  svc: ServiceHealth | null;
  onClose: () => void;
}): JSX.Element {
  const settings = useSettingsRoute();

  return (
    <Drawer
      open={props.svc !== null}
      onClose={props.onClose}
      title={props.svc?.label ?? ""}
    >
      <Show when={props.svc}>
        {(svc) => (
          <Stack gap={4}>
            <StatusFlag status={healthFlag[svc().status]} dot>
              {svc().status.toUpperCase()}
            </StatusFlag>
            <Text variant="body">{svc().detail}</Text>
            <Show when={svc().checkedAt}>
              {(at) => <Field label="Last tested" value={timestamp(at())} />}
            </Show>
            <Show when={svc().critical}>
              <Text variant="caption" tone="dim">
                The workspace cannot run a turn without this.
              </Text>
            </Show>
            <Show when={svc().remediation}>
              {(fix) => (
                <Button
                  variant="default"
                  trailing="arrow-right"
                  onClick={() => {
                    props.onClose();
                    settings.select(fix().section);
                  }}
                >
                  {fix().label}
                </Button>
              )}
            </Show>
          </Stack>
        )}
      </Show>
    </Drawer>
  );
}
