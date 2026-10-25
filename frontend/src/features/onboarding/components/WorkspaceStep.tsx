import { type JSX } from "solid-js";
import { Stack } from "~/ui";
import { AppearanceSection } from "~/features/settings/components/AppearanceSection";
import { PlatformVmSection } from "~/features/settings/components/PlatformVmSection";
import { SearchProvidersPanel } from "~/features/settings/components/SearchProvidersPanel";
import { StepShell } from "./StepShell";

/** Workspace: the Settings sections themselves, so a preference set here is the one
 *  Settings shows. */
export function WorkspaceStep(props: {
  onBack: () => void;
  onContinue: () => void;
}): JSX.Element {
  return (
    <StepShell
      talos="A few preferences. All of them can wait."
      title="Workspace preferences"
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
      <Stack gap={6} class="p-3">
        <AppearanceSection />
        <SearchProvidersPanel />
        <PlatformVmSection />
      </Stack>
    </StepShell>
  );
}
