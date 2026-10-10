import { For, Show, type JSX } from "solid-js";
import { Button, Menu, Tooltip, type IconName, type MenuItem } from "~/ui";
import { activeDownload, triggerDownload } from "../viewport/downloadRegistry";

const FONT_MIN = -2;
const FONT_MAX = 2;

/** One control, described once and drawn two ways — inline, or as a menu row. */
interface ViewAction {
  /** Spoken name, and the menu row's label. Reactive where it names a state. */
  label: () => string;
  /** Inline glyph; an action without one shows `text` instead. */
  icon?: IconName;
  /** Inline text for a control that reads better as a word (A-, Wrap). */
  text?: string;
  /** Hover tip inline. Omitted where the visible word already says it. */
  tip?: () => string;
  /** Pressed state of a toggle — carried by brightness, never color. */
  pressed?: () => boolean;
  disabled?: () => boolean;
  /** Absent means always shown. */
  visible?: () => boolean;
  onSelect: () => void;
}

/** The viewer's actions — DOWNLOAD, the KEEPER pin, font size, soft-wrap, REFRESH and
 *  the fullscreen toggle — rendered into the pane header. A wide pane shows them as a
 *  row of ghost controls; below `@md/pane` they fold into one `···` menu, both built
 *  from the same list so the two forms cannot drift apart. Presentation-only: every
 *  control relays operator intent through props or the shared viewer-persistence seam;
 *  nothing here decides anything. */
export function ViewActionRow(props: {
  /** Rendered only when provided — P5 wires the backend keeper flip. */
  keeper?: boolean;
  onKeeper?: () => void;
  fontStep: number;
  onFontStep: (step: number) => void;
  softWrap: boolean;
  onToggleWrap: () => void;
  /** Reuses the panel's existing reload nonce; absent hides the control (PREVIEW
   *  only — CODE is an immutable snapshot tree, same as before). */
  onRefresh?: () => void;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
}): JSX.Element {
  // Built once with reactive fields, so a font step re-renders a button's state
  // rather than replacing the button — which would drop keyboard focus mid-press.
  const actions: ViewAction[] = [
    {
      label: () => "Download",
      icon: "download",
      tip: () => "Download",
      disabled: () => !activeDownload(),
      onSelect: triggerDownload,
    },
    {
      label: () => (props.keeper ? "Unmark keeper" : "Mark as keeper"),
      icon: "pin",
      tip: () => (props.keeper ? "Unmark keeper" : "Mark as keeper"),
      pressed: () => Boolean(props.keeper),
      visible: () => Boolean(props.onKeeper),
      onSelect: () => props.onKeeper?.(),
    },
    {
      label: () => "Smaller text",
      text: "A-",
      tip: () => "Smaller text",
      disabled: () => props.fontStep <= FONT_MIN,
      onSelect: () => props.onFontStep(Math.max(FONT_MIN, props.fontStep - 1)),
    },
    {
      label: () => "Larger text",
      text: "A+",
      tip: () => "Larger text",
      disabled: () => props.fontStep >= FONT_MAX,
      onSelect: () => props.onFontStep(Math.min(FONT_MAX, props.fontStep + 1)),
    },
    {
      label: () => (props.softWrap ? "Turn off soft wrap" : "Soft wrap"),
      text: "Wrap",
      pressed: () => props.softWrap,
      onSelect: () => props.onToggleWrap(),
    },
    {
      label: () => "Reload view",
      icon: "refresh",
      tip: () => "Reload",
      visible: () => Boolean(props.onRefresh),
      onSelect: () => props.onRefresh?.(),
    },
    // No collapse here: the pane's own frame carries the close, for every surface
    // rather than for the one that grew a control of its own back when it *was* the
    // panel. Two closes an inch apart is one too many.
    {
      label: () => (props.fullscreen ? "Exit full screen" : "Full screen"),
      text: "Expand",
      pressed: () => props.fullscreen,
      onSelect: () => props.onToggleFullscreen(),
    },
  ];

  const shown = (a: ViewAction): boolean => a.visible?.() ?? true;

  const menuItems = (): MenuItem[] =>
    actions.filter(shown).map((a) => ({
      label: a.label(),
      icon: a.icon,
      disabled: a.disabled?.(),
      onSelect: a.onSelect,
    }));

  const inline = (a: ViewAction): JSX.Element => {
    const button = (
      <Button
        variant="ghost"
        size="sm"
        leading={a.icon}
        active={a.pressed?.()}
        aria-label={a.label()}
        aria-pressed={a.pressed ? a.pressed() : undefined}
        disabled={a.disabled?.()}
        onClick={() => a.onSelect()}
      >
        {a.text}
      </Button>
    );
    return a.tip ? (
      <Tooltip label={a.tip()} side="bottom">
        {button}
      </Tooltip>
    ) : (
      button
    );
  };

  return (
    <>
      <div class="hidden shrink-0 items-center gap-1 @md/pane:flex">
        <For each={actions}>
          {(a) => <Show when={shown(a)}>{inline(a)}</Show>}
        </For>
      </div>
      <div class="@md/pane:hidden">
        <Menu
          items={menuItems()}
          trigger={
            <span class="inline-flex h-6 items-center px-2 text-label text-dim transition-colors hover:text-bright">
              <span aria-hidden="true">···</span>
              <span class="sr-only">View actions</span>
            </span>
          }
        />
      </div>
    </>
  );
}
