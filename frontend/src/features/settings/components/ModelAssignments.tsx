import { type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { Stack, toast } from "~/ui";
import {
  decodeModelValue,
  effectiveValue,
  modelPickerGroups,
  sendBlockedReason,
} from "~/lib/stores/models";
import {
  SAME_AS_CHAT,
  useModelAssignments,
  type AssignableRole,
} from "../useModelAssignments";
import { ChoosableModels } from "./ChoosableModels";
import { EmbeddingRoleControls } from "./EmbeddingRoleControls";
import { ModelRoleCard } from "./ModelRoleCard";

/** The role cards, named by job rather than by role id: the model that answers you
 *  (`main`), the cheap one for background work (`utility`), the default for spawned
 *  agents (`subagent`, with the list Talos may choose from), and the one behind recall
 *  (`embedding`). Each offers the same discovery groups the composer's model picker
 *  reads. */
export function ModelAssignments(): JSX.Element {
  const a = useModelAssignments();
  const roleWindow = (role: AssignableRole): number | null =>
    settled(a.roles)?.[role]?.contextWindow ?? null;

  const pickChat = async (value: string): Promise<void> => {
    const sel = decodeModelValue(value);
    if (sel && (await a.pickChat(sel)))
      toast.success(`Chat model set to ${sel.model}.`);
  };

  const withDefault = (label: string) => [
    { label: "Default", options: [{ value: SAME_AS_CHAT, label }] },
    ...modelPickerGroups(),
  ];

  return (
    <Stack gap={6}>
      <ModelRoleCard
        label="Chat model"
        description="Answers you in chat, research, and tasks."
        groups={modelPickerGroups()}
        value={effectiveValue()}
        onChange={(v) => void pickChat(v)}
        onOpen={a.rediscover}
        placeholder="No model"
        endpoint={a.chatEndpoint()}
        contextWindow={roleWindow("main")}
        implicit={settled(a.roles)?.main?.implicit ?? false}
        warning={sendBlockedReason() ?? undefined}
      />

      <ModelRoleCard
        label="Background model"
        description="Titles, summaries, verification. A cheaper model is usually the right call."
        groups={withDefault("Same as chat model")}
        value={a.roleValue("utility")}
        onChange={(v) => void a.pickOrInherit("utility", v)}
        onOpen={a.rediscover}
        placeholder="Same as chat model"
        endpoint={a.roleEndpoint("utility")}
        contextWindow={roleWindow("utility")}
      />

      <ModelRoleCard
        label="Sub-agent model"
        description="What a spawned agent runs on unless the agent is pinned to a model of its own."
        groups={withDefault("Same as parent agent")}
        value={a.roleValue("subagent")}
        onChange={(v) => void a.pickOrInherit("subagent", v)}
        onOpen={a.rediscover}
        placeholder="Same as parent agent"
        endpoint={a.roleEndpoint("subagent")}
        contextWindow={roleWindow("subagent")}
      >
        <ChoosableModels />
      </ModelRoleCard>

      <ModelRoleCard
        label="Search & memory model"
        description="Powers recall across memories and chats. Changing this re-indexes everything."
        groups={modelPickerGroups()}
        value={a.roleValue("embedding")}
        onChange={(v) =>
          void a.bindPicked("embedding", "Search & memory model", v)
        }
        onOpen={a.rediscover}
        placeholder="Not set — recall is keyword-only"
        endpoint={a.roleEndpoint("embedding")}
      >
        <EmbeddingRoleControls
          bound={a.roleEndpoint("embedding") !== undefined}
        />
      </ModelRoleCard>
    </Stack>
  );
}
