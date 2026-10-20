/**
 * What the ambient field (§11.1) is showing: the platform's load and the rings still
 * travelling. State only; `~/app/ambient` decides what counts as an event and where it
 * happened.
 */
import { createSignal } from "solid-js";
import type { Pulse, PulseTone } from "~/ui";

/** A ring is gone by then; a pulse older than this is pruned on the next push. */
const PULSE_KEEP_MS = 3000;

const [load, setLoad] = createSignal(0);
const [pulses, setPulses] = createSignal<readonly Pulse[]>([]);
let nextId = 0;

/** Start a ring at a viewport point (CSS px). */
export function pushPulse(x: number, y: number, tone: PulseTone): void {
  const at = performance.now();
  setPulses((list) => [
    ...list.filter((p) => at - p.at < PULSE_KEEP_MS),
    { id: nextId++, at, x, y, tone },
  ]);
}

export {
  load as ambientLoad,
  setLoad as setAmbientLoad,
  pulses as ambientPulses,
};
