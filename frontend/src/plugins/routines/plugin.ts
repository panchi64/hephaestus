import type { BuiltinPluginUi } from "../types";

/** The `source` a routine's previous-run summary is fenced with; mirrors the routines
 *  plugin's executor. */
const PREVIOUS_RUN_SOURCE = "the routine's previous run";

export const routinesPlugin: BuiltinPluginUi = {
  name: "routines",
  nav: [
    {
      label: "Routines",
      href: "/tasks",
      icon: "clock",
      connected: true,
      description: "Scheduled agent routines and reminders",
    },
  ],
  toolCategory: {
    name: "routines",
    icon: "clock",
    preparing: "Preparing a routine",
  },
  envelopes: {
    [PREVIOUS_RUN_SOURCE]: { label: "Previous run" },
  },
};
