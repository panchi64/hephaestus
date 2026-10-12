import { For, Show, type JSX } from "solid-js";
import {
  ACCENT_TOKENS,
  Button,
  ColorField,
  Divider,
  Panel,
  Row,
  Stack,
  Text,
  ThemeToggle,
  accentOverrides,
  accentValue,
  hasAccentOverrides,
  isAccentOverridden,
  resetAccent,
  resetAllAccents,
  restoreAccents,
  setAccent,
  toast,
  useTheme,
} from "~/ui";

/** Which palette the accent editor is writing. Named in the machine's voice
 *  because it is the *resolved* mode — a fact about the current state, not the
 *  operator's three-way preference. */
const MODE_LABEL: Record<string, string> = {
  phosphor: "Phosphor",
  paper: "Paper",
};

export function AppearanceSection(): JSX.Element {
  const theme = useTheme();
  // The accents are per mode, and only the visible one is editable — choosing a
  // colour you cannot see it applied to is guesswork. Switching the theme above
  // switches which palette these five fields address, which is also the fastest
  // way to reach the other one.
  const mode = () => theme.resolved;

  const resetAll = () => {
    const previous = resetAllAccents();
    toast.success("Accent colors reset", {
      action: { label: "Undo", onClick: () => restoreAccents(previous) },
    });
  };

  return (
    <Stack gap={4}>
      <Panel label="Appearance">
        <Row align="center" justify="between">
          <Stack gap={1}>
            <Text variant="label" tone="default">
              Theme
            </Text>
            <Text variant="micro" tone="dim">
              Phosphor (dark), Paper (light), or follow system. Stored locally
              on this device.
            </Text>
          </Stack>
          <ThemeToggle />
        </Row>
      </Panel>

      <Panel label="Accent colors">
        <Stack gap={3}>
          <Row align="center" justify="between">
            <Stack gap={1}>
              <Text variant="micro" tone="dim">
                Editing the {MODE_LABEL[mode()] ?? mode()} palette. Each mode
                keeps its own colors — switch the theme above to edit the other.
                Stored locally on this device.
              </Text>
            </Stack>
            {/* Reads `accentOverrides()` so the control appears and disappears
                with the state it acts on. */}
            <Show when={accentOverrides() && hasAccentOverrides()}>
              <Button variant="ghost" size="sm" onClick={resetAll}>
                Reset all
              </Button>
            </Show>
          </Row>

          <Divider />

          <Stack gap={3}>
            <For each={ACCENT_TOKENS}>
              {(spec) => (
                <ColorField
                  label={spec.label}
                  description={spec.description}
                  mode={mode()}
                  value={accentValue(mode(), spec.token)}
                  // Dragging the OS picker fires `input` continuously: repaint
                  // every frame, write to storage only when it settles.
                  onInput={(hex) =>
                    setAccent(mode(), spec.token, hex, { persist: false })
                  }
                  onChange={(hex) => setAccent(mode(), spec.token, hex)}
                  onReset={
                    isAccentOverridden(mode(), spec.token)
                      ? () => resetAccent(mode(), spec.token)
                      : undefined
                  }
                />
              )}
            </For>
          </Stack>
        </Stack>
      </Panel>
    </Stack>
  );
}
