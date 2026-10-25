import { type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import {
  encodeModelValue,
  modelGroups,
  type ModelSelection,
} from "~/lib/stores/models";
import {
  useModelAssignments,
  type AssignableRole,
} from "~/features/settings/useModelAssignments";
import { modelReason, rolesBriefing } from "../briefings";
import {
  chatChoices,
  embeddingChoices,
  scannedModel,
  type LocalServer,
} from "../model";
import { ChoiceRow, DEFAULT_PICK, type RowOption } from "./ChoiceRow";
import { StepShell } from "./StepShell";

/** Roles: background, decision, and search & memory, one line each with a dropdown on
 *  its current pick. Nothing is bound here unless the operator picks it. */
export function RolesStep(props: {
  servers: LocalServer[];
  onBack: () => void;
  onContinue: () => void;
}): JSX.Element {
  const a = useModelAssignments();
  const endpoints = () => settled(a.endpoints) ?? [];

  const scanned = (sel: ModelSelection) => {
    const ep = endpoints().find((e) => e.id === sel.endpointId);
    return ep ? scannedModel(props.servers, ep.baseUrl, sel.model) : undefined;
  };
  const endpointName = (sel: ModelSelection) =>
    endpoints().find((e) => e.id === sel.endpointId)?.name ?? "";

  const toOptions = (
    list: ModelSelection[],
    reason: (sel: ModelSelection) => string,
    recommend?: (sel: ModelSelection) => boolean,
  ): RowOption[] =>
    list.map((sel) => ({
      id: encodeModelValue(sel),
      title: sel.model,
      reason: reason(sel),
      hint: [recommend?.(sel) ? "Recommended" : "", endpointName(sel)]
        .filter(Boolean)
        .join(" · "),
    }));

  const chatModels = () =>
    chatChoices(modelGroups(), endpoints(), props.servers);
  const embedders = () =>
    embeddingChoices(modelGroups(), endpoints(), props.servers);
  const scannedEmbedder = () =>
    embedders().find((sel) => scanned(sel)?.kind === "embeddings");

  const current = (role: AssignableRole): string =>
    a.roleValue(role) || DEFAULT_PICK;

  const pick =
    (role: AssignableRole, name: string) =>
    async (id: string): Promise<void> => {
      if (id === DEFAULT_PICK) await a.clearRole(role, name);
      else if (role === "utility") await a.pickBackground(id);
      else await a.bindPicked(role, name, id);
    };

  const briefing = (): string => {
    const found = scannedEmbedder();
    const server = found
      ? endpoints().find((e) => e.id === found.endpointId)?.name
      : undefined;
    return rolesBriefing(
      found && server ? { server, model: found.model } : undefined,
    );
  };

  const recallPurpose = (): string => {
    const found = scannedEmbedder();
    return found && current("embedding") === DEFAULT_PICK
      ? `Recall across past conversations · ${found.model} available`
      : "Recall across past conversations and memories.";
  };

  return (
    <StepShell
      briefing={briefing()}
      title="Helper models"
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
          label="Background"
          purpose="Titles, summaries, and checks on replies."
          defaultTitle="Same as chat model"
          defaultMeaning="No extra model; background work shares the chat model."
          value={current("utility")}
          options={toOptions(chatModels(), (sel) => modelReason(scanned(sel)))}
          onPick={pick("utility", "Background model")}
        />
        <ChoiceRow
          label="Decision"
          purpose="Reviews tool actions when you run in Auto."
          defaultTitle="Ask me instead"
          defaultMeaning="Auto pauses and asks you before anything sensitive."
          value={current("decision")}
          options={toOptions(
            chatModels(),
            () =>
              "Must serve a decision API: llama-server's /v1/systemone, or OpenRouter.",
          )}
          onPick={pick("decision", "Decision model")}
        />
        <ChoiceRow
          label="Search & memory"
          purpose={recallPurpose()}
          defaultTitle="Keyword recall"
          defaultMeaning="Recall matches words, not meaning. Picking a model re-indexes."
          value={current("embedding")}
          options={toOptions(
            embedders(),
            (sel) =>
              scanned(sel)?.kind === "embeddings"
                ? "An embedding model: recall by meaning."
                : "Must be an embedding model.",
            (sel) => scanned(sel)?.kind === "embeddings",
          )}
          onPick={pick("embedding", "Search & memory model")}
        />
      </div>
    </StepShell>
  );
}
