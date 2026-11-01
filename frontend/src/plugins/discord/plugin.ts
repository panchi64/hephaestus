import type { BuiltinPluginUi } from "../types";
import { DiscordSection } from "./DiscordSection";

export const discordPlugin: BuiltinPluginUi = {
  name: "discord",
  defaultEnabled: false,
  settings: {
    id: "discord",
    label: "Discord",
    icon: "chat",
    description:
      "Talk to the agent from a Discord bot. Final answers are sent out through Discord.",
    keywords: ["bot", "dm", "channel", "chat", "message", "token"],
    component: DiscordSection,
  },
};
