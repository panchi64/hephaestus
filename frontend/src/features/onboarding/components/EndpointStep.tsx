import { createSignal, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { ChoiceList, MetaAction, type ChoiceOption } from "~/ui";
import type { ModelEndpoint } from "~/lib/stores/models";
import {
  createEndpoint,
  testEndpoint,
  useEndpoints,
  useProviders,
} from "~/features/settings/data";
import { EndpointForm } from "~/features/settings/components/EndpointForm";
import { useEndpointEditor } from "~/features/settings/useEndpointEditor";
import {
  endpointReady,
  formatTokens,
  unclaimedServers,
  type LocalServer,
} from "../model";
import { StatusBlock, type StatusLine } from "./StatusBlock";
import { StepShell } from "./StepShell";

const LOCAL = "local:";
const PROVIDER = "provider:";

function serverHint(server: LocalServer): string {
  const port = new URL(server.baseUrl).port;
  const windows = server.models
    .map((m) => m.contextWindow)
    .filter((w): w is number => w !== null);
  const ctx = windows.length
    ? ` · ${formatTokens(Math.max(...windows))} CTX`
    : "";
  return `:${port} · ${server.models.length} MODELS${ctx}`;
}

/** Endpoint: a detected local server is one confirm; a provider opens the essentials
 *  form. Every save is tested straight away, and Continue waits for one that passed. */
export function EndpointStep(props: {
  servers: LocalServer[] | undefined;
  scanning: boolean;
  onContinue: () => void;
}): JSX.Element {
  const endpoints = useEndpoints();
  const providers = useProviders();
  const editor = useEndpointEditor();
  const [picked, setPicked] = createSignal<string | null>(null);
  const [form, setForm] = createSignal(false);
  const [testing, setTesting] = createSignal<string | null>(null);
  const [busy, setBusy] = createSignal(false);

  const list = (): ModelEndpoint[] => settled(endpoints) ?? [];
  const ready = (): boolean => endpointReady(list());
  const available = (): LocalServer[] =>
    unclaimedServers(props.servers ?? [], list());

  const options = (): ChoiceOption[] => [
    ...available().map((s) => ({
      id: LOCAL + s.baseUrl,
      label: s.label,
      hint: serverHint(s),
    })),
    ...(settled(providers) ?? []).map((p) => ({
      id: PROVIDER + p.id,
      label: p.displayName,
      hint: p.requiresKey ? "API KEY" : (p.defaultBaseUrl ?? "URL"),
    })),
  ];

  const test = async (id: string): Promise<void> => {
    setTesting(id);
    try {
      await testEndpoint(id);
    } finally {
      setTesting(null);
    }
  };

  const connectLocal = async (server: LocalServer): Promise<void> => {
    const id = await createEndpoint({
      name: server.label,
      baseUrl: server.baseUrl,
      provider: "openai-compatible",
      contextWindow: null,
      maxConcurrentRuns: null,
      nativeTools: true,
      vision: false,
      thinking: false,
    });
    await test(id);
  };

  const submit = async (): Promise<void> => {
    const id = picked();
    if (!id) {
      if (ready()) props.onContinue();
      return;
    }
    setBusy(true);
    try {
      if (id.startsWith(LOCAL)) {
        const server = available().find((s) => LOCAL + s.baseUrl === id);
        if (server) await connectLocal(server);
      } else {
        const provider = (settled(providers) ?? []).find(
          (p) => PROVIDER + p.id === id,
        );
        if (!provider) return;
        editor.seed(null, {
          name: provider.displayName,
          provider: provider.id,
          baseUrl: provider.defaultBaseUrl ?? "",
        });
        setForm(true);
      }
      setPicked(null);
    } finally {
      setBusy(false);
    }
  };

  const saveForm = async (): Promise<void> => {
    const id = await editor.save();
    if (id === null) return;
    setForm(false);
    await test(id);
  };

  const editEndpoint = (ep: ModelEndpoint): void => {
    editor.seed(ep);
    setForm(true);
  };

  const statusLines = (): StatusLine[] => {
    const scan: StatusLine = props.scanning
      ? { label: "Scanning localhost", value: "", live: true }
      : {
          label: "Localhost",
          value: `${props.servers?.length ?? 0} found`,
        };
    const rows = list().map((ep): StatusLine => {
      if (testing() === ep.id)
        return { label: ep.name, value: "Testing", live: true };
      if (ep.lastStatus === "ok")
        return { label: ep.name, value: "OK", tone: "nominal" };
      return {
        label: ep.name,
        value: ep.lastErrorDetail ?? "Untested",
        tone: ep.lastStatus === "error" ? "alert" : "dim",
        action: (
          <>
            <MetaAction onClick={() => editEndpoint(ep)}>edit</MetaAction>
            <MetaAction onClick={() => void test(ep.id)}>retest</MetaAction>
          </>
        ),
      };
    });
    return [scan, ...rows];
  };

  const talos = (): string =>
    !props.scanning && available().length === 0 && list().length === 0
      ? "Nothing is running locally, so point me at a server or an API."
      : "I need a model to think through. I checked this machine first.";

  return (
    <Show
      when={form()}
      fallback={
        <StepShell
          status={<StatusBlock label="Connections" lines={statusLines()} />}
          talos={talos()}
          title={ready() ? "Add another, or continue" : "Choose an endpoint"}
          aside="Required"
        >
          <ChoiceList
            options={options()}
            value={picked()}
            onChange={setPicked}
            onSubmit={() => void submit()}
            submitLabel={picked() ? "Connect" : "Continue"}
            submitDisabled={busy() || (!picked() && !ready())}
            disabled={busy()}
          />
        </StepShell>
      }
    >
      <StepShell
        status={<StatusBlock label="Connections" lines={statusLines()} />}
        talos={talos()}
        title={editor.values().name || "Endpoint"}
        aside="Required"
        buttons={[
          { label: "Back", kbd: "esc", onClick: () => setForm(false) },
          {
            label: editor.saving() ? "Saving…" : "Save & test",
            kbd: "↵",
            primary: true,
            disabled: !editor.valid() || editor.saving(),
            onClick: () => void saveForm(),
          },
        ]}
      >
        <form
          class="p-3"
          onSubmit={(e) => {
            e.preventDefault();
            void saveForm();
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") setForm(false);
          }}
        >
          <EndpointForm
            essentials
            values={editor.values()}
            keyHint={editor.keyHint()}
            onChange={editor.onChange}
          />
        </form>
      </StepShell>
    </Show>
  );
}
