import { createSignal, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { ChoiceList, MetaAction, type ChoiceOption } from "~/ui";
import { modelGroups, type ModelEndpoint } from "~/lib/stores/models";
import { endpointBriefing, providerReason } from "../briefings";
import { OptionLabel } from "./OptionLabel";
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
  /** The endpoint the briefing talks about: the one last added, edited or tested. */
  const [latestId, setLatestId] = createSignal<string | null>(null);

  const list = (): ModelEndpoint[] => settled(endpoints) ?? [];
  const groupSize = (id: string): number =>
    modelGroups().find((g) => g.endpointId === id)?.choices.length ?? 0;
  const ready = (): boolean => endpointReady(list());
  const available = (): LocalServer[] =>
    unclaimedServers(props.servers ?? [], list());

  const options = (): ChoiceOption[] => [
    ...available().map((s, i) => ({
      id: LOCAL + s.baseUrl,
      label: (
        <OptionLabel
          title={s.label}
          reason="Runs on this machine; nothing you send leaves it."
        />
      ),
      hint: `${i === 0 ? "RECOMMENDED · " : ""}${serverHint(s)}`,
    })),
    ...(settled(providers) ?? []).map((p) => ({
      id: PROVIDER + p.id,
      label: (
        <OptionLabel
          title={p.displayName}
          reason={providerReason(p.id, p.requiresKey)}
        />
      ),
      hint: p.requiresKey ? "API KEY" : "URL",
    })),
  ];

  const test = async (id: string): Promise<void> => {
    setLatestId(id);
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

  const briefing = (): string => {
    const latest = list().find((e) => e.id === latestId());
    const provider = (settled(providers) ?? []).find(
      (p) => p.id === editor.values().provider,
    );
    return endpointBriefing({
      scanning: props.scanning,
      available: available(),
      latest:
        latest && latest.lastStatus !== null && testing() !== latest.id
          ? {
              name: latest.name,
              ok: latest.lastStatus === "ok",
              models: groupSize(latest.id),
              detail: latest.lastErrorDetail,
            }
          : undefined,
      form: form()
        ? {
            provider: editor.values().provider,
            displayName: provider?.displayName ?? editor.values().name,
            requiresKey: provider?.requiresKey ?? false,
          }
        : undefined,
    });
  };

  return (
    <Show
      when={form()}
      fallback={
        <StepShell
          status={<StatusBlock label="Connections" lines={statusLines()} />}
          briefing={briefing()}
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
        briefing={briefing()}
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
