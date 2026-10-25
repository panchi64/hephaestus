import { onMount, Show, type JSX } from "solid-js";
import { AppSplash } from "~/app/AppSplash";
import { loadOnboarding, onboardingCompleted } from "./data";
import { OnboardingScreen } from "./OnboardingScreen";

/** Between unlock and the app: the walkthrough while it is owed. A signal-driven branch,
 *  not a route, for the same reason the auth gate is one. */
export function OnboardingGate(props: { children: JSX.Element }): JSX.Element {
  onMount(() => void loadOnboarding());
  return (
    <Show when={onboardingCompleted() !== null} fallback={<AppSplash />}>
      <Show when={onboardingCompleted()} fallback={<OnboardingScreen />}>
        {props.children}
      </Show>
    </Show>
  );
}
