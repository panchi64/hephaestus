import { createSignal, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  confirm,
  Input,
  LoadingText,
  Modal,
  PageHeader,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  toast,
} from "~/ui";
import { clearCredential, setCredential, useCredentials } from "../data";
import type { ServiceCredential } from "../model";

/** Service Keys — the operator's keys for the outbound services the system calls;
 *  today, the mail OAuth clients. One row per service the backend declares; the key is
 *  write-only (the API reports only whether one is set), sealed at rest. This is
 *  configuration, not inbound auth — the tokens clients call *this* API with are the
 *  Access Tokens section (`features/access-tokens`). */
export function ApiTokensScreen(): JSX.Element {
  const credentials = useCredentials();

  const [editing, setEditing] = createSignal<ServiceCredential | null>(null);
  const [apiKey, setApiKey] = createSignal("");
  const [saving, setSaving] = createSignal(false);

  const openSet = (credential: ServiceCredential) => {
    setEditing(credential);
    setApiKey("");
  };
  const close = () => {
    setEditing(null);
    setApiKey("");
    setSaving(false);
  };

  const save = async () => {
    const target = editing();
    if (!target || !apiKey().trim() || saving()) return;
    setSaving(true);
    try {
      await setCredential(target.service, apiKey().trim());
      toast.success(`${target.label} key saved`);
      close();
    } catch {
      toast.error(`Unable to save the ${target.label} key.`);
      setSaving(false);
    }
  };

  const remove = async (credential: ServiceCredential) => {
    if (
      !(await confirm({
        title: `Remove the ${credential.label} key?`,
        detail:
          "Features using this service fall back to their default (or degrade) until a new key is set.",
        confirmLabel: "Remove",
        tone: "alert",
      }))
    )
      return;
    try {
      await clearCredential(credential.service);
      toast.success(`${credential.label} key removed`);
    } catch {
      toast.error(`Unable to remove the ${credential.label} key.`);
    }
  };

  return (
    <Stack gap={6}>
      <PageHeader
        variant="section"
        title="Service keys"
        subtitle="Outbound — keys this system uses to reach third-party services. Stored encrypted at rest; never displayed."
        assetId="HEPH-ADM-04.0 EDITION 02"
      />

      <Panel label="Service credentials" flush>
        <Show
          when={settled(credentials)}
          fallback={
            <div class="p-3">
              <LoadingText />
            </div>
          }
        >
          <For each={settled(credentials) ?? []}>
            {(credential) => (
              <Row align="center" justify="between" gap={3} class="px-3 py-3">
                <Stack gap={1} class="min-w-0">
                  <Row gap={2} align="center">
                    <Text variant="label" tone="bright">
                      {credential.label}
                    </Text>
                    <Show
                      when={credential.hasKey}
                      fallback={<StatusFlag status="idle">No key</StatusFlag>}
                    >
                      <StatusFlag status="nominal">Key set</StatusFlag>
                    </Show>
                  </Row>
                  <Text variant="micro" tone="dim">
                    {credential.purpose}
                  </Text>
                </Stack>
                <span class="flex shrink-0 items-center gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      window.open(credential.docsUrl, "_blank", "noopener")
                    }
                  >
                    GET KEY ↗
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    leading="key"
                    onClick={() => openSet(credential)}
                  >
                    {credential.hasKey ? "Update" : "Set key"}
                  </Button>
                  <Show when={credential.hasKey}>
                    <Button
                      variant="ghost"
                      size="sm"
                      leading="trash"
                      onClick={() => remove(credential)}
                    >
                      Clear
                    </Button>
                  </Show>
                </span>
              </Row>
            )}
          </For>
        </Show>
      </Panel>

      <Modal
        open={editing() !== null}
        onClose={close}
        title={editing() ? `${editing()!.label} — API KEY` : ""}
        class="max-w-lg"
      >
        <Stack gap={3}>
          <Text variant="micro" tone="dim">
            {editing()?.purpose}
          </Text>
          <Input
            label={
              editing()?.hasKey
                ? "API KEY (replaces the stored key)"
                : "API key"
            }
            type="password"
            value={apiKey()}
            onInput={(e) => setApiKey(e.currentTarget.value)}
            placeholder="••••••••"
            hint="Sealed at rest and never shown again after saving."
          />
          <div class="flex justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={!apiKey().trim() || saving()}
              onClick={save}
            >
              {saving() ? "Saving…" : "Save"}
            </Button>
          </div>
        </Stack>
      </Modal>
    </Stack>
  );
}
