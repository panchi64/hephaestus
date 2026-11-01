import { Show, type JSX } from "solid-js";
import { SettingsPageHeader } from "~/app/settings-dialog/SettingsPageHeader";
import { Button, Stack, Tabs, toast } from "~/ui";
import { isApiError } from "~/lib/api";
import { useTabParam } from "~/lib/useTabParam";
import { reopenOnboarding } from "~/features/onboarding/data";
import { EndpointsSection } from "../components/EndpointsSection";
import { FallbackChainsSection } from "../components/FallbackChainsSection";
import { ModelAssignments } from "../components/ModelAssignments";

/** The one home for "which model does what".
 *
 *  Every model is reached through an **endpoint** — an OpenAI-compatible URL the
 *  operator points at, whether a server on this machine or a lab's API. What a model
 *  runs on is decided where it runs; Hephaestus serves nothing itself.
 *
 *  **Two tabs, not a disclosure.** Adding an endpoint is the first thing anyone does
 *  here, so ENDPOINTS is a named half of the pane rather than a collapsed control. The
 *  tab rides in the URL (`?models=endpoints`), so a link can point at the endpoint list
 *  directly and the back button undoes a switch. */
const TABS = ["assignments", "endpoints"] as const;

export function ModelsScreen(): JSX.Element {
  const [tab, setTab] = useTabParam("models", TABS, "assignments");

  const runWalkthrough = async (): Promise<void> => {
    try {
      await reopenOnboarding();
    } catch (e) {
      toast.error(
        isApiError(e) ? e.detail : "Unable to open the setup walkthrough.",
      );
    }
  };

  return (
    <Stack gap={6}>
      <SettingsPageHeader
        section="models"
        actions={
          <Button size="sm" onClick={() => void runWalkthrough()}>
            Run setup walkthrough
          </Button>
        }
      />

      <Tabs
        items={[
          { value: "assignments", label: "Assignments" },
          { value: "endpoints", label: "Endpoints" },
        ]}
        value={tab()}
        onChange={(v) => setTab(v as (typeof TABS)[number])}
      />

      <Show when={tab() === "assignments"}>
        <ModelAssignments />
      </Show>

      <Show when={tab() === "endpoints"}>
        <Stack gap={6}>
          <EndpointsSection />
          <FallbackChainsSection />
        </Stack>
      </Show>
    </Stack>
  );
}
