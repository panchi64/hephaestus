/**
 * The thread's browser, live.
 *
 * The agent's browser is headless and runs in the services VM, so this pane is the only
 * way to see it and the only way to take the wheel: it paints the frames the backend
 * streams and sends back what the operator does to them. Logging in, dismissing a
 * banner or landing on a page before handing the thread on all happen here.
 *
 * **The view owns the keyboard while it has focus.** Keys, Tab and the paste chord go to
 * the page, and the app's own shortcuts and context menu stay out of the way; Escape is
 * the one key that is not the page's, and it hands focus back. The listeners are native
 * and on the element itself, so they run before the app's document-level keymap sees the
 * event at all.
 */

import {
  createEffect,
  createSignal,
  on,
  onCleanup,
  onMount,
  Show,
  type JSX,
} from "solid-js";
import { isApiError } from "~/lib/api";
import {
  Button,
  confirm,
  EmptyState,
  Input,
  LoadingText,
  Text,
  Tooltip,
  toast,
} from "~/ui";
import {
  closeBrowser,
  createBrowserInput,
  openBrowser,
  streamBrowserView,
  type BrowserFrame,
  type BrowserInput,
} from "../data";
import type { BrowserState } from "../browserState";
import {
  heldButton,
  isPasteChord,
  keyText,
  mapPointer,
  modifierMask,
  mouseButton,
  normalizeAddress,
  wheelPixels,
  type Box,
} from "../viewport/browserView";
import { PaneToolbar } from "./PaneFrame";

type Status = "connecting" | "live" | "none" | "ended" | "lost";

/** Reconnects after a dropped transport before the pane gives up and offers a retry. */
const MAX_RECONNECTS = 3;

interface Shown {
  src: string;
  width: number;
  height: number;
  url: string;
  title: string;
}

const toShown = (frame: BrowserFrame): Shown => ({
  src: `data:image/jpeg;base64,${frame.data}`,
  width: frame.width,
  height: frame.height,
  url: frame.url,
  title: frame.title,
});

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const id = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(id);
      resolve();
    });
  });
}

export function BrowserSurface(props: {
  conversationId: () => string | null;
  browser: BrowserState;
  speaker: string;
}): JSX.Element {
  const [status, setStatus] = createSignal<Status>("connecting");
  const [shown, setShown] = createSignal<Shown | null>(null);
  const [draft, setDraft] = createSignal("");
  const [editing, setEditing] = createSignal(false);
  const [attempt, setAttempt] = createSignal(0);
  const [opening, setOpening] = createSignal(false);

  let viewEl: HTMLDivElement | undefined;
  let imgEl: HTMLImageElement | undefined;
  let queue: ReturnType<typeof createBrowserInput> | undefined;

  const live = () => status() === "live";

  // The stream and the input channel share a lifetime: one per thread, restarted when
  // the operator opens a session from the empty state.
  createEffect(
    on([props.conversationId, attempt], ([id]) => {
      const controller = new AbortController();
      onCleanup(() => {
        controller.abort();
        queue?.clear();
        queue = undefined;
      });
      clearFrame();
      if (id === null) {
        setStatus("none");
        return;
      }
      setStatus("connecting");
      queue = createBrowserInput(id, (err) => {
        if (isApiError(err) && err.status === 404 && !controller.signal.aborted)
          sessionGone();
      });
      void watch(id, controller.signal);
    }),
  );

  // Frames arrive faster than the screen paints; only the newest is worth drawing.
  let latest: BrowserFrame | null = null;
  let scheduled = 0;
  onCleanup(() => cancelAnimationFrame(scheduled));
  function paint(frame: BrowserFrame): void {
    latest = frame;
    if (scheduled) return;
    scheduled = requestAnimationFrame(() => {
      scheduled = 0;
      if (latest) setShown(toShown(latest));
      latest = null;
    });
  }

  function clearFrame(): void {
    cancelAnimationFrame(scheduled);
    scheduled = 0;
    latest = null;
    setShown(null);
  }

  function sessionGone(): void {
    clearFrame();
    setStatus("none");
    props.browser.gone();
  }

  async function watch(id: string, signal: AbortSignal): Promise<void> {
    let reconnects = 0;
    while (!signal.aborted) {
      const end = await streamBrowserView(id, {
        signal,
        onEvent: (event) => {
          if (event.type !== "frame") return;
          reconnects = 0;
          setStatus("live");
          paint(event);
        },
      });
      if (signal.aborted) return;
      if (end === "closed") {
        clearFrame();
        setStatus("ended");
        props.browser.gone();
        return;
      }
      if (end === "none") {
        sessionGone();
        return;
      }
      if (reconnects >= MAX_RECONNECTS) {
        setStatus("lost");
        return;
      }
      setStatus("connecting");
      await pause(500 * 2 ** reconnects, signal);
      reconnects += 1;
    }
  }

  // The address shows where the page is, except while the operator is typing in it.
  createEffect(() => {
    const url = shown()?.url ?? "";
    if (!editing()) setDraft(url);
  });

  const send = (event: BrowserInput): void => queue?.push(event);

  const frameBox = (): { box: Box; size: Shown } | null => {
    const size = shown();
    if (!imgEl || !size) return null;
    const rect = imgEl.getBoundingClientRect();
    return {
      box: {
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
      },
      size,
    };
  };

  const pointAt = (clientX: number, clientY: number) => {
    const placed = frameBox();
    return placed
      ? mapPointer(placed.box, placed.size, clientX, clientY)
      : null;
  };

  let pressed = false;

  function sendMouse(
    action: "down" | "up" | "move",
    event: MouseEvent,
    button: ReturnType<typeof mouseButton>,
    requireInside: boolean,
  ): void {
    const point = pointAt(event.clientX, event.clientY);
    if (!point || (requireInside && !point.inside)) return;
    send({
      type: "mouse",
      action,
      x: Math.round(point.x),
      y: Math.round(point.y),
      button,
      click_count: action === "move" ? 0 : Math.max(event.detail, 1),
      modifiers: modifierMask(event),
    });
  }

  function onMouseDown(event: MouseEvent): void {
    if (!live()) return;
    // Focus is taken by hand: the default would also start a text selection or an
    // image drag on the frame.
    event.preventDefault();
    viewEl?.focus();
    const point = pointAt(event.clientX, event.clientY);
    if (!point?.inside) return;
    pressed = true;
    sendMouse("down", event, mouseButton(event.button), true);
    // The release may land outside the pane; the page still needs to see it.
    const move = (e: MouseEvent) =>
      sendMouse("move", e, heldButton(e.buttons), false);
    const up = (e: MouseEvent) => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      pressed = false;
      sendMouse("up", e, mouseButton(e.button), false);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  function onMouseMove(event: MouseEvent): void {
    if (!live() || pressed) return;
    sendMouse("move", event, "none", true);
  }

  function onKey(action: "down" | "up", event: KeyboardEvent): void {
    if (event.isComposing) return;
    if (action === "down" && event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      viewEl?.blur();
      return;
    }
    // The paste chord goes to the browser, which raises `paste` with the text.
    if (isPasteChord(event)) {
      event.stopPropagation();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (!live()) return;
    send({
      type: "key",
      action,
      key: event.key,
      code: event.code,
      text: action === "down" ? keyText(event) : "",
      key_code: event.keyCode,
      modifiers: modifierMask(event),
    });
  }

  onMount(() => {
    const el = viewEl;
    if (!el) return;
    const onKeyDown = (e: KeyboardEvent) => onKey("down", e);
    const onKeyUp = (e: KeyboardEvent) => onKey("up", e);
    const onPaste = (e: ClipboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const text = e.clipboardData?.getData("text/plain") ?? "";
      if (live() && text) send({ type: "text", text });
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (!live()) return;
      const point = pointAt(e.clientX, e.clientY);
      if (!point?.inside) return;
      const page = shown()?.height ?? 0;
      send({
        type: "wheel",
        x: Math.round(point.x),
        y: Math.round(point.y),
        delta_x: wheelPixels(e.deltaX, e.deltaMode, page),
        delta_y: wheelPixels(e.deltaY, e.deltaMode, page),
        modifiers: modifierMask(e),
      });
    };
    // Suppressed at the element so the pane's own right-click (which closes it) and the
    // browser's menu both stay out of a gesture that belongs to the page.
    const onContextMenu = (e: Event) => {
      e.preventDefault();
      e.stopPropagation();
    };
    el.addEventListener("keydown", onKeyDown);
    el.addEventListener("keyup", onKeyUp);
    el.addEventListener("paste", onPaste);
    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("contextmenu", onContextMenu);
    onCleanup(() => {
      el.removeEventListener("keydown", onKeyDown);
      el.removeEventListener("keyup", onKeyUp);
      el.removeEventListener("paste", onPaste);
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("contextmenu", onContextMenu);
    });
  });

  function navigate(): void {
    const url = normalizeAddress(draft());
    if (url === null) {
      toast.error("Enter a web address starting with http:// or https://.");
      return;
    }
    send({ type: "navigate", url });
    viewEl?.focus();
  }

  async function open(): Promise<void> {
    const id = props.conversationId();
    if (id === null) return;
    setOpening(true);
    try {
      await openBrowser(id);
      props.browser.opened();
      setAttempt((n) => n + 1);
    } catch (err) {
      toast.error(isApiError(err) ? err.detail : "Unable to open the browser.");
    } finally {
      setOpening(false);
    }
  }

  async function end(): Promise<void> {
    const id = props.conversationId();
    if (id === null) return;
    const ok = await confirm({
      title: "End the browser session?",
      detail: "The agent is told you closed it.",
      confirmLabel: "End session",
      tone: "alert",
    });
    if (!ok) return;
    try {
      await closeBrowser(id);
      props.browser.ended();
    } catch (err) {
      toast.error(
        isApiError(err) ? err.detail : "Unable to end the browser session.",
      );
    }
  }

  const nav = (action: "back" | "forward" | "reload"): void =>
    send({ type: "history", action });

  return (
    <div class="flex h-full min-h-0 w-full flex-col gap-2 px-3 pb-2">
      <PaneToolbar>
        <Tooltip label="Back" side="bottom">
          <Button
            variant="ghost"
            size="sm"
            leading="chevron-left"
            aria-label="Back"
            disabled={!live()}
            onClick={() => nav("back")}
          />
        </Tooltip>
        <Tooltip label="Forward" side="bottom">
          <Button
            variant="ghost"
            size="sm"
            leading="chevron-right"
            aria-label="Forward"
            disabled={!live()}
            onClick={() => nav("forward")}
          />
        </Tooltip>
        <Tooltip label="Reload" side="bottom">
          <Button
            variant="ghost"
            size="sm"
            leading="refresh"
            aria-label="Reload"
            disabled={!live()}
            onClick={() => nav("reload")}
          />
        </Tooltip>
        <Tooltip label="End session" side="bottom">
          <Button
            variant="ghost"
            size="sm"
            leading="stop"
            aria-label="End session"
            disabled={status() === "none" || status() === "ended"}
            onClick={() => void end()}
          />
        </Tooltip>
      </PaneToolbar>

      <form
        class="flex shrink-0 flex-col gap-1"
        onSubmit={(e) => {
          e.preventDefault();
          navigate();
        }}
      >
        <Input
          type="text"
          inputmode="url"
          autocomplete="off"
          spellcheck={false}
          aria-label="Address"
          placeholder="Search or enter a web address"
          value={draft()}
          disabled={!live()}
          onInput={(e) => setDraft(e.currentTarget.value)}
          onFocus={(e) => {
            setEditing(true);
            e.currentTarget.select();
          }}
          onBlur={() => {
            setEditing(false);
            setDraft(shown()?.url ?? "");
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") e.currentTarget.blur();
          }}
        />
        <Show when={shown()?.title}>
          <Text variant="label" tone="dim" class="truncate">
            {shown()?.title}
          </Text>
        </Show>
      </form>

      <div class="relative min-h-0 flex-1 border border-line bg-raised">
        <div
          ref={viewEl}
          tabIndex={0}
          role="application"
          aria-label="Browser view"
          class="absolute inset-0 outline-none focus-visible:outline-1 focus-visible:outline-offset-[-1px]"
          classList={{ "cursor-default": live(), invisible: !shown() }}
          onMouseDown={onMouseDown}
          onMouseMove={onMouseMove}
        >
          <Show when={shown()}>
            {(frame) => (
              <img
                ref={imgEl}
                src={frame().src}
                alt={frame().title || "Browser page"}
                draggable={false}
                class="h-full w-full select-none object-contain"
              />
            )}
          </Show>
        </div>
        <Show when={!shown()}>
          <div class="absolute inset-0 flex items-center justify-center">
            <Show when={status() === "connecting"}>
              <LoadingText label="Connecting to the browser" />
            </Show>
            <Show when={status() === "none"}>
              <EmptyState
                icon="link"
                message="No browser session"
                hint={`Open one to watch and drive ${props.speaker}'s browser here.`}
                action={
                  <Button
                    variant="default"
                    size="sm"
                    pending={opening()}
                    onClick={() => void open()}
                  >
                    Open browser
                  </Button>
                }
              />
            </Show>
            <Show when={status() === "ended"}>
              <EmptyState
                icon="link"
                message="Browser session ended"
                hint="The agent's browser was closed."
                action={
                  <Button
                    variant="default"
                    size="sm"
                    pending={opening()}
                    onClick={() => void open()}
                  >
                    Open browser
                  </Button>
                }
              />
            </Show>
            <Show when={status() === "lost"}>
              <EmptyState
                icon="warning"
                message="Connection lost"
                hint="The live view stopped arriving."
                action={
                  <Button
                    variant="default"
                    size="sm"
                    onClick={() => setAttempt((n) => n + 1)}
                  >
                    Reconnect
                  </Button>
                }
              />
            </Show>
          </div>
        </Show>
        <Show when={shown() && !live()}>
          <div class="absolute inset-x-0 bottom-0 bg-raised/90 px-3 py-1">
            <Show when={status() === "connecting"}>
              <LoadingText label="Reconnecting" />
            </Show>
            <Show when={status() === "lost"}>
              <Button
                variant="default"
                size="sm"
                onClick={() => setAttempt((n) => n + 1)}
              >
                Reconnect
              </Button>
            </Show>
          </div>
        </Show>
      </div>
    </div>
  );
}
