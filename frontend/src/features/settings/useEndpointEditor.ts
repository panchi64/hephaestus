import { createMemo, createSignal } from "solid-js";
import { settled } from "~/lib/resource";
import { type SelectOption, toast } from "~/ui";
import { isApiError } from "~/lib/api";
import type { ModelEndpoint, ModelProvider } from "~/lib/stores/models";
import { createEndpoint, updateEndpoint, useProviders } from "./data";
import type { EndpointFormValues } from "./components/EndpointForm";

// "openai-compatible" is the wire default the backend applies when no provider
// is chosen — not a frontend-invented preset.
const BLANK_FORM: EndpointFormValues = {
  name: "",
  baseUrl: "",
  provider: "openai-compatible",
  model: "",
  apiKey: "",
  contextWindow: "",
  maxConcurrentRuns: "",
  nativeTools: true,
  vision: false,
  thinking: false,
};

function formFor(ep: ModelEndpoint): EndpointFormValues {
  return {
    name: ep.name,
    baseUrl: ep.baseUrl,
    provider: ep.provider,
    model: ep.model ?? "",
    apiKey: "",
    contextWindow: ep.contextWindow != null ? String(ep.contextWindow) : "",
    maxConcurrentRuns:
      ep.maxConcurrentRuns != null ? String(ep.maxConcurrentRuns) : "",
    nativeTools: ep.nativeTools,
    vision: ep.vision,
    thinking: ep.thinking,
  };
}

/** Field state and the create/update call behind an endpoint form. */
export function useEndpointEditor() {
  const providers = useProviders();
  const [values, setValues] = createSignal<EndpointFormValues>(BLANK_FORM);
  const [target, setTarget] = createSignal<ModelEndpoint | null>(null);
  const [saving, setSaving] = createSignal(false);

  const setField = <K extends keyof EndpointFormValues>(
    key: K,
    value: EndpointFormValues[K],
  ) => setValues((f) => ({ ...f, [key]: value }));

  // The provider presets back the PROVIDER select and its prefills — served by
  // the backend, never hardcoded here.
  const providerById = (id: string): ModelProvider | undefined =>
    (settled(providers) ?? []).find((p) => p.id === id);
  const providerOptions = createMemo<SelectOption[]>(() =>
    (settled(providers) ?? []).map((p) => ({
      value: p.id,
      label: p.displayName,
    })),
  );

  // Picking a provider prefills the base URL from its preset when the field is
  // still untouched (blank, or exactly the prior preset's default) — a typed URL
  // is never clobbered. Prefill only applies while creating.
  const changeProvider = (id: string) => {
    const prior = providerById(values().provider);
    setField("provider", id);
    const next = providerById(id);
    const url = values().baseUrl.trim();
    if (
      !target() &&
      next?.defaultBaseUrl &&
      (url === "" || url === prior?.defaultBaseUrl)
    )
      setField("baseUrl", next.defaultBaseUrl);
  };

  const onChange = <K extends keyof EndpointFormValues>(
    key: K,
    value: EndpointFormValues[K],
  ) => {
    if (key === "provider") changeProvider(value as string);
    else setField(key, value);
  };

  const valid = () =>
    values().name.trim() !== "" && values().baseUrl.trim() !== "";

  const seed = (
    endpoint: ModelEndpoint | null,
    overrides?: Partial<EndpointFormValues>,
  ) => {
    setTarget(endpoint);
    setValues({
      ...(endpoint ? formFor(endpoint) : BLANK_FORM),
      ...overrides,
    });
  };

  const save = async (): Promise<string | null> => {
    if (!valid() || saving()) return null;
    setSaving(true);
    const f = values();
    const cw = f.contextWindow.trim();
    const mcr = f.maxConcurrentRuns.trim();
    const model = f.model.trim();
    const fields = {
      name: f.name.trim(),
      baseUrl: f.baseUrl.trim(),
      provider: f.provider,
      contextWindow: cw ? Number(cw) : null,
      nativeTools: f.nativeTools,
      vision: f.vision,
      thinking: f.thinking,
    };
    try {
      const existing = target();
      if (existing) {
        // Always send model so a cleared field unsets the default; the key is
        // only sent when typed (blank = leave the stored key unchanged).
        await updateEndpoint(existing.id, {
          ...fields,
          maxConcurrentRuns: mcr ? Number(mcr) : 0,
          model,
          ...(f.apiKey ? { apiKey: f.apiKey } : {}),
        });
        toast.success("Endpoint updated");
        return existing.id;
      }
      const id = await createEndpoint({
        ...fields,
        maxConcurrentRuns: mcr ? Number(mcr) : null,
        model: model || undefined,
        apiKey: f.apiKey || undefined,
      });
      toast.success("Endpoint added");
      return id;
    } catch (e) {
      // A 422 (e.g. a key-requiring provider without a key) carries a
      // plain-language detail from the backend — render it verbatim.
      toast.error(isApiError(e) ? e.detail : "Unable to save the endpoint.");
      return null;
    } finally {
      setSaving(false);
    }
  };

  return {
    values,
    providerOptions,
    keyHint: () => providerById(values().provider)?.keyHint ?? undefined,
    onChange,
    valid,
    saving,
    seed,
    save,
  };
}
