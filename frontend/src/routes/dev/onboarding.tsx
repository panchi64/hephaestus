import { createSignal, For, onCleanup, Show, type JSX } from "solid-js";
import { refresh } from "~/lib/stores/session";
import { OnboardingGate } from "~/features/onboarding";
import { installFakeBackend } from "~/dev/onboarding/fakeBackend";
import { SCENARIOS, scenarioById } from "~/dev/onboarding/scenarios";
import { Text } from "~/ui";

/**
 * Onboarding bench — a dev-only harness, not a product surface.
 *
 * The walkthrough only appears on a fresh workspace, behind a password, against a real
 * model server, so seeing it in the app means wiping a workspace each time. This route
 * is a sibling of `(app).tsx`, outside `RequireAuth`, and runs the real `OnboardingGate`
 * against an in-browser fake of every route it reaches (`~/dev/onboarding`), so no
 * backend, session or model is involved.
 *
 *   /dev/onboarding?scenario=local      LM Studio found (default)
 *                   &scenario=nothing   the scan finds nothing
 *                   &scenario=no-window a model with no context window
 *                   &scenario=failing   a configured endpoint that fails its test
 *                   &scenario=resume    required steps done; resumes at Roles
 *
 * A URL containing "fail" never passes the connection test; a cloud provider passes once
 * it has a key. State lives in the page, so a reload starts the scenario over.
 */
export default function OnboardingBench(): JSX.Element {
  if (!import.meta.env.DEV) return <div>the onboarding bench is dev-only</div>;

  const params = new URLSearchParams(window.location.search);
  const scenario = scenarioById(params.get("scenario"));
  // Installed during setup, before any child creates a resource that would fetch.
  const restore = installFakeBackend(scenario);
  onCleanup(restore);

  const [ready, setReady] = createSignal(false);
  void refresh().then(() => setReady(true));

  const open = (id: string): void => {
    window.location.assign(`/dev/onboarding?scenario=${id}`);
  };

  return (
    <>
      <Show when={ready()}>
        <OnboardingGate>
          <div class="flex h-screen items-center justify-center">
            <Text variant="body" tone="dim">
              Walkthrough finished — the app would open here.
            </Text>
          </div>
        </OnboardingGate>
      </Show>
      <div class="fixed right-3 top-3 z-50 flex max-w-xs flex-col gap-1 border border-line bg-raised px-3 py-2 font-mono text-micro text-dim">
        <span class="text-bright">onboarding bench · {scenario.label}</span>
        <span>{scenario.note}</span>
        <div class="flex flex-wrap gap-x-2">
          <For each={SCENARIOS}>
            {(s) => (
              <button
                type="button"
                class={s.id === scenario.id ? "text-bright" : "hover:text-text"}
                onClick={() => open(s.id)}
              >
                {s.id}
              </button>
            )}
          </For>
        </div>
      </div>
    </>
  );
}
