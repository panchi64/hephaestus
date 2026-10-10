import { createSignal, onCleanup, onMount, Show, type JSX } from "solid-js";
import { Button } from "./Button";
import { Drawer } from "./Drawer";
import { Text } from "../primitives/Text";
import { Row } from "../primitives/Row";
import { Stack } from "../primitives/Stack";

export interface EditorShellProps {
  /** The return path to the parent list, as either a route or a callback.
   *  `onBack` wins when both are given — an editor mounted inside an overlay
   *  has a parent list but no URL to go back to, and a link there would leave
   *  the overlay entirely. */
  backHref?: string;
  onBack?: () => void;
  backLabel: string;
  /** Title shown in the header. */
  title: string;
  /** Unsaved indicator — shows a dot and arms the beforeunload guard. */
  dirty?: boolean;
  /** Sub-line under the title (e.g. word count / updated time). */
  meta?: JSX.Element;
  /** Status chip(s), right side of the header. */
  status?: JSX.Element;
  /** Primary actions, right side of the header (e.g. a SAVE button). */
  actions?: JSX.Element;
  /** Right-hand tools column. A thunk so it can render in both the desktop
   *  column and the mobile drawer. */
  aside?: () => JSX.Element;
  /** Mobile drawer title + trigger label. Default "Tools". */
  asideLabel?: string;
  /** The editor body (e.g. a Textarea). */
  children: JSX.Element;
}

/** Shared full-height editor skeleton: back link, header (title + dirty dot +
 *  status + actions), a main editing column, and a tools aside (desktop column /
 *  mobile drawer). Also arms the unsaved-changes guard from `dirty`. The Document
 *  and Skill editors both compose this, so the two surfaces stay identical. */
export function EditorShell(props: EditorShellProps): JSX.Element {
  const [asideOpen, setAsideOpen] = createSignal(false);
  const asideLabel = () => props.asideLabel ?? "Tools";

  // Unsaved-changes guard — owned here so every editor gets it for free.
  onMount(() => {
    function beforeUnload(e: BeforeUnloadEvent): void {
      if (props.dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", beforeUnload);
    onCleanup(() => window.removeEventListener("beforeunload", beforeUnload));
  });

  return (
    <div class="flex h-full min-h-0 gap-4">
      {/* Editor column */}
      <section class="flex min-w-0 flex-1 flex-col gap-3">
        {/* Persistent return path to the parent list. */}
        <Button
          variant="ghost"
          size="sm"
          leading="chevron-left"
          href={props.onBack ? undefined : props.backHref}
          onClick={props.onBack}
          class="self-start"
        >
          {props.backLabel}
        </Button>

        <header class="flex items-center justify-between gap-3 border-b border-line pb-3">
          <Stack gap={0}>
            <Row gap={2} align="center">
              <Text variant="readout" tone="bright">
                {props.title}
              </Text>
              <Show when={props.dirty}>
                <span class="h-1.5 w-1.5 bg-warn" title="Unsaved changes" />
              </Show>
            </Row>
            <Show when={props.meta}>{props.meta}</Show>
          </Stack>
          <Row gap={2} align="center">
            <Show when={props.status}>{props.status}</Show>
            <Show when={props.aside}>
              <Button
                variant="ghost"
                size="sm"
                leading="note"
                onClick={() => setAsideOpen(true)}
                class="lg:hidden"
              >
                {asideLabel()}
              </Button>
            </Show>
            {props.actions}
          </Row>
        </header>

        <div class="min-h-0 flex-1">{props.children}</div>
      </section>

      {/* Tools aside — desktop column */}
      <Show when={props.aside}>
        <aside class="hidden w-72 shrink-0 flex-col gap-4 lg:flex">
          {props.aside!()}
        </aside>

        {/* Tools aside — mobile drawer */}
        <Drawer
          open={asideOpen()}
          onClose={() => setAsideOpen(false)}
          title={asideLabel()}
          side="right"
        >
          <Stack gap={4}>{props.aside!()}</Stack>
        </Drawer>
      </Show>
    </div>
  );
}
