import { useNavigate } from "@solidjs/router";
import {
  createResource,
  createSignal,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import { SandboxedFrame } from "~/features/chat/components/SandboxedFrame";
import { chatDraftKey } from "~/features/chat/data";
import { openWindowSession } from "~/features/plugins/windows";
import { Icon, LoadingText, Text, cx, saveDraft } from "~/ui";
import { connectBridge } from "./bridge/broker";
import {
  windowActions,
  type PluginWindowState,
  type Viewport,
} from "./windowStore";

function viewport(): Viewport {
  return { width: window.innerWidth, height: window.innerHeight };
}

/** Follows a pointer from `pointerdown` until it lets go, reporting each step's travel. */
function drag(
  e: PointerEvent,
  onStep: (dx: number, dy: number) => void,
  onEnd: () => void,
): void {
  e.preventDefault();
  const target = e.currentTarget as HTMLElement;
  target.setPointerCapture(e.pointerId);
  let lastX = e.clientX;
  let lastY = e.clientY;
  const move = (ev: PointerEvent) => {
    onStep(ev.clientX - lastX, ev.clientY - lastY);
    lastX = ev.clientX;
    lastY = ev.clientY;
  };
  // `pointercancel` settles it too: the browser sends that when the gesture is taken away.
  const up = () => {
    target.removeEventListener("pointermove", move);
    target.removeEventListener("pointerup", up);
    target.removeEventListener("pointercancel", up);
    onEnd();
  };
  target.addEventListener("pointermove", move);
  target.addEventListener("pointerup", up);
  target.addEventListener("pointercancel", up);
}

/** One plugin window: a title bar to drag, the plugin's page in a sandboxed frame,
 *  and a corner grip to resize it. */
export function PluginWindow(props: {
  win: PluginWindowState;
  /** Its place in the stack; higher is in front. */
  layer: number;
  top: boolean;
  onSettled: () => void;
}): JSX.Element {
  const navigate = useNavigate();
  const [reloadKey, setReloadKey] = createSignal(0);
  // A string source: moving or resizing the window must not open a new session, which
  // would reload its page.
  const [session] = createResource(
    () => `${props.win.key}#${reloadKey()}`,
    () => openWindowSession(props.win.pluginId, props.win.window),
  );
  // While dragging, the frame would swallow the pointer; a shield over it keeps it here.
  const [dragging, setDragging] = createSignal(false);
  let disconnect: (() => void) | undefined;
  onCleanup(() => disconnect?.());

  const close = (): void => windowActions.close(props.win.key);
  const focus = (): void => windowActions.focus(props.win.key);
  const settle = (): void => {
    setDragging(false);
    props.onSettled();
  };

  const onFrameLoad = (frame: HTMLIFrameElement): void => {
    const current = session();
    if (!current) return;
    disconnect?.();
    disconnect = connectBridge(frame, {
      plugin: props.win.plugin,
      window: props.win.window,
      title: props.win.title,
      bridgeToken: current.bridgeToken,
      permissions: current.permissions,
      close,
      openChat: (prompt) => {
        if (prompt) saveDraft(chatDraftKey(null), prompt);
        navigate("/chat");
      },
    });
  };

  return (
    <section
      role="dialog"
      aria-label={props.win.title}
      class={cx(
        "pointer-events-auto absolute flex flex-col border bg-surface shadow-2",
        props.top ? "border-line-strong" : "border-line",
      )}
      style={{
        left: `${props.win.x}px`,
        top: `${props.win.y}px`,
        width: `${props.win.width}px`,
        height: `${props.win.height}px`,
        "z-index": props.layer,
      }}
      onPointerDown={focus}
    >
      <header
        class="flex shrink-0 cursor-move select-none items-center justify-between gap-2 border-b border-line px-2 py-1"
        onPointerDown={(e) => {
          if ((e.target as HTMLElement).closest("button")) return;
          setDragging(true);
          drag(
            e,
            (dx, dy) => windowActions.move(props.win.key, dx, dy, viewport()),
            settle,
          );
        }}
      >
        <Text variant="micro" class="truncate">
          {props.win.title.toUpperCase()}
        </Text>
        <div class="flex items-center gap-1">
          <button
            type="button"
            aria-label={`Reload ${props.win.title}`}
            class="p-1 text-dim hover:text-text"
            onClick={() => setReloadKey((n) => n + 1)}
          >
            <Icon name="refresh" size={12} />
          </button>
          <button
            type="button"
            aria-label={`Close ${props.win.title}`}
            class="p-1 text-dim hover:text-text"
            onClick={close}
          >
            <Icon name="close" size={12} />
          </button>
        </div>
      </header>
      <div class="relative min-h-0 flex-1">
        <Show
          when={session()}
          fallback={
            <Show
              when={session.error}
              fallback={<LoadingText label="Opening" />}
            >
              <Text variant="micro" tone="dim" class="p-3">
                This window could not be opened. The plugin may have been
                switched off or changed.
              </Text>
            </Show>
          }
        >
          {(current) => (
            <SandboxedFrame
              src={current().src}
              title={props.win.title}
              policy="plugin"
              onLoad={onFrameLoad}
            />
          )}
        </Show>
        <Show when={dragging()}>
          <div class="absolute inset-0" />
        </Show>
      </div>
      <Show when={props.win.resizable}>
        <div
          aria-hidden="true"
          class="absolute bottom-0 right-0 h-3 w-3 cursor-nwse-resize"
          onPointerDown={(e) => {
            setDragging(true);
            drag(
              e,
              (dw, dh) =>
                windowActions.resize(props.win.key, dw, dh, viewport()),
              settle,
            );
          }}
        />
      </Show>
    </section>
  );
}
