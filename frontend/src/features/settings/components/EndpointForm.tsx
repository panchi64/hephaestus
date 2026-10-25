import { Show, type JSX } from "solid-js";
import {
  Disclosure,
  Field,
  Input,
  Row,
  Select,
  type SelectOption,
  Stack,
  Toggle,
} from "~/ui";

/** The endpoint form's field state. Held by the parent so this stays a pure
 *  presentation control: it renders fields and relays changes, it owns no state
 *  and makes no decision. */
export interface EndpointFormValues {
  name: string;
  baseUrl: string;
  /** The provider adapter id (from `GET /models/providers`). */
  provider: string;
  model: string;
  apiKey: string;
  contextWindow: string;
  maxConcurrentRuns: string;
  nativeTools: boolean;
  vision: boolean;
  thinking: boolean;
}

export interface EndpointFormProps {
  values: EndpointFormValues;
  onChange: <K extends keyof EndpointFormValues>(
    key: K,
    value: EndpointFormValues[K],
  ) => void;
  /** Edit mode relabels the key field (blank = leave the stored key unchanged). */
  editing?: boolean;
  /** The provider choices (from `GET /models/providers`). Omitted ⇒ no provider
   *  select is rendered. */
  providerOptions?: SelectOption[];
  /** The chosen provider's key hint (e.g. "sk-ant-…"), shown on the key field. */
  keyHint?: string;
  /** Show only Base URL and API key; the rest sits under a closed "Advanced". */
  essentials?: boolean;
  /** Essentials mode only: render the provider select (when options are given). */
  showProvider?: boolean;
}

/** The create/edit endpoint field set — name, how it is reached, and what it can do. */
export function EndpointForm(props: EndpointFormProps): JSX.Element {
  const v = () => props.values;

  const name = () => (
    <Input
      label="Name"
      value={v().name}
      onInput={(e) => props.onChange("name", e.currentTarget.value)}
      placeholder="e.g. local-qwen"
    />
  );

  const provider = () => (
    <Show when={props.providerOptions}>
      {(options) => (
        <Select
          label="Provider"
          value={v().provider}
          options={options()}
          onChange={(id) => props.onChange("provider", id)}
          hint="How Hephaestus talks to this endpoint. Presets prefill the base URL."
        />
      )}
    </Show>
  );

  const baseUrl = () => (
    <Input
      label="Base URL"
      value={v().baseUrl}
      onInput={(e) => props.onChange("baseUrl", e.currentTarget.value)}
      placeholder="http://localhost:1234/v1"
    />
  );

  const model = () => (
    <Input
      label="DEFAULT MODEL (optional)"
      value={v().model}
      onInput={(e) => props.onChange("model", e.currentTarget.value)}
      placeholder="qwen2.5-coder:32b"
      hint="Models are discovered from the provider and picked in the composer. Set a default only as a fallback for providers without a models API."
    />
  );

  const apiKey = () => (
    <Input
      label={
        props.editing ? "API KEY (blank = unchanged)" : "API KEY (optional)"
      }
      type="password"
      value={v().apiKey}
      onInput={(e) => props.onChange("apiKey", e.currentTarget.value)}
      placeholder={props.keyHint ?? "••••••••"}
    />
  );

  const capabilities = () => (
    <>
      <Input
        label="CONTEXT WINDOW (optional)"
        value={v().contextWindow}
        onInput={(e) => props.onChange("contextWindow", e.currentTarget.value)}
        placeholder="32768"
      />
      <Input
        label="Concurrent runs"
        value={v().maxConcurrentRuns}
        onInput={(e) =>
          props.onChange("maxConcurrentRuns", e.currentTarget.value)
        }
        placeholder="auto"
        inputMode="numeric"
        hint="How many runs may use this endpoint at once; others wait in line. Empty uses the server's slot count when it reports one, otherwise no limit."
      />
      <Row gap={4} align="center" justify="between">
        <Field label="Native tools" orientation="row" value="" />
        <Toggle
          checked={v().nativeTools}
          onChange={(c) => props.onChange("nativeTools", c)}
        />
      </Row>
      <Row gap={4} align="center" justify="between">
        <Field label="Vision" orientation="row" value="" />
        <Toggle
          checked={v().vision}
          onChange={(c) => props.onChange("vision", c)}
        />
      </Row>
      <Row gap={4} align="center" justify="between">
        <Field label="Thinking" orientation="row" value="" />
        <Toggle
          checked={v().thinking}
          onChange={(c) => props.onChange("thinking", c)}
        />
      </Row>
    </>
  );

  return (
    <Show
      when={props.essentials}
      fallback={
        <Stack gap={3}>
          {name()}
          {provider()}
          {baseUrl()}
          {model()}
          {apiKey()}
          {capabilities()}
        </Stack>
      }
    >
      <Stack gap={3}>
        <Show when={props.showProvider}>{provider()}</Show>
        {baseUrl()}
        {apiKey()}
        <Disclosure label="Advanced">
          <Stack gap={3}>
            {name()}
            {model()}
            {capabilities()}
          </Stack>
        </Disclosure>
      </Stack>
    </Show>
  );
}
