/**
 * What the ambient field (§11.1) is showing: the platform's load and the glows still
 * alive. State only; `~/app/ambient` decides what counts as an event and where it
 * happened.
 */
import { createSignal } from "solid-js";
import { GLOW_LIFE_MS, type Pulse, type PulseTone } from "~/ui";

const [load, setLoad] = createSignal(0);
const [pulses, setPulses] = createSignal<readonly Pulse[]>([]);
let nextId = 0;

/** Start a glow at a viewport point (CSS px). Glows already over are pruned. */
export function pushPulse(x: number, y: number, tone: PulseTone): void {
  const at = performance.now();
  setPulses((list) => [
    ...list.filter((p) => at - p.at <= GLOW_LIFE_MS),
    { id: nextId++, at, x, y, tone },
  ]);
}

export {
  load as ambientLoad,
  setLoad as setAmbientLoad,
  pulses as ambientPulses,
};
