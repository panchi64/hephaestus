import { createSignal, onCleanup, type JSX } from "solid-js";
import { cx } from "../cx";

const STEPS = ["", ".", "..", "..."];
const STEP_MS = 400;

/** The step is read off the page clock, not counted, so every ellipsis shares one
 *  beat however often the clock stops and restarts — a remount never shifts it. */
const now = (): number =>
  Math.floor(performance.now() / STEP_MS) % STEPS.length;
const [step, setStep] = createSignal(0);

const motionQuery =
  typeof window === "undefined"
    ? undefined
    : window.matchMedia("(prefers-reduced-motion: reduce)");
const [reduced, setReduced] = createSignal(motionQuery?.matches ?? false);
motionQuery?.addEventListener("change", (e) => {
  setReduced(e.matches);
  resume();
});

let subscribers = 0;
let timer: ReturnType<typeof setTimeout> | undefined;

function tick(): void {
  timer = undefined;
  // A hidden tab shows nothing, so it ticks nothing (§8); `visibilitychange` resumes.
  if (subscribers === 0 || reduced() || document.hidden) return;
  setStep(now());
  timer = setTimeout(tick, STEP_MS - (performance.now() % STEP_MS));
}

function resume(): void {
  if (timer === undefined) tick();
}

function subscribe(): () => void {
  if (typeof document === "undefined") return () => {};
  subscribers += 1;
  if (subscribers === 1) document.addEventListener("visibilitychange", resume);
  resume();
  return () => {
    subscribers -= 1;
    if (subscribers > 0) return;
    document.removeEventListener("visibilitychange", resume);
    clearTimeout(timer);
    timer = undefined;
  };
}

/** A "working" label with trailing dots cycled by hard cuts (§8 machine register);
 *  a static `…` under reduced motion. The dots hang outside the label's box, so a
 *  centred label stays centred and never reflows as they change. */
export function Ellipsis(props: {
  children: JSX.Element;
  class?: string;
}): JSX.Element {
  onCleanup(subscribe());
  return (
    <span class={cx("relative", props.class)}>
      {props.children}
      <span class="absolute top-0 left-full" aria-hidden="true">
        {reduced() ? "…" : STEPS[step()]}
      </span>
    </span>
  );
}
