import {
  children,
  createContext,
  createSignal,
  Show,
  Suspense,
  useContext,
  type Accessor,
  type JSX,
} from "solid-js";
import { Portal } from "solid-js/web";
import { Button, Icon, LoadingText, Text, Tooltip } from "~/ui";
import { surfaceSpec, type SurfaceId } from "../viewport/surfaces";

/**
 * What every pane wears, drawn by the host rather than by the surface inside it.
 *
 * A pane could be dismissed three ways — the header button that opened it, a right-click,
 * or `shift+w` — and **none of them was visible from the pane itself**. Only the View
 * carried a close control, because the View was the whole panel back when the panel held
 * exactly one thing. A region the operator cannot see how to shut is one they stop
 * opening, so the affordance belongs on every pane; and since the host is the one place
 * that knows a pane's identity (`surfaceSpec`) and how to close it (`closeSurface`), it is
 * the host that draws it. A surface does not get to forget.
 *
 * **The label is the host's, so a surface must not print its own.** Tasks, Agents and
 * Files each drew a title row of their own, which is why those rows are gone: two headings
 * an inch apart saying the same word is worse than either alone. What is left of those
 * rows — a progress figure, a count — arrives here as `meta`, opaque JSX the frame places
 * and never inspects. That is the whole of what keeps this component surface-agnostic.
 *
 * **`header={false}` is for a pane inside a stack**, where the tab strip is already the
 * label and the close rides at its end. The frame still wraps the body, so the box a
 * stacked surface is laid out in is the same box a leaf gets — and `toolbarMount` is
 * where that member's `PaneToolbar` lands in the strip instead of a header.
 */
export function PaneFrame(props: {
  id: SurfaceId;
  /** Right-aligned, before the close button — the surface's own figures. */
  meta?: JSX.Element;
  /** Draw the header. Default true; false inside a tabbed stack. */
  header?: boolean;
  /** Where `PaneToolbar` renders when the frame draws no header — the stack's
   *  strip hands each member a mount of its own. */
  toolbarMount?: Accessor<HTMLElement | undefined>;
  /** The pane has the host to itself and should grow into it — see `usePaneFill`. */
  fill?: boolean;
  onClose: () => void;
  children: JSX.Element;
}): JSX.Element {
  const spec = () => surfaceSpec(props.id);
  // Resolved once: a slot prop is a getter, so guarding on it and then rendering it
  // builds the same elements twice.
  const meta = children(() => props.meta);
  const [ownMount, setOwnMount] = createSignal<HTMLElement>();
  const mount = (): HTMLElement | undefined =>
    props.header === false ? props.toolbarMount?.() : ownMount();
  // No height of its own: the frame is a flex child of the box the host lays panes out
  // in, so it stretches to that. An `h-full` here would be a percentage of a strip's
  // content-sized parent — the one case where it means nothing.
  //
  // `@container/pane` sizes the header's own controls (the View's actions fold into a
  // menu below `@md/pane`); the body's unnamed `@container` is what a surface's
  // layout queries — a pane's width is the operator's drag, never the viewport's.
  return (
    <div class="@container/pane flex min-h-0 min-w-0 flex-1 flex-col">
      <Show when={props.header !== false}>
        <div class="flex shrink-0 items-center gap-2 px-3 py-2">
          <Icon name={spec().icon} class="shrink-0 text-dim" />
          {/* A plate: it names the region (design §4), in the voice every other
              region name in chat speaks. */}
          <Text variant="plate" tone="default" class="min-w-0 truncate">
            {spec().label}
          </Text>
          {/* Meta and toolbar give way before the close does: the close is the one
              control every pane must keep. */}
          <div class="ml-auto flex min-w-0 items-center gap-2">
            <Show when={meta()}>
              <div class="flex min-w-0 items-center">{meta()}</div>
            </Show>
            <div ref={setOwnMount} class="flex min-w-0 items-center gap-1" />
            <div class="shrink-0">
              <PaneCloseButton label={spec().label} onClose={props.onClose} />
            </div>
          </div>
        </div>
      </Show>
      {/* A COLUMN, holding one child, and the direction is the whole point: a
          surface body sizes itself with `h-full` and no width of its own, so in a
          flex *row* it shrank to its own max-content — the View rendered its
          preview in a column the width of its PREVIEW/CODE tabs and left the rest
          of the pane empty. A column stretches its child across instead, which is
          the box claiming the space rather than every surface remembering to. */}
      {/* **A pane's own fetches stop here, and that is the point.** Every one of
          these surfaces reads a resource straight into its markup — a file list,
          an artifact's bytes, a diff — and in Solid a read of a *pending* resource
          suspends the nearest boundary, wherever that happens to be. The nearest
          one was the shell's, around the whole route: opening a pane for the first
          time fetched, suspended, and blanked the entire room — header, transcript
          and composer — to the shell's "Loading" line for as long as the pane's
          own request took. Every surface already carries a local loading arm it
          never got to render.

          So the boundary belongs to the pane, at the same box the surface is laid
          out in, and it is drawn here rather than in each surface for the reason
          the header is: the host is what guarantees a pane wears this, so a
          surface cannot forget it and a new one gets it for nothing. */}
      <div class="@container flex min-h-0 min-w-0 flex-1 flex-col">
        <PaneToolbarContext.Provider value={mount}>
          <PaneFillContext.Provider value={() => props.fill === true}>
            <Suspense fallback={<LoadingText class="px-3 py-2" />}>
              {props.children}
            </Suspense>
          </PaneFillContext.Provider>
        </PaneToolbarContext.Provider>
      </div>
    </div>
  );
}

const PaneFillContext = createContext<Accessor<boolean>>(() => false);

/**
 * Whether this pane is a strip with the host to itself — nothing below it to make
 * room for. A strip caps its own height so it cannot push the panels off the bottom;
 * with no panels there is nothing to push, and the cap only leaves a clipped list over
 * an empty frame. Handed down by the host rather than through the renderer map, since
 * it is a fact about where the pane sits and not about what it shows.
 */
export function usePaneFill(): Accessor<boolean> {
  return useContext(PaneFillContext);
}

/** The element a surface's toolbar renders into — the pane header, or the stack
 *  strip's slot for this member. `undefined` until that element has mounted. */
const PaneToolbarContext = createContext<Accessor<HTMLElement | undefined>>();

/**
 * A surface's own controls, drawn in the pane's header rather than in a row of its own.
 *
 * The host still owns the header — its name, its figures, its close — and a surface
 * only lends it controls, so a pane stays one row of chrome instead of a header over a
 * toolbar. The children stay in the surface's own tree (context, ownership, local
 * state) and only their DOM moves. Outside a pane there is no header to reach, so they
 * render where they stand.
 */
export function PaneToolbar(props: { children: JSX.Element }): JSX.Element {
  const mount = useContext(PaneToolbarContext);
  if (!mount) return <>{props.children}</>;
  return (
    <Show when={mount()}>
      {(el) => (
        // Portal wraps its children in a `div`; `contents` takes that box out of
        // the header's flex row so the controls lay out as its own items.
        <Portal mount={el()} ref={(box) => (box.className = "contents")}>
          {props.children}
        </Portal>
      )}
    </Show>
  );
}

/** The close itself, shared with the tab strip a stacked pane wears instead of a
 *  header — one button in two places, so they cannot drift into two gestures. */
export function PaneCloseButton(props: {
  /** The surface's label, spoken to a screen reader: "Close Changes". */
  label: string;
  onClose: () => void;
}): JSX.Element {
  return (
    <Tooltip label="Close" side="bottom">
      <Button
        variant="ghost"
        size="sm"
        leading="close"
        aria-label={`Close ${props.label}`}
        onClick={() => props.onClose()}
      />
    </Tooltip>
  );
}
