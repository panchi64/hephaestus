import type { JSX } from "solid-js";
import { useTheme } from "../theme/useTheme";
import type { ThemePreference } from "../theme/theme-store";
import { Icon } from "../primitives/Icon";
import type { IconName } from "../icons/registry";

/** Cycles theme preference: Phosphor (dark) → Paper (light) → Follow system. */
const ICON: Record<ThemePreference, IconName> = {
  phosphor: "moon",
  paper: "sun",
  system: "system",
};

const LABEL: Record<ThemePreference, string> = {
  phosphor: "Phosphor (dark)",
  paper: "Paper (light)",
  system: "Follow system",
};

export function ThemeToggle(): JSX.Element {
  const theme = useTheme();
  return (
    <button
      type="button"
      onClick={() => theme.toggle()}
      aria-label={`Theme: ${LABEL[theme.preference]}. Click to change.`}
      title={LABEL[theme.preference]}
      class="inline-flex items-center justify-center border border-line w-6 h-6 text-dim transition-colors hover:bg-raised hover:text-bright"
    >
      <Icon name={ICON[theme.preference]} size={12} />
    </button>
  );
}
