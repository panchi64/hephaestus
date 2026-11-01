import { createEffect, onCleanup, onMount } from "solid-js";

export interface AutosizeOptions {
  /** The most lines the field grows to before it scrolls. */
  maxRows: number;
  /** The most of the window's height it may take, whichever cap is smaller. */
  maxViewportShare: number;
}

/**
 * Grow a textarea to fit its content, from one row up to the smaller of `maxRows`
 * and `maxViewportShare` of the window, then scroll. With `field-sizing: content`
 * the browser does the growing and only the cap is set, again when the window or
 * the field resizes.
 * Otherwise the field is re-measured whenever `value` changes — typing, a draft
 * load, a clear after send — on window resize, and whenever its width changes,
 * since a narrower field wraps onto more lines.
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
  /** The field's border-box cap and its border, from computed style alone. */
  const bounds = (el: HTMLTextAreaElement) => {
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
    return { max, border };
  };

  // Where the engine sizes a field to its content, the browser grows it during its
  // own layout and only the cap is ours: no measure, so no forced layout per keystroke.
  if (typeof CSS !== "undefined" && CSS.supports("field-sizing", "content")) {
    const cap = () => {
      const el = field();
      if (el) el.style.maxHeight = `${bounds(el).max}px`;
    };
    onMount(() => {
      const el = field();
      if (!el) return;
      el.style.setProperty("field-sizing", "content");
      el.style.overflowY = "auto";
      cap();
      window.addEventListener("resize", cap);
      onCleanup(() => window.removeEventListener("resize", cap));
      // A line-height or padding change resizes the field; the cap follows it.
      if (typeof ResizeObserver === "undefined") return;
      const observer = new ResizeObserver(cap);
      observer.observe(el);
      onCleanup(() => observer.disconnect());
    });
    return;
  }

  const autosize = () => {
    const el = field();
    if (!el) return;
    el.style.height = "auto";
    const { max, border } = bounds(el);
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
