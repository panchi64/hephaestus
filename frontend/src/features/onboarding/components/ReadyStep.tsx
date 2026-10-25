import { createSignal, For, type JSX } from "solid-js";
import { settled } from "~/lib/resource";
import { MetaAction, Text } from "~/ui";
import { effectiveSelection } from "~/lib/stores/models";
import { useEndpoints, useRoles } from "~/features/settings/data";
import { readyBriefing } from "../briefings";
import { formatTokens, type StepId } from "../model";
import { StepShell } from "./StepShell";

interface SummaryRow {
  label: string;
  value: string;
  step: StepId;
}

/** Ready: what the workspace will run on, each line a way back to the step that set it,
 *  then the way in. */
export function ReadyStep(props: {
  onEdit: (step: StepId) => void;
  onBack: () => void;
  onEnter: () => Promise<void>;
}): JSX.Element {
  const endpoints = useEndpoints();
  const roles = useRoles();
  const [entering, setEntering] = createSignal(false);

  const roleModel = (role: string, fallback: string): string => {
    const binding = settled(roles)?.[role];
    if (!binding || binding.endpointIds.length === 0)
      return `Default · ${fallback}`;
    const ep = (settled(endpoints) ?? []).find(
      (e) => e.id === binding.endpointIds[0],
    );
    return binding.model ?? ep?.model ?? ep?.name ?? "Set";
  };

  const rows = (): SummaryRow[] => {
    const working = (settled(endpoints) ?? []).filter(
      (e) => e.enabled && e.lastStatus === "ok",
    );
    const window = settled(roles)?.main?.contextWindow;
    return [
      {
        label: "Endpoints",
        value: working.map((e) => e.name).join(", ") || "None",
        step: "endpoint",
      },
      {
        label: "Chat model",
        value: [
          effectiveSelection()?.model ?? "None",
          window ? `${formatTokens(window)} CTX` : "",
        ]
          .filter(Boolean)
          .join(" · "),
        step: "chat",
      },
      {
        label: "Background",
        value: roleModel("utility", "chat model"),
        step: "roles",
      },
      {
        label: "Decision",
        value: roleModel("decision", "Auto asks you"),
        step: "roles",
      },
      {
        label: "Search & memory",
        value: roleModel("embedding", "keyword recall"),
        step: "roles",
      },
    ];
  };

  const enter = async (): Promise<void> => {
    setEntering(true);
    try {
      await props.onEnter();
    } finally {
      setEntering(false);
    }
  };

  return (
    <StepShell
      briefing={readyBriefing()}
      title="Summary"
      buttons={[
        { label: "Back", onClick: props.onBack },
        {
          label: entering() ? "Entering…" : "Enter workspace",
          kbd: "↵",
          primary: true,
          disabled: entering(),
          onClick: () => void enter(),
        },
      ]}
    >
      <div class="divide-y divide-line">
        <For each={rows()}>
          {(row) => (
            <div class="flex min-w-0 items-center gap-3 px-3 py-2">
              <Text variant="label" class="shrink-0">
                {row.label}
              </Text>
              <Text
                variant="micro"
                tone="bright"
                class="ml-auto min-w-0 truncate text-right font-mono uppercase"
              >
                {row.value}
              </Text>
              <MetaAction onClick={() => props.onEdit(row.step)}>
                edit
              </MetaAction>
            </div>
          )}
        </For>
      </div>
    </StepShell>
  );
}
