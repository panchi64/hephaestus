import { Show, type JSX } from "solid-js";
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
import { EmbeddingRoleControls } from "./EmbeddingRoleControls";
import { ModelRoleCard } from "./ModelRoleCard";

const ALL_ROLES: AssignableRole[] = [
  "main",
  "utility",
  "embedding",
  "decision",
];

/** The role cards, named by job rather than by role id: the model that answers you
 *  (`main`), the cheap one for background work (`utility`), the one behind recall
 *  (`embedding`), and Auto's reviewer (`decision`). Each offers the same discovery
 *  groups the composer's model picker reads. `roles` picks which cards show, in this order. */
export function ModelAssignments(props: {
  roles?: AssignableRole[];
}): JSX.Element {
  const a = useModelAssignments();
  const shows = (role: AssignableRole): boolean =>
    (props.roles ?? ALL_ROLES).includes(role);
  const roleWindow = (role: AssignableRole): number | null =>
    settled(a.roles)?.[role]?.contextWindow ?? null;

  const pickChat = async (value: string): Promise<void> => {
    const sel = decodeModelValue(value);
    if (sel && (await a.pickChat(sel)))
      toast.success(`Chat model set to ${sel.model}.`);
  };

  const backgroundGroups = () => [
    {
      label: "Default",
      options: [{ value: SAME_AS_CHAT, label: "Same as chat model" }],
    },
    ...modelPickerGroups(),
  ];

  return (
    <Stack gap={6}>
      <Show when={shows("main")}>
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
      </Show>

      <Show when={shows("utility")}>
        <ModelRoleCard
          label="Background model"
          description="Titles, summaries, verification. A cheaper model is usually the right call."
          groups={backgroundGroups()}
          value={a.roleValue("utility")}
          onChange={(v) => void a.pickBackground(v)}
          onOpen={a.rediscover}
          placeholder="Same as chat model"
          endpoint={a.roleEndpoint("utility")}
          contextWindow={roleWindow("utility")}
        />
      </Show>

      <Show when={shows("embedding")}>
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
      </Show>

      <Show when={shows("decision")}>
        <ModelRoleCard
          label="Decision model"
          description="Rules on Auto's actions without asking you. Needs a decision endpoint: llama-server's /v1/systemone, or OpenRouter's decision models."
          groups={modelPickerGroups()}
          value={a.roleValue("decision")}
          onChange={(v) => void a.bindPicked("decision", "Decision model", v)}
          onOpen={a.rediscover}
          placeholder="Not set — Auto asks you instead"
          endpoint={a.roleEndpoint("decision")}
        />
      </Show>
    </Stack>
  );
}
