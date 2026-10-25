import { createMemo, createSignal } from "solid-js";
import { settled } from "~/lib/resource";
import type { SelectOption } from "~/ui";
import type { ModelEndpoint, ModelProvider } from "~/lib/stores/models";
import { useProviders } from "./data";
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

/** Field state behind an endpoint form; `saveEndpointForm` writes it. */
export function useEndpointEditor() {
  const providers = useProviders();
  const [values, setValues] = createSignal<EndpointFormValues>(BLANK_FORM);
  const [target, setTarget] = createSignal<ModelEndpoint | null>(null);

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

  return {
    values,
    /** The endpoint being edited, or null while creating. */
    target,
    providerOptions,
    keyHint: () => providerById(values().provider)?.keyHint ?? undefined,
    onChange,
    valid,
    seed,
  };
}
