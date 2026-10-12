import { createSignal, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  Input,
  Panel,
  Resource,
  Row,
  Stack,
  StatusFlag,
  Text,
  confirm,
  toast,
} from "~/ui";
import {
  deletePluginSecret,
  pluginErrorMessage,
  refreshSecrets,
  setPluginSecret,
  usePluginSecrets,
} from "../data";
import {
  currentManifest,
  secretRows,
  type Plugin,
  type SecretRow,
} from "../model";

function SetForm(props: {
  pluginId: string;
  row: SecretRow;
  onDone: () => void;
}): JSX.Element {
  const [value, setValue] = createSignal("");
  const [host, setHost] = createSignal("");
  const [busy, setBusy] = createSignal(false);

  async function save(): Promise<void> {
    if (!value() || busy()) return;
    setBusy(true);
    try {
      await setPluginSecret(
        props.pluginId,
        props.row.name,
        value(),
        host().trim() || undefined,
      );
      toast.success(`${props.row.name} saved`);
      props.onDone();
    } catch (err) {
      toast.error(pluginErrorMessage(err, `Could not save ${props.row.name}.`));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      class="flex flex-col gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <Input
        type="password"
        autocomplete="off"
        label="Value"
        value={value()}
        onInput={(e) => setValue(e.currentTarget.value)}
      />
      <Input
        label="Host override (optional)"
        placeholder={props.row.declaredHost ?? props.row.host}
        hint="Defaults to the host the plugin declares."
        value={host()}
        onInput={(e) => setHost(e.currentTarget.value)}
      />
      <Row gap={2}>
        <Button
          type="submit"
          variant="primary"
          size="sm"
          leading="check"
          disabled={!value() || busy()}
        >
          Save
        </Button>
        <Button variant="ghost" size="sm" onClick={props.onDone}>
          Cancel
        </Button>
      </Row>
    </form>
  );
}

/** Each secret the plugin declares, the host it is sent to, and whether a value
 *  is stored. Values are write-only. */
export function PluginSecrets(props: { plugin: Plugin }): JSX.Element {
  const stored = usePluginSecrets(() => props.plugin.id);
  const [editing, setEditing] = createSignal<string | null>(null);

  const rows = (): SecretRow[] =>
    secretRows(
      currentManifest(props.plugin)?.secrets ?? {},
      settled(stored) ?? [],
    );

  async function remove(row: SecretRow): Promise<void> {
    const ok = await confirm({
      title: `Remove ${row.name}?`,
      detail:
        "The stored value is deleted. The plugin cannot use it until you set it again.",
      confirmLabel: "Remove",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await deletePluginSecret(props.plugin.id, row.name);
      toast.success(`${row.name} removed`);
    } catch (err) {
      toast.error(pluginErrorMessage(err, `Could not remove ${row.name}.`));
    }
  }

  return (
    <Panel label="Secrets">
      <Resource
        data={stored}
        loadingLabel="Loading secrets"
        errorMessage="Could not load the secrets"
        onRetry={refreshSecrets}
        isEmpty={() => rows().length === 0}
        emptyMessage="No secrets"
        emptyHint="This plugin declares no secrets."
      >
        {() => (
          <Stack gap={4}>
            <For each={rows()}>
              {(row) => (
                <Stack gap={2}>
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <Stack gap={1}>
                      <Row gap={2} align="center" wrap>
                        <Text variant="code" tone="bright">
                          {row.name}
                        </Text>
                        <StatusFlag status={row.isSet ? "nominal" : "warn"}>
                          {row.isSet ? "SET" : "NOT SET"}
                        </StatusFlag>
                        <Show when={row.declaredHost === null}>
                          <Text variant="micro" tone="dim">
                            NOT DECLARED
                          </Text>
                        </Show>
                        <Show when={row.overridden}>
                          <Text variant="micro" tone="dim">
                            HOST OVERRIDDEN
                          </Text>
                        </Show>
                      </Row>
                      <Text variant="meta" tone="dim">
                        Sent to {row.host}
                      </Text>
                    </Stack>
                    <Show when={editing() !== row.name}>
                      <Row gap={2}>
                        <Button
                          variant="default"
                          size="sm"
                          leading="key"
                          onClick={() => setEditing(row.name)}
                        >
                          {row.isSet ? "Replace" : "Set"}
                        </Button>
                        <Show when={row.isSet}>
                          <Button
                            variant="ghost"
                            size="sm"
                            leading="trash"
                            onClick={() => void remove(row)}
                          >
                            Remove
                          </Button>
                        </Show>
                      </Row>
                    </Show>
                  </div>
                  <Show when={editing() === row.name}>
                    <SetForm
                      pluginId={props.plugin.id}
                      row={row}
                      onDone={() => setEditing(null)}
                    />
                  </Show>
                </Stack>
              )}
            </For>
          </Stack>
        )}
      </Resource>
    </Panel>
  );
}
