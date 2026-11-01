import type { JSX } from "solid-js";
import { PageHeader } from "~/ui";
import { sectionHeading } from "./sections";

export interface SettingsPageHeaderProps {
  /** Id of the `sections.ts` entry this pane renders; title and subtitle come
   *  from it, so the pane and the nav row can't disagree. */
  section: string;
  actions?: JSX.Element;
  status?: JSX.Element;
}

export function SettingsPageHeader(
  props: SettingsPageHeaderProps,
): JSX.Element {
  const heading = () => sectionHeading(props.section);
  return (
    <PageHeader
      variant="section"
      title={heading()?.label ?? props.section}
      subtitle={heading()?.description}
      actions={props.actions}
      status={props.status}
    />
  );
}
