import { createContext, useContext } from "solid-js";

/** Where a step hands its briefing to the one `TalosBriefing` the walkthrough keeps
 *  mounted. One instance for every step is what lets text that stays the same across a
 *  re-render, a form opening, or a pick stay on screen instead of being revealed again. */
export const BriefingSlot = createContext<(text: string) => void>();

export function useBriefingSlot(): (text: string) => void {
  const set = useContext(BriefingSlot);
  if (!set) throw new Error("a walkthrough step rendered outside its screen");
  return set;
}
