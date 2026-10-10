import {
  createEffect,
  createSignal,
  onCleanup,
  Show,
  type JSX,
} from "solid-js";
import { api } from "~/lib/api";
import { apiUrl } from "~/lib/config";
import { EmptyState, Text } from "~/ui";
import type { ViewLiveRef } from "../model";
import { SandboxedFrame } from "./SandboxedFrame";

/** How often to ask the backend whether this head is still running, while the tab
 *  is visible. Well under the sandbox's own idle window, so a warm foregrounded
 *  tab notices a reap in a bounded time rather than only on its next reload. */
const STATUS_POLL_MS = 5_000;

/** `HH:MM:SS` (24h, local) for the "last checked" strip. */
function nowLabel(): string {
  return new Date().toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** The `token` segment of a live head's proxied URL (`/previews/{token}/…`) — the
 *  one thing `/views/live/status` needs to ask whether it's still running. */
function tokenFromUrl(url: string): string | null {
  const match = /^\/previews\/([^/]+)\//.exec(url);
  return match ? match[1] : null;
}

/**
 * Mounts the View's live head — a server the agent started in its sandbox. The
 * backend reverse-proxies it under a token-gated path on the API origin; the token
 * *is* the credential, so the iframe needs no auth header. Sandboxed without
 * `allow-same-origin` so the framed (model-generated) app runs in an opaque origin
 * and can't act as the operator against the API. `live.url` already carries the
 * entry path, so the page renders rather than a directory listing.
 *
 * The sandbox's idle reaper can kill this server out from under the iframe with no
 * event to catch (no run is active at reap time) — the proxy just starts 404ing.
 * Rather than render a dead frame forever, this polls the head's own status (keyed
 * off the token already in `live.url`, no extra plumbing) and swaps to a plain
 * stopped notice the moment the backend confirms it's gone. Polling only runs
 * while the tab is visible (paused when backgrounded) and re-checks immediately on
 * refocus, so a warm tab left open in another window doesn't burn a poll every 5s
 * for nothing and still catches up the moment the operator looks back. A network
 * hiccup (as opposed to a confirmed "stopped" verdict) doesn't tear the iframe
 * down — it surfaces a small non-blocking strip above the frame instead.
 */
export function ViewLiveContent(props: {
  live: ViewLiveRef;
  /** Manual reload nonce — bumping it reloads the running server's page in place. */
  reloadKey: number;
  /** Operator zoom step (-2..+2), applied as whole-page scale on the frame. */
  fontStep?: number;
}): JSX.Element {
  const [stopped, setStopped] = createSignal(false);
  const [unreachable, setUnreachable] = createSignal<string | null>(null);

  createEffect(() => {
    const token = tokenFromUrl(props.live.url);
    setStopped(false);
    setUnreachable(null);
    if (!token) return; // an unrecognized URL shape — nothing to poll, just render it

    let live = true;
    const check = async (): Promise<void> => {
      if (!live) return;
      try {
        const res = await api.get<{ status: string }>(
          `/views/live/status?token=${encodeURIComponent(token)}`,
        );
        if (!live) return;
        setUnreachable(null);
        if (res.status !== "running") {
          setStopped(true);
          live = false; // verdict reached — stop polling
        }
      } catch {
        // A transient/backend hiccup isn't a verdict — keep the iframe up, surface
        // a dim strip, and retry on the next tick rather than flashing a false
        // "stopped" state.
        if (live) setUnreachable(nowLabel());
      }
    };
    void check();

    const interval = setInterval(() => {
      if (live && document.visibilityState === "visible") void check();
    }, STATUS_POLL_MS);
    const onVisibilityChange = (): void => {
      if (live && document.visibilityState === "visible") void check();
    };
    const onFocus = (): void => {
      if (live) void check();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("focus", onFocus);

    onCleanup(() => {
      live = false;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("focus", onFocus);
    });
  });

  return (
    <Show
      when={!stopped()}
      fallback={
        <EmptyState
          icon="warning"
          message="Live preview stopped"
          hint="Live preview stopped (sandbox went idle) — ask the assistant to serve it again."
        />
      }
    >
      <div class="flex h-full min-h-0 flex-col">
        <Show when={unreachable()}>
          {(t) => (
            <div class="shrink-0 px-2 py-1">
              <Text variant="micro" tone="dim">
                Live preview unreachable — last checked {t()}
              </Text>
            </div>
          )}
        </Show>
        <div class="min-h-0 flex-1">
          <SandboxedFrame
            src={apiUrl(props.live.url)}
            title={props.live.title ?? "Live view"}
            reloadKey={props.reloadKey}
            fontStep={props.fontStep}
          />
        </div>
      </div>
    </Show>
  );
}
