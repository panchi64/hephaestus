import { createSignal, type Accessor } from "solid-js";
import { readLS, writeLS } from "~/lib/storage";

const STORAGE_KEY = "heph.sidebar.width";
const DEFAULT_WIDTH = 248;
const MIN_WIDTH = 208;
const MAX_WIDTH = 420;

export function clampWidth(w: number): number {
  return Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Math.round(w)));
}

function load(): number {
  const raw = readLS(STORAGE_KEY);
  const n = raw ? Number(raw) : NaN;
  return Number.isFinite(n) ? clampWidth(n) : DEFAULT_WIDTH;
}

/** The rail's dragged width. The live signal tracks the drag; only the settled
 *  value is written. */
export function useSidebarWidth(): {
  width: Accessor<number>;
  resize: (deltaX: number) => void;
  persist: () => void;
} {
  const [width, setWidth] = createSignal(load());
  return {
    width,
    resize: (deltaX) => setWidth((w) => clampWidth(w + deltaX)),
    persist: () => writeLS(STORAGE_KEY, String(width())),
  };
}
