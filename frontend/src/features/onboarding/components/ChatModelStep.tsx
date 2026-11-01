import { createEffect, createSignal, on, Show, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { isApiError } from "~/lib/api";
import { ChoiceList, Input, Text, toast, type ChoiceOption } from "~/ui";
import {
  effectiveSelection,
  encodeModelValue,
  modelGroups,
  refreshRoles,
  sendBlockedReason,
  type ModelSelection,
} from "~/lib/stores/models";
import { updateEndpoint } from "~/features/settings/data";
import { useModelAssignments } from "~/features/settings/useModelAssignments";
import {
  chatChoices,
  chatReady,
  formatTokens,
  preferredChoice,
  scannedModel,
  type LocalServer,
} from "../model";
import { chatBriefing, modelReason } from "../briefings";
import { OptionLabel } from "./OptionLabel";
import { StatusBlock } from "./StatusBlock";
import { StepShell } from "./StepShell";

/** Chat model: the discovered models on working endpoints, the loaded one picked for the
 *  operator. A pick is written at once; Continue waits for a binding with a window. */
export function ChatModelStep(props: {
  servers: LocalServer[];
  onBack: () => void;
  onContinue: () => void;
}): JSX.Element {
  const a = useModelAssignments();
  const [windowDraft, setWindowDraft] = createSignal("");
  const [busy, setBusy] = createSignal(false);

  const endpoints = () => settled(a.endpoints) ?? [];
  const main = () => settled(a.roles)?.main;
  const choices = (): ModelSelection[] =>
    chatChoices(modelGroups(), endpoints(), props.servers);

  const endpointOf = (id: string) => endpoints().find((e) => e.id === id);

  const scannedFor = (sel: ModelSelection) => {
    const ep = endpointOf(sel.endpointId);
    return ep ? scannedModel(props.servers, ep.baseUrl, sel.model) : undefined;
  };
  const recommended = (): string | null => {
    const pick = preferredChoice(choices(), endpoints(), props.servers);
    return pick ? encodeModelValue(pick) : null;
  };

  const hint = (sel: ModelSelection): string => {
    const scanned = scannedFor(sel);
    const parts = [
      encodeModelValue(sel) === recommended() ? "RECOMMENDED" : "",
      endpointOf(sel.endpointId)?.name ?? "",
    ];
    if (scanned?.contextWindow)
      parts.push(`${formatTokens(scanned.contextWindow)} CTX`);
    return parts.filter(Boolean).join(" · ");
  };

  const options = (): ChoiceOption[] =>
    choices().map((c) => ({
      id: encodeModelValue(c),
      label: (
        <OptionLabel title={c.model} reason={modelReason(scannedFor(c))} />
      ),
      hint: hint(c),
    }));

  // Read off the selection itself, which a pick moves at once, rather than off the
  // binding: the binding refetches after every pick, and between the two it would
  // describe no model and then the new one, re-revealing the whole line.
  const briefing = (): string => {
    const sel = effectiveSelection();
    const scanned = sel ? scannedFor(sel) : undefined;
    const bound = main();
    const window =
      scanned?.contextWindow ??
      (bound && bound.model === sel?.model ? bound.contextWindow : null);
    return chatBriefing({
      picked: choices().length > 0 ? sel?.model : undefined,
      pickedWindow: window,
      pickedLoaded: scanned?.loaded === true,
      needsWindow: needsWindow(),
    });
  };

  const pickedValue = (): string | null => {
    const sel = effectiveSelection();
    return sel && !main()?.implicit ? encodeModelValue(sel) : null;
  };

  const pick = async (id: string | null): Promise<void> => {
    const sel = choices().find((c) => encodeModelValue(c) === id);
    if (!sel) return;
    setBusy(true);
    try {
      await a.pickChat(sel);
    } finally {
      setBusy(false);
    }
  };

  // An unchosen chat model is chosen for the operator once the list is known; Talos says
  // so, and any row overrides it.
  createEffect(
    on([choices, () => settled(a.roles)], ([list, roles]) => {
      if (busy() || !roles || roles.main?.implicit === false) return;
      const preferred = preferredChoice(list, endpoints(), props.servers);
      if (preferred) void pick(encodeModelValue(preferred));
    }),
  );

  const needsWindow = (): boolean =>
    !!main() && !main()?.implicit && main()?.contextWindow === null;

  const ready = (): boolean => chatReady(main(), endpoints());

  const saveWindow = async (): Promise<void> => {
    const n = Number(windowDraft().trim());
    const id = main()?.endpointIds[0];
    if (!id) return;
    if (!Number.isInteger(n) || n <= 0) {
      toast.error("Enter the window as a whole number of tokens, like 32768.");
      return;
    }
    try {
      await updateEndpoint(id, { contextWindow: n });
      refreshRoles();
    } catch (e) {
      toast.error(
        isApiError(e) ? e.detail : "Unable to set the context window.",
      );
    }
  };

  const status = () => {
    const m = main();
    if (!m || m.implicit) return [];
    return [
      {
        label: effectiveSelection()?.model ?? "Chat model",
        value: m.contextWindow
          ? `${formatTokens(m.contextWindow)} CTX`
          : "No context window",
        tone: m.contextWindow ? ("nominal" as const) : ("alert" as const),
      },
    ];
  };

  return (
    <StepShell
      status={<StatusBlock label="Chat model" lines={status()} />}
      briefing={briefing()}
      title="Pick the chat model"
      aside="Required"
    >
      <Show when={needsWindow()}>
        <form
          class="flex items-end gap-2 p-3"
          onSubmit={(e) => {
            e.preventDefault();
            void saveWindow();
          }}
        >
          <Input
            label="Context window"
            value={windowDraft()}
            onInput={(e) => setWindowDraft(e.currentTarget.value)}
            placeholder="32768"
            inputMode="numeric"
            hint={sendBlockedReason() ?? undefined}
          />
        </form>
      </Show>
      <Show
        when={options().length > 0}
        fallback={
          <Text variant="caption" tone="dim" class="block p-3">
            No working endpoint lists any models yet. Go back and check the
            endpoint.
          </Text>
        }
      >
        <ChoiceList
          options={options()}
          value={pickedValue()}
          onChange={(id) => void pick(id)}
          onSubmit={() => {
            if (needsWindow() && windowDraft().trim()) void saveWindow();
            else if (ready()) props.onContinue();
          }}
          submitLabel="Continue"
          submitDisabled={
            busy() || (!ready() && !(needsWindow() && windowDraft().trim()))
          }
          secondary={[{ label: "Back", onClick: props.onBack }]}
          disabled={busy()}
        />
      </Show>
    </StepShell>
  );
}
