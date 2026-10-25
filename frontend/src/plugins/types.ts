import type { Component } from "solid-js";
import type { NavItem } from "~/app/nav/types";
import type { EnvelopeLabel } from "~/features/chat/envelopeLabels";
import type { ToolEntry } from "~/features/chat/toolPresentation";
import type { IconName } from "~/ui";

export interface PluginSettingsProps {
  /** Switches the settings dialog to another section, by id. */
  openSection: (id: string) => void;
}

/** A section a plugin adds to the settings dialog, shown only while it is on. */
export interface PluginSettingsSection {
  /** A URL slug, unique across every settings section. */
  id: string;
  label: string;
  icon: IconName;
  description: string;
  keywords?: string[];
  component: Component<PluginSettingsProps>;
}

/** The UI a built-in plugin brings to the shell. Each field is one kind of
 *  contribution the shell knows how to render; a plugin fills in the ones it has. */
export interface BuiltinPluginUi {
  /** The backend plugin's name, as `/plugins/builtin` lists it. */
  name: string;
  /** Mirrors the backend's `default_enabled`; assumed until the switches load. */
  defaultEnabled?: boolean;
  /** Pages it adds to the rail's COMMS area. */
  nav?: NavItem[];
  /** Its tool category's glyph and preparing phrase in the transcript. */
  toolCategory?: { name: string; icon: IconName; preparing: string };
  /** Transcript rows for its tools, by namespaced tool name. */
  tools?: Record<string, ToolEntry>;
  /** How an untrusted part its turns carry is labelled, by the part's source. */
  envelopes?: Record<string, EnvelopeLabel>;
  /** A section it adds to the settings dialog's CONNECTIONS group. */
  settings?: PluginSettingsSection;
}
