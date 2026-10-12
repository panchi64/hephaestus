import { createMemo, type JSX } from "solid-js";
import { Select, Tooltip } from "~/ui";
import type { Project } from "~/lib/stores/projects";
import { directoryLabel } from "../addDirectory";

/** The composer status-bar cell choosing which project's folder the next thread works in. */
export function ProjectControl(props: {
  projects: readonly Project[];
  value: string | undefined;
  onChange: (projectId: string | undefined) => void;
}): JSX.Element {
  const options = createMemo(() => [
    { value: "", label: "No project" },
    ...props.projects.map((p) => ({
      value: p.id,
      label: directoryLabel(p),
      description: p.rootPath,
    })),
  ]);

  return (
    <Tooltip delay={600} side="top" label="Work in this project's folder">
      <Select
        cell
        placeholder="No project"
        value={props.value ?? ""}
        onChange={(v) => props.onChange(v || undefined)}
        options={options()}
        aria-label="Project"
      />
    </Tooltip>
  );
}
