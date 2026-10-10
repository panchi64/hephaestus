import { createSignal, For, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  EmptyState,
  LoadingText,
  Panel,
  Stack,
  confirm,
  toast,
} from "~/ui";
import type { ModelEndpoint } from "~/lib/stores/models";
import {
  deleteEndpoint,
  setEndpointEnabled,
  testEndpoint,
  useEndpoints,
  useProviders,
} from "../data";
import { useEndpointDiscovery } from "../useEndpointDiscovery";
import { EndpointFormDialog } from "./EndpointFormDialog";
import { EndpointRow } from "./EndpointRow";

export function EndpointsSection(): JSX.Element {
  const endpoints = useEndpoints();
  const providers = useProviders();
  const discoveryFor = useEndpointDiscovery();

  // `null` while creating; the dialog seeds its fields from this on open.
  const [formOpen, setFormOpen] = createSignal(false);
  const [editing, setEditing] = createSignal<ModelEndpoint | null>(null);
  const openForm = (ep: ModelEndpoint | null) => {
    setEditing(ep);
    setFormOpen(true);
  };

  const providerName = (id: string): string =>
    (settled(providers) ?? []).find((p) => p.id === id)?.displayName ?? id;

  /* TEST probes the endpoint now (the backend persists the verdict, which the
     re-read reflects on the row) and surfaces the backend's verbatim detail. The
     toggle PATCHes `enabled`; disabling drops the endpoint from the picker. */
  const [testing, setTesting] = createSignal<string | null>(null);
  const runTest = async (ep: ModelEndpoint) => {
    if (testing()) return;
    setTesting(ep.id);
    try {
      const verdict = await testEndpoint(ep.id);
      if (verdict.status === "ok")
        toast.success(`"${ep.name}" — ${verdict.errorDetail}`);
      else toast.error(`"${ep.name}" — ${verdict.errorDetail}`);
    } catch {
      toast.error(`Unable to test "${ep.name}".`);
    } finally {
      setTesting(null);
    }
  };

  const toggleEnabled = async (ep: ModelEndpoint) => {
    try {
      await setEndpointEnabled(ep.id, !ep.enabled);
      toast.success(ep.enabled ? "Endpoint disabled" : "Endpoint enabled");
    } catch {
      toast.error("Unable to update the endpoint.");
    }
  };

  const remove = async (ep: ModelEndpoint) => {
    if (
      !(await confirm({
        title: `Delete endpoint "${ep.name}"?`,
        detail: "Any role bound to it will fall back to its remaining chain.",
        confirmLabel: "Delete",
        tone: "alert",
      }))
    )
      return;
    try {
      await deleteEndpoint(ep.id);
      toast.success("Endpoint deleted");
    } catch {
      toast.error("Unable to delete the endpoint.");
    }
  };

  return (
    <>
      <Panel
        label="Model endpoints"
        meta={
          <Button
            variant="primary"
            size="sm"
            leading="plus"
            onClick={() => openForm(null)}
          >
            Add endpoint
          </Button>
        }
      >
        <Show when={settled(endpoints)} fallback={<LoadingText />}>
          <Show
            when={(settled(endpoints) ?? []).length}
            fallback={
              <EmptyState
                icon="cpu"
                message="No endpoints"
                hint="Add an OpenAI-compatible endpoint to pick its models from the top bar."
              />
            }
          >
            <Stack gap={0}>
              <For each={settled(endpoints) ?? []}>
                {(ep) => (
                  <EndpointRow
                    endpoint={ep}
                    providerName={providerName(ep.provider)}
                    discovery={discoveryFor(ep.id)}
                    testing={testing() === ep.id}
                    onToggleEnabled={() => void toggleEnabled(ep)}
                    onTest={() => void runTest(ep)}
                    onEdit={() => openForm(ep)}
                    onDelete={() => void remove(ep)}
                  />
                )}
              </For>
            </Stack>
          </Show>
        </Show>
      </Panel>

      <EndpointFormDialog
        open={formOpen()}
        endpoint={editing()}
        onClose={() => setFormOpen(false)}
      />
    </>
  );
}
