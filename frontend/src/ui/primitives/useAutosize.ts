import { createEffect, onCleanup, onMount } from "solid-js";

export interface AutosizeOptions {
  /** The most lines the field grows to before it scrolls. */
  maxRows: number;
  /** The most of the window's height it may take, whichever cap is smaller. */
  maxViewportShare: number;
}

/**
 * Grow a textarea to fit its content, from one row up to the smaller of `maxRows`
 * and `maxViewportShare` of the window, then scroll. Re-measured whenever `value`
 * changes — typing, a draft load, a clear after send — on window resize, since one
 * cap is a share of the window, and whenever the field's width changes, since a
 * narrower field wraps onto more lines. A behavior hook: no styling of its own.
 *
 * **The measure waits for the DOM.** `value` is the signal the field's own `value`
 * binding reads, and a write to it from inside another effect (a draft restored on
 * load, a thread switch) reaches this effect *before* the binding has put the text in
 * the element — so a synchronous measure read the old, empty field and sized a five
 * line draft to one row, where it stayed until the next keystroke. Deferring to a
 * microtask runs after the binding and still before paint, so typing never flickers.
 */
export function useAutosize(
  field: () => HTMLTextAreaElement | undefined,
  value: () => string,
  options: AutosizeOptions,
): void {
  const autosize = () => {
    const el = field();
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const line = parseFloat(cs.lineHeight) || 20;
    const padding = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    const border =
      parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
    const max = Math.max(
      line + padding + border,
      Math.min(
        window.innerHeight * options.maxViewportShare,
        line * options.maxRows + padding + border,
      ),
    );
    // `scrollHeight` covers content + padding but not border; the field is
    // border-box, so add the border back or the set height clips by that much.
    const fit = el.scrollHeight + border;
    el.style.height = `${Math.min(fit, max)}px`;
    el.style.overflowY = fit > max ? "auto" : "hidden";
  };
  createEffect(() => {
    value();
    queueMicrotask(autosize);
  });
  onMount(() => {
    window.addEventListener("resize", autosize);
    onCleanup(() => window.removeEventListener("resize", autosize));
    const el = field();
    if (!el || typeof ResizeObserver === "undefined") return;
    // Width only: the height is ours to set, and reacting to it would loop.
    let width = el.clientWidth;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      autosize();
    });
    observer.observe(el);
    onCleanup(() => observer.disconnect());
  });
}
