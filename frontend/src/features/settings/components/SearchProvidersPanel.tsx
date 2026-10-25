import { createSignal, For, onMount, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  Button,
  EmptyState,
  Field,
  Input,
  LoadingText,
  Modal,
  Panel,
  Row,
  Stack,
  StatusFlag,
  Text,
  Textarea,
  Toggle,
  confirm,
  toast,
} from "~/ui";
import {
  createSearchProvider,
  deleteSearchProvider,
  refreshWebSearchHealth,
  updateSearchProvider,
  useSearchProviders,
  useWebSearchHealth,
} from "../data";
import { webSearchReadout, type SearchProvider } from "../model";

/** Web search: the built-in SearXNG, and the operator's own instances that override it.
 *  Owns its catalog resource and its add/edit form so the Settings screen just composes
 *  it. Enabled/disabled reads by brightness, not hue; the API key is write-only. */
export function SearchProvidersPanel(): JSX.Element {
  const providers = useSearchProviders();
  const health = useWebSearchHealth();
  const readout = () =>
    webSearchReadout(
      health(),
      (settled(providers) ?? []).filter((p) => p.enabled).length,
    );
  onMount(() => void refreshWebSearchHealth());

  const [formOpen, setFormOpen] = createSignal(false);
  const [editing, setEditing] = createSignal<SearchProvider | null>(null);
  const [name, setName] = createSignal("");
  const [baseUrl, setBaseUrl] = createSignal("");
  const [enabled, setEnabled] = createSignal(true);
  const [engines, setEngines] = createSignal("");
  const [params, setParams] = createSignal("");
  const [apiKey, setApiKey] = createSignal("");
  const [saving, setSaving] = createSignal(false);

  const openCreate = () => {
    setEditing(null);
    setName("");
    setBaseUrl("");
    setEnabled(true);
    setEngines("");
    setParams("");
    setApiKey("");
    setFormOpen(true);
  };
  const openEdit = (p: SearchProvider) => {
    setEditing(p);
    setName(p.name);
    setBaseUrl(p.baseUrl);
    setEnabled(p.enabled);
    setEngines(p.engines.join(", "));
    setParams(
      Object.keys(p.params).length ? JSON.stringify(p.params, null, 2) : "",
    );
    setApiKey("");
    setFormOpen(true);
  };

  const valid = () => name().trim() !== "" && baseUrl().trim() !== "";

  // Parse the comma list + JSON params; null signals a params parse error so the
  // save aborts with a message rather than sending a malformed body.
  const collect = (): {
    engines: string[];
    params: Record<string, unknown>;
  } | null => {
    const engineList = engines()
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const raw = params().trim();
    if (!raw) return { engines: engineList, params: {} };
    try {
      const obj: unknown = JSON.parse(raw);
      if (obj === null || typeof obj !== "object" || Array.isArray(obj)) {
        return null;
      }
      return { engines: engineList, params: obj as Record<string, unknown> };
    } catch {
      return null;
    }
  };

  const save = async () => {
    if (!valid() || saving()) return;
    const extra = collect();
    if (!extra) {
      toast.error(
        'Advanced params must be a JSON object, e.g. {"language":"en"}.',
      );
      return;
    }
    setSaving(true);
    const fields = {
      name: name().trim(),
      baseUrl: baseUrl().trim(),
      enabled: enabled(),
      engines: extra.engines,
      params: extra.params,
    };
    try {
      const target = editing();
      if (target) {
        // The key is only sent when typed (blank = leave the stored key unchanged).
        await updateSearchProvider(target.id, {
          ...fields,
          ...(apiKey() ? { apiKey: apiKey() } : {}),
        });
        toast.success("Provider updated");
      } else {
        await createSearchProvider({
          ...fields,
          ...(apiKey() ? { apiKey: apiKey() } : {}),
        });
        toast.success("Provider added");
      }
      setFormOpen(false);
    } catch {
      toast.error("Unable to save the provider.");
    } finally {
      setSaving(false);
    }
  };

  // Quick enable/disable without opening the form — the active provider is the
  // first enabled one, so this is the most common adjustment.
  const toggleEnabled = async (p: SearchProvider) => {
    try {
      await updateSearchProvider(p.id, { enabled: !p.enabled });
    } catch {
      toast.error(`Unable to ${p.enabled ? "disable" : "enable"} "${p.name}".`);
    }
  };

  const remove = async (p: SearchProvider) => {
    if (
      !(await confirm({
        title: `Delete provider "${p.name}"?`,
        detail:
          "Web search falls back to the next enabled provider, or to the built-in SearXNG if none remain.",
        confirmLabel: "Delete",
        tone: "alert",
      }))
    )
      return;
    try {
      await deleteSearchProvider(p.id);
      toast.success("Provider deleted");
    } catch {
      toast.error("Unable to delete the provider.");
    }
  };

  return (
    <Panel
      label="Web search"
      meta={
        <Button variant="primary" size="sm" leading="plus" onClick={openCreate}>
          Add provider
        </Button>
      }
    >
      <Stack gap={4}>
        <Text variant="micro" tone="dim">
          Web search runs on a built-in SearXNG instance. Add your own to use it
          instead: the first enabled one is used, and the rest stay as
          alternates. Your instance needs JSON output enabled.
        </Text>
        <Row align="center" justify="between" gap={3} class="py-2">
          <Stack gap={1} class="min-w-0">
            <Text
              variant="label"
              tone={readout().source === "built-in" ? "bright" : "dim"}
            >
              Built-in SearXNG
            </Text>
            <Text variant="micro" tone="dim">
              {readout().source === "built-in"
                ? "In use."
                : "Standing by while your instance is enabled."}
            </Text>
          </Stack>
          <Text
            variant="micro"
            tone={readout().source === "built-in" ? readout().tone : "dim"}
            class="shrink-0 font-mono uppercase"
          >
            {readout().source === "built-in" ? readout().label : "Standby"}
          </Text>
        </Row>
        <Show when={settled(providers)} fallback={<LoadingText />}>
          <Show
            when={(settled(providers) ?? []).length}
            fallback={
              <EmptyState
                icon="search"
                message="No instance of your own"
                hint="Add a SearXNG instance to search through it instead of the built-in one."
              />
            }
          >
            <Stack gap={0}>
              <For each={settled(providers) ?? []}>
                {(p) => (
                  <Row align="center" justify="between" gap={3} class="py-2">
                    <Stack gap={1} class="min-w-0">
                      <Row gap={2} align="center">
                        <Text
                          variant="label"
                          tone={p.enabled ? "bright" : "dim"}
                        >
                          {p.name}
                        </Text>
                        <Show when={p.hasApiKey}>
                          <StatusFlag status="nominal">Key</StatusFlag>
                        </Show>
                      </Row>
                      <Text variant="micro" tone="dim" class="truncate">
                        {p.engines.length
                          ? `${p.baseUrl} · ${p.engines.join(", ")}`
                          : p.baseUrl}
                      </Text>
                    </Stack>
                    <span class="flex shrink-0 items-center gap-2">
                      <Toggle
                        checked={p.enabled}
                        onChange={() => void toggleEnabled(p)}
                      />
                      <Button
                        variant="ghost"
                        size="sm"
                        leading="edit"
                        onClick={() => openEdit(p)}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        leading="trash"
                        onClick={() => remove(p)}
                      >
                        Delete
                      </Button>
                    </span>
                  </Row>
                )}
              </For>
            </Stack>
          </Show>
        </Show>
      </Stack>

      <Modal
        open={formOpen()}
        onClose={() => setFormOpen(false)}
        title={editing() ? "Edit provider" : "Add provider"}
        class="max-w-lg"
      >
        <Stack gap={3}>
          <Input
            label="Name"
            value={name()}
            onInput={(e) => setName(e.currentTarget.value)}
            placeholder="e.g. searxng"
          />
          <Input
            label="Base URL"
            value={baseUrl()}
            onInput={(e) => setBaseUrl(e.currentTarget.value)}
            placeholder="http://localhost:8080"
          />
          <Input
            label="ENGINES (optional, comma-separated)"
            value={engines()}
            onInput={(e) => setEngines(e.currentTarget.value)}
            placeholder="google, duckduckgo"
            hint="Limit which SearXNG engines answer. Blank = the instance default."
          />
          <Input
            label={
              editing() ? "API KEY (blank = unchanged)" : "API KEY (optional)"
            }
            type="password"
            value={apiKey()}
            onInput={(e) => setApiKey(e.currentTarget.value)}
            placeholder="••••••••"
            hint="Only for a guarded instance; most SearXNG setups need none."
          />
          <Textarea
            label="ADVANCED PARAMS (optional, JSON)"
            value={params()}
            onInput={(e) => setParams(e.currentTarget.value)}
            placeholder={'{ "language": "en" }'}
            rows={3}
          />
          <Row gap={4} align="center" justify="between">
            <Field label="Enabled" orientation="row" value="" />
            <Toggle checked={enabled()} onChange={setEnabled} />
          </Row>
          <div class="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setFormOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={!valid() || saving()}
              onClick={save}
            >
              {saving() ? "Saving…" : "Save"}
            </Button>
          </div>
        </Stack>
      </Modal>
    </Panel>
  );
}
