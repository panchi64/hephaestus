import type { NavArea, NavItem, RailRow } from "./types";

/**
 * The areas the rail still groups. There is exactly one, and that is the point:
 * the rail is the thread list now, and everything that was configuration moved
 * into the settings dialog (`app/settings-dialog/categories.ts`).
 *
 * COMMS survived as an area because its three surfaces are genuinely *places you
 * go and work* — an inbox, a schedule, a task list — not values you set. They
 * are pages for the same reason chat is.
 *
 * An `href` must appear in exactly one area: the active area is derived from the
 * current path, so a duplicated href would make that derivation ambiguous.
 */
export const AREAS: NavArea[] = [
  {
    id: "comms",
    label: "COMMS",
    icon: "mail",
    description: "Mail, schedule, and the agent's standing work",
    items: [
      {
        label: "Email",
        href: "/email",
        icon: "mail",
        description: "Read and send email",
        connected: true,
      },
      {
        label: "Calendar",
        href: "/calendar",
        icon: "calendar",
        connected: true,
        description: "View and manage your schedule",
      },
      {
        label: "Tasks",
        href: "/tasks",
        icon: "clock",
        connected: true,
        description: "Scheduled agent tasks and reminders",
      },
    ],
  },
];

/**
 * The surfaces that belong to no area.
 *
 * **This is the map, not the rail.** Being here says the app owns this route:
 * it is what tells the shell `/chat` is a real backed surface rather than
 * something to paint NOT CONNECTED over, and what names the page. Whether the
 * rail draws a row for it — and where — is a separate question with a separate
 * answer, in `RAIL_ROWS` below.
 *
 * Those two were one list until a row was removed from the rail and took the
 * route's registration with it, overlaying NOT CONNECTED across the whole of
 * chat. Placement and existence are not the same fact and no longer share a
 * declaration.
 *
 * Research is deliberately **not** here. It stopped being a surface that produces
 * reports and became a *mode a thread can be in*, so its home is the thread list —
 * a page of its own would list the same conversations twice under two names.
 *
 * MCP is deliberately **not** here. Registering a tool server and deciding which
 * of its tools may run is configuration — a value you set and leave — so it is a
 * section of the settings dialog, beside the other connections.
 *
 * Settings is deliberately **not** here. It opens the dialog rather than
 * navigating, so there is no `href` to name, and the rail's footer renders it
 * directly.
 */
export const CHAT: NavItem = {
  label: "Chat",
  href: "/chat",
  icon: "chat",
  connected: true,
  description: "Converse with local models and tool-using agents",
};

export const KNOWLEDGE_BASE: NavItem = {
  label: "Knowledge Base",
  href: "/rag",
  icon: "library",
  connected: true,
  description:
    "The unified retrieval corpus — every source the assistant can search (RAG)",
};

export const COMPARE: NavItem = {
  label: "Compare",
  href: "/compare",
  icon: "compare",
  connected: true,
  description: "Run the same prompt across models side by side",
};

/** Every surface outside an area. Order is the map's, not the rail's. */
export const LOOSE_SURFACES: NavItem[] = [CHAT, KNOWLEDGE_BASE, COMPARE];

/**
 * Where the rail draws a row, and in what order.
 *
 * A surface absent from this list simply has no row — it is still on the map, so
 * nothing about the route changes. That asymmetry is the point: rail layout can
 * be rearranged freely without any chance of un-registering a page.
 *
 * Rows carry the surface itself rather than an href to look up, so a row for a
 * surface that does not exist cannot be written.
 *
 * **Chat has no row.** One pointing at `/chat` sat directly above the mode
 * switch, the project switcher and the thread list — three controls that are all
 * *about* chat — so it read as a fourth sibling rather than as the destination
 * containing them. The mode switch is the honest entry point: picking a kind of
 * work is how you get to the work, and it files the list underneath at the same
 * time.
 *
 * **The other two sit in the footer**, on the same footing as COMMS and for the
 * same reason: a corpus and a compare bench are things you go and check, not the
 * thing you are doing.
 */
export const RAIL_ROWS: RailRow[] = [
  { slot: "footer", item: KNOWLEDGE_BASE },
  { slot: "footer", item: COMPARE },
];
