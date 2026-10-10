import { type JSX } from "solid-js";
import { useNavigate } from "@solidjs/router";
import { SESSION_MODES, type SessionMode } from "~/lib/modes";
import { Segmented, type SegmentedOption } from "~/ui";
import { isMac, registerKeymap } from "~/lib/keymap";
import { modeShortcutCombo, modeShortcutLabel } from "../modeShortcuts";
import {
  activeSessionMode,
  setActiveSessionMode,
} from "~/lib/stores/sessionMode";

/** The registry's modes as the control's options — built once, since the set is
 *  static and rebuilding it per render would remake the array every time the
 *  mode changed. */
const OPTIONS: SegmentedOption<SessionMode>[] = SESSION_MODES.map(
  (spec, i) => ({
    value: spec.id,
    label: spec.label,
    icon: spec.icon,
    description: spec.description,
    // The shortcut lives in the tooltip only: the bar is three words, and a key
    // printed in each cell would crowd the one strong element at the rail's head.
    hint: `${spec.description} (${modeShortcutLabel(i, isMac())})`,
  }),
);

/**
 * **Which kind of work you are looking at** — the rail's first control, above the
 * threads it arranges.
 *
 * It used to be a dropdown in the composer, shown only while a thread was still
 * unsaved, because the mode was a property of the message being sent. It is not:
 * it is a property of the *thread*, and therefore of the list of threads. Here it
 * does one thing that reads as three — it files what the rail shows, decides what
 * the next new thread will be, and repaints the signature accent through
 * `data-mode` — because those are the same fact stated at three ranges.
 *
 * The mode of a thread already saved is still immutable and still the backend's:
 * a code thread owns a git branch, and re-pointing it would strand that branch.
 * Opening one moves this switch to match it rather than the other way round.
 *
 * **It no longer asks which directory a code thread will work in.** That line lived
 * here for as long as the directory was something chosen *after* pressing NEW; now the
 * directory comes first and the thread is started from its own section of the rail, so
 * the question is asked where the answer already is. This is the mode switch and
 * nothing else.
 */
export function SessionModeSwitch(): JSX.Element {
  const mode = activeSessionMode;
  const navigate = useNavigate();
  /** Pick a mode, and go where that mode's threads are.
   *
   *  Filing the rail without moving was right while a Chat row sat above this one
   *  to do the moving. With that row gone this is the way in, and a control that
   *  re-sorts a list you cannot see is not one. Navigating from `/chat` is a no-op
   *  and leaves the open thread alone, so the only case it changes is the one
   *  where the operator is looking at something else. */
  const setMode = (id: Parameters<typeof setActiveSessionMode>[0]): void => {
    setActiveSessionMode(id);
    navigate("/chat");
  };

  registerKeymap(() =>
    OPTIONS.map((option, i) => ({
      combo: modeShortcutCombo(i),
      run: () => setMode(option.value),
    })),
  );

  return (
    <Segmented
      variant="ruled"
      aria-label="Session mode"
      options={OPTIONS}
      value={mode()}
      onChange={setMode}
    />
  );
}
