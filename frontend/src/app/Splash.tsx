import {
  createEffect,
  createMemo,
  createSignal,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import { useSession } from "~/lib/stores/session";
import { LoadingText, Reveal, Stack, Text } from "~/ui";

const [holds, setHolds] = createSignal(0);

/** Holds the splash up while mounted. Counted in the body rather than on mount, so a
 *  fallback handing over to the next one lands in the same update and the splash never drops. */
export function SplashHold(): JSX.Element {
  setHolds((n) => n + 1);
  onCleanup(() => setHolds((n) => n - 1));
  return null;
}

/**
 * The whole-screen holding state, shown while any gate holds it: the boot probe
 * deciding whether the workspace is locked, the walkthrough flag loading, and the
 * shell's cold reads after unlock. One layer for the whole run, because to the
 * operator it is one moment — the app exists but cannot yet say anything about itself.
 *
 * It fades in on its first appearance in a page load, cuts in after that (unlock),
 * and lifts off whatever screen replaces it.
 */
export function SplashHost(): JSX.Element {
  const session = useSession();
  const held = createMemo(() => holds() > 0);
  const [up, setUp] = createSignal(held());
  createEffect(() => {
    if (held()) setUp(true);
  });
  const [lifted, setLifted] = createSignal(false);

  const content = (): JSX.Element => (
    <Stack gap={1} class="items-center">
      <Text variant="display" tone="bright">
        Hephaestus
      </Text>
      <LoadingText
        label={session.isAuthenticated ? "Waking Talos" : "Establishing link"}
      />
    </Stack>
  );

  return (
    <Show when={up()}>
      {/* A sibling of the screens, never their ancestor: its animating opacity
          would make it a backdrop root, and their glass would frost nothing. */}
      <div
        class="fixed inset-0 z-50 flex items-center justify-center bg-bg"
        classList={{ "pointer-events-none": !held() }}
        style={
          held()
            ? undefined
            : {
                "--reveal-blur": "0px",
                animation:
                  "heph-reveal-fade-out var(--motion-stage) var(--ease) forwards",
              }
        }
        onAnimationEnd={(e) => {
          if (e.target !== e.currentTarget || held()) return;
          setUp(false);
          setLifted(true);
        }}
      >
        <Show when={!lifted()} fallback={content()}>
          <Reveal duration={320}>{content()}</Reveal>
        </Show>
      </div>
    </Show>
  );
}
