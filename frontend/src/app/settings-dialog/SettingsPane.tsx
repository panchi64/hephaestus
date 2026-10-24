import { Show, type JSX } from "solid-js";
import {
  AgentToolsPanel,
  AppearanceSection,
  ChatSection,
  ClearDataSection,
  ModelsScreen,
  OfflineSection,
  PlatformVmSection,
  SearchProvidersPanel,
} from "~/features/settings";
import { McpScreen } from "~/features/mcp";
import { MemoryTimelineScreen } from "~/features/memory";
import { ProjectsScreen } from "~/features/projects";
import { AgentsSection } from "~/features/agents";
import { PluginsSection } from "~/features/plugins";
import { SkillsSection } from "~/features/skills";
import { WorkflowsSection } from "~/features/workflows";
import { VaultScreen } from "~/features/vault";
import { ApiTokensScreen } from "~/features/tokens";
import { AccessTokensScreen } from "~/features/access-tokens";
import { IntegrationsScreen } from "~/features/integrations";
import { BackupScreen } from "~/features/backup";
import { HealthScreen } from "~/features/health";
import type { SettingsSection } from "./sections";

/**
 * The one place a section id becomes a component. `sections.ts` says what
 * exists and in what order; this says what each entry renders, and nothing else
 * knows both — so adding a section is one line in each file, and forgetting the
 * line here is what the dev warning below catches. It is a warning rather than a
 * test because importing this map constructs every feature's resources at module
 * load, and the unit suite has no DOM to hold them.
 *
 * Every value here is a **thunk**, not an element. Solid evaluates JSX eagerly,
 * so a map of elements would construct all sixteen sections — and create every
 * resource behind them — the moment this module is read, which is exactly the
 * cost the dialog's mount-only-while-open gate exists to avoid.
 */
export const SECTION_RENDERERS: Record<string, () => JSX.Element> = {
  appearance: () => <AppearanceSection />,
  chat: () => <ChatSection />,
  offline: () => <OfflineSection />,
  "platform-vm": () => <PlatformVmSection />,
  models: () => <ModelsScreen />,
  "agent-tools": () => <AgentToolsPanel />,
  skills: () => <SkillsSection />,
  agents: () => <AgentsSection />,
  plugins: () => <PluginsSection />,
  workflows: () => <WorkflowsSection />,
  projects: () => <ProjectsScreen />,
  memory: () => <MemoryTimelineScreen />,
  mcp: () => <McpScreen />,
  integrations: () => <IntegrationsScreen />,
  "web-search": () => <SearchProvidersPanel />,
  vault: () => <VaultScreen />,
  "service-keys": () => <ApiTokensScreen />,
  "access-tokens": () => <AccessTokensScreen />,
  backup: () => <BackupScreen />,
  data: () => <ClearDataSection />,
  health: () => <HealthScreen />,
};

/**
 * The dialog's right column: the picked section, and only it.
 *
 * Keyed on the section id, so switching **remounts** the pane rather than
 * diffing one section into another. That is deliberate: these sections own
 * resources and local form state, and a section you navigate away from should
 * let go of both — the same reasoning that puts the whole dialog behind a mount
 * gate, applied one level down.
 */
export function SettingsPane(props: { section: SettingsSection }): JSX.Element {
  return (
    <Show keyed when={props.section}>
      {(section) => {
        const render = SECTION_RENDERERS[section.id];
        // A section listed in `sections.ts` with no renderer here is a silently
        // blank pane — the exact failure a build cannot see. Say so in dev; in
        // production, render nothing rather than throw.
        if (!render && import.meta.env.DEV)
          console.warn(
            `[settings] no renderer for section "${section.id}" — see SettingsPane.tsx`,
          );
        return <Show when={render}>{render?.()}</Show>;
      }}
    </Show>
  );
}
