import { createSignal, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { isApiError } from "~/lib/api";
import { Input, MetaAction, Text, toast, useTheme } from "~/ui";
import {
  createSearchProvider,
  useSearchProviders,
} from "~/features/settings/data";
import { workspaceBriefing } from "../briefings";
import { ChoiceRow, DEFAULT_PICK } from "./ChoiceRow";
import { StepShell } from "./StepShell";

/** Web search as one row: off until a SearXNG address is given. The full provider list
 *  stays in Settings. */
function SearchRow(): JSX.Element {
  const providers = useSearchProviders();
  const [open, setOpen] = createSignal(false);
  const [url, setUrl] = createSignal("");
  const [saving, setSaving] = createSignal(false);
  const count = () =>
    (settled(providers) ?? []).filter((p) => p.enabled).length;

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
      toast.success("Web search added.");
    } catch (e) {
      toast.error(isApiError(e) ? e.detail : "Unable to add web search.");
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
            Where web searches go. Needs a SearXNG instance with JSON output.
          </Text>
        </div>
        <div class="ml-auto flex shrink-0 items-center gap-2 pt-0.5">
          <Text
            variant="micro"
            tone={count() > 0 ? "nominal" : "dim"}
            class="font-mono uppercase"
          >
            {count() > 0 ? `${count()} configured` : "Off"}
          </Text>
          <MetaAction
            active={open()}
            aria-expanded={open()}
            onClick={() => setOpen((v) => !v)}
          >
            {open() ? "close" : "add"}
          </MetaAction>
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
