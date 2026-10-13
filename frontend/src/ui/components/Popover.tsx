import {
  Show,
  createEffect,
  createSignal,
  onCleanup,
  onMount,
  type JSX,
} from "solid-js";
import { Portal } from "solid-js/web";
import { cx } from "../cx";
import {
  computePlacement,
  type Placement,
  type Rect,
} from "./popoverPlacement";

export type PanelVariant = "default" | "console";

const PANEL_SURFACE: Record<PanelVariant, string> = {
  default: "heph-rise bg-surface shadow-2",
  console: "heph-rise border border-line-strong bg-surface shadow-2",
};

export interface PopoverApi {
  /** Reactive open-state accessor — call it (`open()`) in the trigger. */
  open: () => boolean;
  setOpen: (open: boolean) => void;
  close: () => void;
}

export interface PopoverProps {
  /** The clickable trigger; receives the open state + setters. */
  trigger: (api: PopoverApi) => JSX.Element;
  /** The floating panel contents; receives `close` to dismiss on select. */
  panel: (api: { close: () => void }) => JSX.Element;
  /** Horizontal alignment of the panel against the trigger. Default left. */
  align?: "left" | "right";
  /** Full-width field layout: the root fills its container and the panel spans the
   *  trigger's width (ignores `align`). Default inline/content. */
  block?: boolean;
  /** Extra classes for the panel (width, max-height, layout). */
  panelClass?: string;
  /** Drop the panel's own surface — no fill, no radius, no elevation — leaving only
   *  the positioning. For a panel that brings its own container (the framed, frosted
   *  region a `ConstructionReveal` draws): a card *around* that frame is the
   *  box-in-a-box the frame exists to avoid, and its shadow would sit on the glass. */
  bare?: boolean;
  /** The panel's own surface. `console` is the square, ruled menu of the console
   *  register — a `line-strong` frame and no radius — for a menu opened from a ruled
   *  strip (the composer's pickers), so it reads as the strip's own. It keeps the
   *  shadow: it floats. */
  variant?: PanelVariant;
  /** Fired on the closed→open edge only, so a caller can refresh what the panel is
   *  about to show. Not fired on close, and never twice for one opening. */
  onOpen?: () => void;
  class?: string;
}

/** Whether two placements would paint identically — the panel is positioned entirely
 *  from these four numbers. */
function samePlacement(a: Placement | null, b: Placement): boolean {
  return (
    a !== null &&
    a.top === b.top &&
    a.left === b.left &&
    a.clampHeight === b.clampHeight &&
    a.minWidth === b.minWidth &&
    a.maxWidth === b.maxWidth
  );
}

export interface FloatingPanelProps {
  /** Whether the panel is showing. Owned by the caller rather than here, because the
   *  two callers keep it in different places: `Popover` has its own signal, a context
   *  menu derives it from whether it has an anchor at all. */
  open: () => boolean;
  /** Dismissal. Backdrop click and Escape both route through this. */
  onClose: () => void;
  /** The rect to place against, in viewport coordinates, **re-read on every pass** —
   *  that is what keeps a panel anchored to an element pinned to it through a scroll.
   *  Null means there is nothing to measure against yet and the pass is skipped.
   *
   *  Taking a rect rather than reading a trigger element is the one thing that makes
   *  this reusable: a *point* is a legal anchor. A zero-size rect at the cursor flips
   *  and clamps through exactly the same geometry as a button-sized one, so a context
   *  menu needs no arithmetic of its own. */
  anchor: () => Rect | null;
  /** The panel's contents, built lazily — a closed panel must not construct (or run
   *  the effects of) anything it isn't showing. */
  panel: () => JSX.Element;
  align?: "left" | "right";
  block?: boolean;
  panelClass?: string;
  bare?: boolean;
  variant?: PanelVariant;
  /** Right-click on the backdrop. Left unset it does nothing, which is what the
   *  dropdowns want; a context menu passes a handler that closes, so the operator's
   *  next right-click reaches the row underneath instead of the backdrop. */
  onBackdropContextMenu?: (e: MouseEvent) => void;
  /** Place the panel and nothing else: **no backdrop, no Escape listener**. The caller
   *  owns dismissal entirely.
   *
   *  For a panel anchored to something the operator is still *typing into* — the
   *  composer's `/` and `@` menus. Both defaults are actively wrong there and in ways
   *  that read as flakiness rather than as a bug:
   *
   *  - The backdrop is a full-screen `fixed inset-0`, so while the menu is open every
   *    click on the field underneath hits it instead. The caret cannot be repositioned,
   *    and mousedown on a non-focusable div blurs the field mid-sentence.
   *  - The Escape listener is a bare `document` keydown with no editable-target check
   *    (unlike `~/lib/keymap`), so one Escape would close the menu *and* whatever else
   *    is listening behind it.
   *
   *  Dismissal is not lost, it moves: a menu that lives off the field's own content
   *  closes when the token does, and the field handles its own Escape. */
  passive?: boolean;
  /** Hold the panel to the anchor's width rather than letting its content set it —
   *  see `Placement.maxWidth`. For a panel that reads as an extension of a full-width
   *  field, not as a dropdown hanging off a narrow control. */
  fit?: boolean;
  /** Which side of the anchor to open on when both would do. Default `"below"`. */
  prefer?: "above" | "below";
}

/** The floating half of every overlay in the system: portal, click-out backdrop,
 *  Escape, and a panel kept placed against a moving anchor. `Popover` wraps it with a
 *  trigger and open state; `ContextMenu` drives it from a cursor point.
 *
 *  **The panel is portalled to `document.body` and positioned `fixed`.** It used to be
 *  an `absolute` child of the trigger, which had two failure modes that looked like
 *  one: near the bottom of the window it ran off-screen, and inside any scroll
 *  container — the chat transcript, a modal body, the viewport panel — it was *clipped*
 *  by that ancestor's `overflow`, which no amount of `align` tuning could fix. A portal
 *  escapes every ancestor's overflow and stacking context; fixed coordinates measured
 *  from the anchor keep it in place.
 *
 *  Placement flips above the anchor when there is more room there, and shifts
 *  horizontally to stay inside the viewport. It is recomputed on scroll (capture phase,
 *  so scroll containers fire it too, not just the window), on resize, and **whenever the
 *  panel's own content changes size** — a disclosure opening inside it, a list filtering
 *  down. That last one is not a refinement: a placement measured once is wrong in the
 *  one direction that hurts, since a panel placed below an anchor with just enough room
 *  keeps growing *downward* off the bottom of the window, and the clamp that would have
 *  made it scroll was decided when it was still small. */
export function FloatingPanel(props: FloatingPanelProps): JSX.Element {
  let panelRef: HTMLDivElement | undefined;
  const [placement, setPlacement] = createSignal<Placement | null>(null);

  const measure = (): void => {
    const anchor = props.anchor();
    if (!anchor) return;
    // Before the panel has rendered there is no height to flip on, so the first pass
    // places it below and a second (from the panel's own onMount) corrects it.
    //
    // The measurement has to be of the panel's **natural** height, which is why the
    // inline `max-height` comes off first. Measuring through it feeds the previous
    // pass's clamp back in: a 400px panel clamped to 200 measures 200 next time, `200 >
    // 200` is false, the clamp is dropped, the panel springs open off-screen, and the
    // pass after that clamps it again — a visible flicker on every scroll, with the
    // flip decision oscillating alongside it because it reads the same height.
    let panel: { width: number; height: number } | null = null;
    if (panelRef) {
      const restore = panelRef.style.maxHeight;
      panelRef.style.maxHeight = "";
      const rect = panelRef.getBoundingClientRect();
      panel = { width: rect.width, height: rect.height };
      panelRef.style.maxHeight = restore;
    }
    const next = computePlacement({
      anchor,
      panel,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      align: props.align,
      block: props.block,
      fit: props.fit,
      prefer: props.prefer,
    });
    // An unchanged placement is not re-published. The panel's own size is watched
    // below, and applying a clamp *changes* that size — so a pass that concludes
    // nothing moved must end there rather than write an identical object and wake the
    // observer that called it.
    setPlacement((prev) => (samePlacement(prev, next) ? prev : next));
  };

  createEffect(() => {
    if (!props.open()) {
      setPlacement(null);
      return;
    }
    measure();
    // Capture phase: a scroll inside the transcript doesn't bubble to window, and that
    // is exactly the case the portal has to keep up with.
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    onCleanup(() => {
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    });
  });

  // Escape closes while open (the backdrop handles outside clicks). Both are skipped
  // for a `passive` panel, whose caller owns dismissal — see the prop.
  createEffect(() => {
    if (!props.open() || props.passive) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    document.addEventListener("keydown", onKey);
    onCleanup(() => document.removeEventListener("keydown", onKey));
  });

  return (
    <Show when={props.open()}>
      <Portal>
        <Show when={!props.passive}>
          <div
            class="fixed inset-0 z-40"
            onClick={() => props.onClose()}
            onContextMenu={(e) => props.onBackdropContextMenu?.(e)}
          />
        </Show>
        <PopoverPanel
          ref={(el) => {
            panelRef = el;
          }}
          placement={placement()}
          panelClass={props.panelClass}
          bare={props.bare}
          variant={props.variant}
          onMeasure={measure}
        >
          {props.panel()}
        </PopoverPanel>
      </Portal>
    </Show>
  );
}

/** The dropdown shell shared by Menu, Select and Combobox: an anchored trigger plus a
 *  `FloatingPanel`. Owns nothing but the open state and the trigger box — everything
 *  about the floating half (portal, backdrop, Escape, placement) lives above, so a
 *  caller anchoring to something other than a trigger gets identical behavior. */
export function Popover(props: PopoverProps): JSX.Element {
  const [open, setOpen] = createSignal(false);
  const close = () => setOpen(false);

  // The open edge, fired from the state change rather than the trigger's click
  // handler — the trigger is only one of the ways this opens, and a caller asking
  // "refresh when the panel appears" means whenever it appears.
  let wasOpen = false;
  createEffect(() => {
    const isOpen = open();
    if (isOpen && !wasOpen) props.onOpen?.();
    wasOpen = isOpen;
  });

  let triggerRef: HTMLDivElement | undefined;

  return (
    <div
      ref={triggerRef}
      class={cx(
        props.block ? "relative flex w-full" : "relative inline-flex",
        props.class,
      )}
    >
      {props.trigger({ open, setOpen, close })}
      <FloatingPanel
        open={open}
        onClose={close}
        anchor={() => triggerRef?.getBoundingClientRect() ?? null}
        align={props.align}
        block={props.block}
        panelClass={props.panelClass}
        bare={props.bare}
        variant={props.variant}
        panel={() => props.panel({ close })}
      />
    </div>
  );
}

/** Split out so `onMount` fires once the panel element exists — the first measure
 *  runs without a height (nothing is rendered yet) and this is what corrects it,
 *  before paint, so the panel never visibly jumps.
 *
 *  It is also where the panel's own size is watched, for the same reason: the element
 *  is only here. */
function PopoverPanel(props: {
  ref: (el: HTMLDivElement) => void;
  placement: Placement | null;
  panelClass?: string;
  bare?: boolean;
  variant?: PanelVariant;
  onMeasure: () => void;
  children: JSX.Element;
}): JSX.Element {
  let el: HTMLDivElement | undefined;
  onMount(() => {
    props.onMeasure();
    // Re-place on every size change the panel makes for itself. Observing the panel
    // rather than listening for a caller's "I grew" keeps the rule in one place: any
    // panel whose content can change while open is covered, and none of them has to
    // know it.
    const ro = new ResizeObserver(() => props.onMeasure());
    if (el) ro.observe(el);
    onCleanup(() => ro.disconnect());
  });
  return (
    <div
      ref={(node) => {
        el = node;
        props.ref(node);
      }}
      class={cx(
        "fixed z-50",
        // A bare panel also drops the rise: it brings its own arrival, and two
        // entrance animations on nested elements read as a bounce.
        !props.bare && PANEL_SURFACE[props.variant ?? "default"],
        // Scrolling is added only when we actually clamp; Select already scrolls
        // itself and Combobox scrolls an inner element, and nesting a second
        // overflow-auto around either gives the panel two scrollbars.
        props.placement?.clampHeight != null &&
          "overflow-y-auto scrollbar-thin",
        props.panelClass,
      )}
      style={{
        top: `${props.placement?.top ?? 0}px`,
        left: `${props.placement?.left ?? 0}px`,
        "max-height":
          props.placement?.clampHeight != null
            ? `${props.placement.clampHeight}px`
            : undefined,
        // A floor, not a fixed width: a `block` panel spans its field but grows to
        // fit its own options rather than truncating them (see `Placement.minWidth`).
        // Safe to measure through — unlike the max-height above, a min-width can only
        // widen the natural size, so re-measuring never feeds a shrinking value back.
        "min-width": props.placement?.minWidth
          ? `${props.placement.minWidth}px`
          : undefined,
        // Also safe to measure through, and for a sturdier reason than the min-width
        // above: it is derived from the anchor and the viewport, never from the panel,
        // so a pass cannot feed its own result back the way the max-height could. What
        // it does change is the measured *height* — a narrower panel wraps taller — and
        // that is the height the flip and the clamp should be deciding against, because
        // it is the height the panel will actually have.
        "max-width": props.placement
          ? `${props.placement.maxWidth}px`
          : undefined,
        // Until the first measure lands the panel would flash at 0,0 in the corner.
        visibility: props.placement ? "visible" : "hidden",
      }}
    >
      {props.children}
    </div>
  );
}
