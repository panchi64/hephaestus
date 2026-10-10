import { isMac } from "~/lib/keymap";

// The send chord's glyph, for the button's hint. Read once: the platform does not
// change under a running page.
export const IS_MAC = isMac();
export const SEND_CHORD = IS_MAC ? "⌘↵" : "Ctrl↵";
