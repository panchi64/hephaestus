import { createContext, useContext } from "solid-js";

/** Whether a row is arriving — provided by its `Branch` while it traces in, so every
 *  kind of row on a trunk arrives the same way. A nested `Branch` provides its own
 *  value, so a script's calls do not inherit their parent's arrival. */
export const ArrivalContext = createContext(false);

const SWEEP = "heph-trace-sweep";

function ownsText(el: Element): boolean {
  for (const node of el.childNodes)
    if (node.nodeType === Node.TEXT_NODE && node.textContent?.trim())
      return true;
  return false;
}

/** Runs the arrival sweep (`[data-sweep]`) across every text segment of a row.
 *
 *  Each segment clips the band to its own glyphs, so each is told the row's width and
 *  its own offset in it, which lines the clips up into one band. Measured a frame
 *  later, once the row is laid out. */
function sweepText(row: HTMLElement): void {
  requestAnimationFrame(() => {
    if (!row.isConnected) return;
    const box = row.getBoundingClientRect();
    row.style.setProperty("--sweep-w", `${box.width}px`);
    for (const el of row.querySelectorAll<HTMLElement>("*")) {
      if (el.closest("svg") || !ownsText(el)) continue;
      el.style.setProperty(
        "--sweep-x",
        `${el.getBoundingClientRect().left - box.left}px`,
      );
      el.dataset.sweep = "";
      const done = (e: AnimationEvent): void => {
        if (e.target !== el || e.animationName !== SWEEP) return;
        delete el.dataset.sweep;
        el.removeEventListener("animationend", done);
      };
      el.addEventListener("animationend", done);
    }
  });
}

/** A row's side of the arrival (§8): read once, since an arrival is the row's mount.
 *  `body` fades the row in, `socket` charges its glyph, and `sweep` is the ref of the
 *  element whose text the band crosses. */
export function useArrival(): {
  body: string | undefined;
  socket: string | undefined;
  sweep: (el: HTMLElement) => void;
} {
  const arriving = useContext(ArrivalContext);
  return {
    body: arriving ? "heph-trace-body" : undefined,
    socket: arriving ? "heph-trace-charge-socket" : undefined,
    sweep: (el) => {
      if (arriving) sweepText(el);
    },
  };
}
