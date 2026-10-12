import { createEffect, createMemo, createSignal, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { Button, Modal, type SelectOption, Stack, toast } from "~/ui";
import { isApiError } from "~/lib/api";
import type { ModelEndpoint, ModelProvider } from "~/lib/stores/models";
import { createEndpoint, updateEndpoint, useProviders } from "../data";
import { EndpointForm, type EndpointFormValues } from "./EndpointForm";

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

/**
 * Create/edit an endpoint. Owns the field values and the save call; the caller
 * only says whether it's open and on which endpoint; `EndpointForm` is the field
 * set it drives.
 */
export function EndpointFormDialog(props: {
  open: boolean;
  /** The endpoint being edited, or `null` to create a new one. */
  endpoint: ModelEndpoint | null;
  onClose: () => void;
}): JSX.Element {
  const providers = useProviders();
  const [values, setValues] = createSignal<EndpointFormValues>(BLANK_FORM);
  const [saving, setSaving] = createSignal(false);

  // Seed the fields each time it opens, on whichever endpoint.
  createEffect(() => {
    if (props.open)
      setValues(props.endpoint ? formFor(props.endpoint) : BLANK_FORM);
  });

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
      !props.endpoint &&
      next?.defaultBaseUrl &&
      (url === "" || url === prior?.defaultBaseUrl)
    )
      setField("baseUrl", next.defaultBaseUrl);
  };

  const valid = () =>
    values().name.trim() !== "" && values().baseUrl.trim() !== "";

  const save = async () => {
    if (!valid() || saving()) return;
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
      const target = props.endpoint;
      if (target) {
        // Always send model so a cleared field unsets the default; the key is
        // only sent when typed (blank = leave the stored key unchanged).
        await updateEndpoint(target.id, {
          ...fields,
          maxConcurrentRuns: mcr ? Number(mcr) : 0,
          model,
          ...(f.apiKey ? { apiKey: f.apiKey } : {}),
        });
        toast.success("Endpoint updated");
      } else {
        await createEndpoint({
          ...fields,
          maxConcurrentRuns: mcr ? Number(mcr) : null,
          model: model || undefined,
          apiKey: f.apiKey || undefined,
        });
        toast.success("Endpoint added");
      }
      props.onClose();
    } catch (e) {
      // A 422 (e.g. a key-requiring provider without a key) carries a
      // plain-language detail from the backend — render it verbatim.
      toast.error(isApiError(e) ? e.detail : "Unable to save the endpoint.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={props.open}
      onClose={props.onClose}
      title={props.endpoint ? "Edit endpoint" : "Add endpoint"}
      class="max-w-lg"
    >
      <Stack gap={3}>
        <EndpointForm
          editing={!!props.endpoint}
          values={values()}
          providerOptions={providerOptions()}
          keyHint={providerById(values().provider)?.keyHint ?? undefined}
          onChange={(key, value) => {
            if (key === "provider") changeProvider(value as string);
            else setField(key, value);
          }}
        />
        <div class="flex justify-end gap-2">
          <Button variant="ghost" onClick={props.onClose}>
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
  );
}
