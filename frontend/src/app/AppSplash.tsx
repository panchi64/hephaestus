import { type JSX } from "solid-js";
import { LoadingText, Stack, Text } from "~/ui";

/**
 * The whole-screen holding state, shown while the app has nothing truthful to
 * render yet: the session boot probe deciding whether the workspace is locked,
 * and the first paint after unlock while the shell's cold resources land.
 *
 * One component for both because they are the same moment to the operator — the
 * app exists but cannot yet say anything about itself — and because a second copy
 * is how the two drift apart.
 */
export function AppSplash(props: { label?: string }): JSX.Element {
  return (
    <div class="flex h-screen items-center justify-center bg-bg">
      <Stack gap={1} class="items-center">
        <Text variant="display" tone="bright">
          Odysseus
        </Text>
        <LoadingText label={props.label ?? "Establishing link…"} />
      </Stack>
    </div>
  );
}
