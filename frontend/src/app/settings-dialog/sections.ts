import type { IconName } from "~/ui";

/**
 * One configuration surface — a row in the dialog's left column, and the pane
 * that appears on the right when it is picked.
 *
 * `id` is what the URL carries (`?settings=<id>`), so it is a slug and not a
 * label — a deep link names it, and a renderer keys off it.
 *
 * The **title** of the pane is not here. Every section component already carries
 * its own `PageHeader variant="section"` or `Panel label`, and a second copy in
 * this file would be the one that goes stale — `label` is the row's name in the
 * column, and `description` is what the row's tooltip and the search field match
 * against.
 */
export interface SettingsSection {
  id: string;
  /** Left-column row label. */
  label: string;
  icon: IconName;
  /** One line saying what the section is for — tooltip text, and matched when
   *  searching the column. */
  description: string;
  /** Extra search terms: the words an operator would type looking for this pane
   *  that appear in neither its label nor its description ("api key", "endpoint",
   *  "theme"). Searching is how a sixteen-row column stays findable, so a section
   *  that owns a concept it doesn't name has to say so here. */
  keywords?: string[];
}

/** A heading in the left column with its sections beneath it. Groups are
 *  collapsible, never a destination: they carry no pane of their own, because a
 *  category that is also a page is a second place the same settings live. */
export interface SettingsGroup {
  id: string;
  label: string;
  sections: SettingsSection[];
}

/**
 * Every configuration surface, grouped. This file is **data** — the same
 * discipline `nav/areas.ts` keeps: what exists and how it is labelled lives
 * here, and the components that render it derive everything else.
 *
 * Each row is one surface, not an umbrella of unrelated sections: the column lists
 * the things an operator comes to change, the groups only sort them, and the
 * search field skips the hierarchy altogether.
 *
 * Ordering is deliberate rather than alphabetical: GENERAL first because it is
 * what an operator opens the dialog for most often, SYSTEM last because it is
 * where you go when something is already wrong.
 */
export const SETTINGS_GROUPS: SettingsGroup[] = [
  {
    id: "general",
    label: "GENERAL",
    sections: [
      {
        id: "appearance",
        label: "Appearance",
        icon: "sun",
        description: "Theme and accent colors",
        keywords: ["theme", "dark", "light", "ink", "paper", "color", "accent"],
      },
      {
        id: "chat",
        label: "Chat",
        icon: "chat",
        description: "How conversations behave — compaction, limits, timeouts",
        keywords: ["compact", "context", "window", "timeout", "limit"],
      },
      {
        id: "offline",
        label: "Offline mode",
        icon: "system",
        description: "Whether the agent's web tools are suspended",
        keywords: ["network", "connectivity", "air-gapped", "web"],
      },
    ],
  },
  {
    id: "agent",
    label: "AGENT",
    sections: [
      {
        id: "models",
        label: "Models",
        icon: "cpu",
        description:
          "Which model answers, and the endpoints they are reached through",
        keywords: [
          "endpoint",
          "provider",
          "openai",
          "anthropic",
          "embedding",
          "decision model",
          "fallback",
          "context window",
          "api key",
        ],
      },
      {
        id: "agent-tools",
        label: "Agent tools",
        icon: "grid",
        description: "The built-in tools the agent may reach for",
        keywords: ["shell", "code", "files", "approval", "capabilities"],
      },
      {
        id: "skills",
        label: "Skills",
        icon: "note",
        description: "Reusable instruction bundles the agent can load",
        keywords: ["prompt", "instructions", "playbook"],
      },
      {
        id: "agents",
        label: "Agents",
        icon: "cpu",
        description: "Named agents a thread can run as, with their own limits",
        keywords: ["custom", "persona", "subagent", "delegate", "instructions"],
      },
      {
        id: "plugins",
        label: "Plugins",
        icon: "plug",
        description:
          "Python tools agents write, which run in the platform VM once you publish them",
        keywords: ["tool", "python", "extension", "egress", "secret", "draft"],
      },
      {
        id: "workflows",
        label: "Workflows",
        icon: "terminal",
        description: "Prompts you invoke by name from the composer's / menu",
        // "slash" and "command" are what an operator types looking for this, and
        // neither appears in the label or the description — the row is about the
        // things, not about the punctuation that reaches them.
        keywords: ["slash", "command", "shortcut", "template", "snippet"],
      },
      {
        id: "projects",
        label: "Projects",
        icon: "layers",
        description: "Workspaces that scope chats, files, and memory",
        keywords: ["workspace", "folder", "scope", "path"],
      },
      {
        id: "memory",
        label: "Memory",
        icon: "database",
        description: "The long-term facts Talos remembers about you",
        keywords: ["facts", "recall", "remember", "forget"],
      },
    ],
  },
  {
    id: "connections",
    label: "CONNECTIONS",
    sections: [
      {
        id: "mcp",
        label: "MCP connections",
        icon: "plug",
        description: "Model Context Protocol servers and the tools they expose",
        keywords: [
          "model context protocol",
          "server",
          "stdio",
          "sse",
          "transport",
          "external tools",
        ],
      },
      {
        id: "integrations",
        label: "Integrations",
        icon: "link",
        description: "Third-party connectors the agent can act through",
        keywords: ["connector", "service", "webhook", "oauth"],
      },
      {
        id: "web-search",
        label: "Web search",
        icon: "search",
        description: "The search providers the agent queries",
        keywords: ["searxng", "provider", "internet"],
      },
    ],
  },
  {
    id: "security",
    label: "SECURITY",
    sections: [
      {
        id: "vault",
        label: "Secrets vault",
        icon: "lock",
        description: "Encrypted secrets this workspace holds",
        keywords: ["password", "secret", "encryption", "unlock"],
      },
      {
        id: "service-keys",
        label: "Service keys",
        icon: "key",
        description: "Outbound keys for the third-party services you use",
        keywords: ["api key", "credential", "token", "outbound"],
      },
      {
        id: "access-tokens",
        label: "Access tokens",
        icon: "terminal",
        description: "Inbound scoped tokens for reaching this workspace",
        keywords: ["api", "bearer", "scope", "inbound", "cli"],
      },
    ],
  },
  {
    id: "system",
    label: "SYSTEM",
    sections: [
      {
        id: "backup",
        label: "Backup",
        icon: "archive",
        description: "Snapshots of this workspace, and restoring from one",
        keywords: ["export", "import", "restore", "snapshot"],
      },
      {
        id: "data",
        label: "Data",
        icon: "trash",
        description: "Delete all chats, memories, notifications or uploads",
        keywords: ["clear", "delete", "wipe", "reset", "history", "privacy"],
      },
      {
        id: "platform-vm",
        label: "Platform VM",
        icon: "cpu",
        description: "The VM that runs agent code, previews and web tools",
        keywords: ["sandbox", "reset", "restart", "runtime", "virtual machine"],
      },
      {
        id: "health",
        label: "Health",
        icon: "activity",
        description: "The state of the machine and its services",
        keywords: ["status", "diagnostics", "latency", "uptime"],
      },
    ],
  },
];

/** Every section, in column order. */
export function allSections(): SettingsSection[] {
  return SETTINGS_GROUPS.flatMap((g) => g.sections);
}

/** Every section id — what the renderer map is checked against. */
export function allSectionIds(): string[] {
  return allSections().map((s) => s.id);
}

/** The section a `?settings=` value names. An unknown slug lands on the first
 *  section rather than on an empty pane, so a hand-typed URL still opens the
 *  dialog on something. */
export function sectionById(id: string | undefined): SettingsSection {
  return allSections().find((s) => s.id === id) ?? allSections()[0];
}

/** The group a section sits under — the breadcrumb the dialog shows, and the
 *  meta on a search result row. */
export function groupForSection(id: string): SettingsGroup | undefined {
  return SETTINGS_GROUPS.find((g) => g.sections.some((s) => s.id === id));
}
