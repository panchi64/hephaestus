import { createSignal, onMount, Show, type JSX } from "solid-js";
import { AppSplash } from "~/app/AppSplash";
import { loadOnboarding, onboardingCompleted } from "./data";
import { OnboardingScreen } from "./OnboardingScreen";

/** Between unlock and the app: the walkthrough while it is owed. A signal-driven branch,
 *  not a route, for the same reason the auth gate is one. */
export function OnboardingGate(props: { children: JSX.Element }): JSX.Element {
  onMount(() => void loadOnboarding());
  return (
    <Show when={onboardingCompleted() !== null} fallback={<AppSplash waking />}>
      <Show when={onboardingCompleted()} fallback={<OnboardingScreen />}>
        {props.children}
        <GroundLift />
      </Show>
    </Show>
  );
}

/** The splash's ground lifting off the shell as it arrives. A cover that fades out
 *  rather than a fade on the shell itself: an animating opacity on the shell would
 *  make it a backdrop root, and its glass would frost nothing until the fade ended. */
function GroundLift(): JSX.Element {
  const [lifted, setLifted] = createSignal(false);
  return (
    <Show when={!lifted()}>
      <div
        class="pointer-events-none fixed inset-0 z-50 bg-bg"
        style={{
          "--reveal-blur": "0px",
          animation:
            "heph-reveal-fade-out var(--motion-stage) var(--ease) forwards",
        }}
        aria-hidden="true"
        onAnimationEnd={() => setLifted(true)}
      />
    </Show>
  );
}
