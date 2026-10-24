/**
 * What the ambient field (§11.1) is showing: the platform's load, its live sub-agents
 * and the lightning still flashing. State only; `~/app/ambient` decides what counts as
 * an event and where on the planet it strikes.
 */
import { createSignal } from "solid-js";
import { STRIKE_LIFE_MS, type Strike, type StrikeTone } from "~/ui";

const [load, setLoad] = createSignal(0);
const [aurora, setAurora] = createSignal(0);
const [strikes, setStrikes] = createSignal<readonly Strike[]>([]);
let nextId = 0;

/** Strike at a viewport point (CSS px). Strikes already over are pruned. */
export function pushStrike(x: number, y: number, tone: StrikeTone): void {
  const at = performance.now();
  setStrikes((list) => [
    ...list.filter((s) => at - s.at <= STRIKE_LIFE_MS),
    { id: nextId++, at, x, y, tone },
  ]);
}

export {
  load as ambientLoad,
  setLoad as setAmbientLoad,
  aurora as ambientAurora,
  setAurora as setAmbientAurora,
  strikes as ambientStrikes,
};
