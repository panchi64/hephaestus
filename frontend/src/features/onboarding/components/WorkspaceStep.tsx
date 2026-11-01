import { createSignal, onMount, Show, type JSX } from "solid-js";
import { isApiError } from "~/lib/api";
import { Input, MetaAction, Text, toast, useTheme } from "~/ui";
import {
  createSearchProvider,
  refreshCapabilityHealth,
  useSearchProviders,
  useWebSearchReadout,
} from "~/features/settings/data";
import { workspaceBriefing } from "../briefings";
import { ChoiceRow, DEFAULT_PICK } from "./ChoiceRow";
import { StepShell } from "./StepShell";

/** Web search as one row: the built-in SearXNG unless the operator gives an address of
 *  their own. The full provider list stays in Settings. */
function SearchRow(): JSX.Element {
  const readout = useWebSearchReadout(useSearchProviders());
  const [open, setOpen] = createSignal(false);
  const [url, setUrl] = createSignal("");
  const [saving, setSaving] = createSignal(false);
  onMount(() => void refreshCapabilityHealth());

  const add = async (): Promise<void> => {
    const baseUrl = url().trim();
    if (!baseUrl) return;
    setSaving(true);
    try {
      await createSearchProvider({
        name: "SearXNG",
        baseUrl,
        enabled: true,
        engines: [],
        params: {},
      });
      setUrl("");
      setOpen(false);
      toast.success("Searches now go to your SearXNG.");
    } catch (e) {
      toast.error(isApiError(e) ? e.detail : "Unable to add your SearXNG.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div class="flex flex-col">
      <div class="flex min-w-0 items-center gap-3 px-3 py-3">
        <div class="flex min-w-0 flex-col gap-0.5">
          <Text variant="body" tone="bright">
            Web search
          </Text>
          <Text variant="micro" tone="dim">
            {readout().source === "own"
              ? "Searches go to your SearXNG instance."
              : "Searches go to the built-in SearXNG. Add your own, with JSON output on, to use it instead."}
          </Text>
        </div>
        <div class="ml-auto flex shrink-0 items-center gap-2 pt-0.5">
          <Text
            variant="micro"
            tone={readout().tone}
            class="font-mono uppercase"
          >
            {readout().label}
          </Text>
          <Show when={readout().source === "built-in"}>
            <MetaAction
              active={open()}
              aria-expanded={open()}
              onClick={() => setOpen((v) => !v)}
            >
              {open() ? "close" : "use own"}
            </MetaAction>
          </Show>
        </div>
      </div>
      <Show when={open()}>
        <form
          class="flex items-end gap-2 border-t border-line p-3"
          onSubmit={(e) => {
            e.preventDefault();
            void add();
          }}
        >
          <div class="min-w-0 flex-1">
            <Input
              label="SearXNG address"
              value={url()}
              onInput={(e) => setUrl(e.currentTarget.value)}
              placeholder="http://127.0.0.1:8888"
            />
          </div>
          <MetaAction
            disabled={saving() || !url().trim()}
            onClick={() => void add()}
          >
            {saving() ? "adding…" : "add"}
          </MetaAction>
        </form>
      </Show>
    </div>
  );
}

/** Workspace: the two preferences worth setting on day one, as rows. Everything else,
 *  the accent palette and the VM included, stays in Settings. */
export function WorkspaceStep(props: {
  onBack: () => void;
  onContinue: () => void;
}): JSX.Element {
  const theme = useTheme();
  const themeValue = () =>
    theme.preference === "system" ? DEFAULT_PICK : theme.preference;

  return (
    <StepShell
      briefing={workspaceBriefing()}
      title="Preferences"
      aside="Optional"
      buttons={[
        { label: "Back", onClick: props.onBack },
        {
          label: "Continue",
          kbd: "↵",
          primary: true,
          onClick: props.onContinue,
        },
      ]}
    >
      <div class="divide-y divide-line">
        <ChoiceRow
          label="Theme"
          purpose="How the workspace looks on this device."
          defaultTitle="Follow system"
          defaultMeaning="Dark or light with your operating system."
          value={themeValue()}
          options={[
            {
              id: "phosphor",
              title: "Phosphor",
              reason: "Dark, always.",
            },
            { id: "paper", title: "Paper", reason: "Light, always." },
          ]}
          onPick={async (id) => {
            theme.set(
              id === "paper"
                ? "paper"
                : id === "phosphor"
                  ? "phosphor"
                  : "system",
            );
          }}
        />
        <SearchRow />
      </div>
    </StepShell>
  );
}
