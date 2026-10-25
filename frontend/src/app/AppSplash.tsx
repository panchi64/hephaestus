import { type JSX } from "solid-js";
import { LoadingText, Reveal, Stack, Text } from "~/ui";

/** Up to three splashes mount back to back (boot probe, onboarding gate, root
 *  Suspense); only the first fades in, so the run reads as one screen. */
let arrived = false;

/**
 * The whole-screen holding state, shown while the app has nothing truthful to
 * render yet: the session boot probe deciding whether the workspace is locked,
 * and the first paint after unlock while the shell's cold resources land.
 *
 * One component for both because they are the same moment to the operator — the
 * app exists but cannot yet say anything about itself — and because a second copy
 * is how the two drift apart.
 */
export function AppSplash(props: {
  /** After unlock: the workspace is open and the shell is loading. */
  waking?: boolean;
}): JSX.Element {
  const content = (
    <Stack gap={1} class="items-center">
      <Text variant="display" tone="bright">
        Hephaestus
      </Text>
      <LoadingText
        label={props.waking ? "Waking Talos" : "Establishing link"}
      />
    </Stack>
  );
  const first = !arrived;
  arrived = true;
  return (
    <div class="flex h-screen items-center justify-center bg-bg">
      {first ? <Reveal duration={320}>{content}</Reveal> : content}
    </div>
  );
}
