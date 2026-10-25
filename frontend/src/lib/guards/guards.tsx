import { Show, type JSX } from "solid-js";
import { AuthLayout } from "~/app/AuthLayout";
import { AppSplash } from "~/app/AppSplash";
import { LoginScreen, SignupScreen } from "~/features/auth";
import { OnboardingGate } from "~/features/onboarding";
import { useSession } from "../stores/session";

/**
 * The auth gate. Single operator, so there is one question: is the workspace
 * unlocked? While the boot probe runs we hold on a splash; an un-unlocked
 * workspace gets its entry surface — setup on first run, otherwise unlock.
 *
 * **The gate renders those screens itself; it does not redirect to them.** It
 * used to `<Navigate>` to `/login`, which made unlocking a *route transition* —
 * and Solid commits a transition only once every resource read in the new tree
 * has settled, showing no fallback in the meantime. The shell's cold
 * `/projects` and `/conversations` reads therefore pinned the transition and the
 * login form stayed on screen until the operator reloaded the page by hand. As a
 * branch of one already-mounted route, unlocking is a plain signal flip: the
 * subtree swaps in the same tick and the shell's resources suspend normally,
 * against a fallback the operator can see.
 *
 * Behind unlock sits the first-run walkthrough (`OnboardingGate`), a branch of the same kind.
 *
 * `/login` and `/signup` still resolve (bookmarks, an old toast) — they redirect
 * to `/` and land here.
 */
export function RequireAuth(props: { children: JSX.Element }): JSX.Element {
  const session = useSession();
  return (
    <Show when={session.status !== "loading"} fallback={<AppSplash />}>
      <Show
        when={session.isAuthenticated}
        fallback={
          <AuthLayout>
            <Show
              when={session.status === "uninitialized"}
              fallback={<LoginScreen />}
            >
              <SignupScreen />
            </Show>
          </AuthLayout>
        }
      >
        <OnboardingGate>{props.children}</OnboardingGate>
      </Show>
    </Show>
  );
}
