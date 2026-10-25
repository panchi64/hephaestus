import type { NavItem } from "~/app/nav/types";
import type { EnvelopeLabel } from "~/features/chat/envelopeLabels";
import type { ToolEntry } from "~/features/chat/toolPresentation";
import type { IconName } from "~/ui";

/** The UI a built-in plugin brings to the shell. Each field is one kind of
 *  contribution the shell knows how to render; a plugin fills in the ones it has. */
export interface BuiltinPluginUi {
  /** The backend plugin's name, as `/plugins/builtin` lists it. */
  name: string;
  /** Pages it adds to the rail's COMMS area. */
  nav?: NavItem[];
  /** Its tool category's glyph and preparing phrase in the transcript. */
  toolCategory?: { name: string; icon: IconName; preparing: string };
  /** Transcript rows for its tools, by namespaced tool name. */
  tools?: Record<string, ToolEntry>;
  /** How an untrusted part its turns carry is labelled, by the part's source. */
  envelopes?: Record<string, EnvelopeLabel>;
}
