import { type JSX } from "solid-js";
import { ModelAssignments } from "~/features/settings/components/ModelAssignments";
import type { LocalServer } from "../model";
import { StatusBlock, type StatusLine } from "./StatusBlock";
import { StepShell } from "./StepShell";

/** Roles: background, decision, and search & memory on one page, each showing its
 *  default. Nothing is bound here unless the operator picks it. */
export function RolesStep(props: {
  servers: LocalServer[];
  onBack: () => void;
  onContinue: () => void;
}): JSX.Element {
  // An embedding model the scan saw is worth pointing at, never binding: binding one
  // starts a re-index the operator didn't ask for.
  const embedders = (): StatusLine[] =>
    props.servers.flatMap((s) =>
      s.models
        .filter((m) => m.kind === "embeddings")
        .map((m) => ({
          label: `${s.label} · ${m.id}`,
          value: "Embedding model",
        })),
    );

  return (
    <StepShell
      status={<StatusBlock label="Found on this machine" lines={embedders()} />}
      talos="Three smaller jobs. The defaults work; change any you care about."
      title="Assign the other roles"
      aside="Optional"
      buttons={[
        { label: "Back", onClick: props.onBack },
        {
          label: "Keep these",
          kbd: "↵",
          primary: true,
          onClick: props.onContinue,
        },
      ]}
    >
      <div class="p-3">
        <ModelAssignments roles={["utility", "decision", "embedding"]} />
      </div>
    </StepShell>
  );
}
