import { Show, type JSX } from "solid-js";
import { AuthLayout } from "~/app/AuthLayout";
import { SplashHold } from "~/app/Splash";
import { LoginScreen, SignupScreen } from "~/features/auth";
import { OnboardingGate } from "~/features/onboarding";
import { useSession } from "../stores/session";

/**
 * The auth gate. Single operator, so there is one question: is the workspace
 * unlocked? While the boot probe runs we hold on a splash; an un-unlocked
 * workspace gets its entry surface — setup on first run, otherwise unlock.
 *
 * **The gate renders those screens itself; it does not redirect to them.** A
 * redirect makes unlocking a route transition, which Solid commits only once every
 * resource in the new tree settles, with no fallback meanwhile — the shell's cold
 * reads would pin the login form on screen. As a branch of one mounted route,
 * unlocking is a signal flip and the shell suspends against a visible fallback.
 *
 * Behind unlock sits the first-run walkthrough (`OnboardingGate`), a branch of the same kind.
 *
 * `/login` and `/signup` still resolve (bookmarks, an old toast) — they redirect
 * to `/` and land here.
 */
export function RequireAuth(props: { children: JSX.Element }): JSX.Element {
  const session = useSession();
  return (
    <Show when={session.status !== "loading"} fallback={<SplashHold />}>
      <Show
        when={session.isAuthenticated}
        fallback={
          // Keyed so a swap between setup and unlock builds a fresh plate.
          <Show
            when={session.status === "uninitialized" ? "setup" : "unlock"}
            keyed
          >
            {(screen) => (
              <AuthLayout>
                {screen === "setup" ? <SignupScreen /> : <LoginScreen />}
              </AuthLayout>
            )}
          </Show>
        }
      >
        <OnboardingGate>{props.children}</OnboardingGate>
      </Show>
    </Show>
  );
}
