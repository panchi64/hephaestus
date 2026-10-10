import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  type JSX,
} from "solid-js";
import {
  Combobox,
  EmptyState,
  Modal,
  StatusFlag,
  Text,
  confirm,
  toast,
} from "~/ui";
import {
  MessageItem,
  ParkDock,
  ViewportPanel,
  collectViewItems,
} from "~/features/chat";
import {
  decodeModelValue,
  encodeModelValue,
  modelPickerGroups,
} from "~/lib/stores/models";
import type { ComparePane } from "./data";

/** One column of the side-by-side compare: a per-pane model picker and the live
 *  transcript for that model. The transcript reuses the chat turn renderer
 *  (`MessageItem`) wired to this pane's stream, so reasoning, tools, host
 *  commands, approvals, artifacts, and previews all render at full fidelity —
 *  and per-turn actions (regenerate / edit / version / pin / delete / rewind)
 *  operate on this pane's own conversation.
 *
 *  A pane is half a screen wide, so its View can't sit *beside* the transcript the way
 *  chat's does; it opens as an overlay off the same inline chips instead. Same
 *  `ViewportPanel`, same versions/PREVIEW/CODE — it's fully controlled, so the pane just
 *  owns the little state it needs (`CMP-2`). */
export function ComparePaneView(props: {
  pane: ComparePane;
  label: string;
}): JSX.Element {
  const stream = () => props.pane.stream;

  // Follow the stream: keep the transcript pinned to the bottom as the in-flight
  // turn grows, but yield the moment the operator scrolls up to read back — only
  // re-pin when they return near the bottom. Without this guard every streamed
  // token would yank a reader who scrolled up straight back down.
  let scrollEl: HTMLDivElement | undefined;
  let pinned = true;
  const onScroll = () => {
    if (!scrollEl) return;
    const distance =
      scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
    pinned = distance < 80; // within 80px of the bottom counts as attached
  };
  const tick = createMemo(() => {
    const msgs = stream().messages;
    const last = msgs[msgs.length - 1];
    let n = msgs.length + (last?.content?.length ?? 0);
    for (const b of last?.blocks ?? []) {
      if (b.kind === "thinking" || b.kind === "text") n += b.text.length;
      else n += 1;
    }
    return n;
  });
  createEffect(() => {
    tick();
    if (!pinned) return;
    queueMicrotask(() => {
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  });
  // A new turn re-attaches the follow, so a just-sent answer is tracked even if
  // the operator had scrolled up during the previous one.
  let wasSending = false;
  createEffect(() => {
    const isSending = stream().sending();
    if (isSending && !wasSending) pinned = true;
    wasSending = isSending;
  });

  const value = () => {
    const sel = props.pane.selection();
    return sel ? encodeModelValue(sel) : "";
  };

  // This pane's View: the same ordered version list the chat viewport builds, from this
  // pane's own conversation. Opening is chip-driven — there is no persistent panel to
  // auto-open into, and no per-thread persistence, because a compare thread is scratch.
  const viewItems = createMemo(() =>
    collectViewItems(stream().messages, stream().snapshots()),
  );
  const [openKey, setOpenKey] = createSignal<string | null>(null);
  const [tab, setTab] = createSignal<"preview" | "code">("preview");
  const [fontStep, setFontStep] = createSignal(0);
  const [softWrap, setSoftWrap] = createSignal(false);
  // The overlay's own "fullscreen" is simply a wider dialog — the panel already owns the
  // control, so it drives the width rather than rendering a dead button.
  const [wide, setWide] = createSignal(false);

  return (
    <div class="flex min-h-0 min-w-0 flex-1 flex-col border border-line bg-surface">
      <header class="flex items-center justify-between gap-2 p-2">
        <div class="flex min-w-0 items-center gap-2">
          <Text variant="label" tone="dim">
            {props.label}
          </Text>
          <Combobox
            groups={modelPickerGroups()}
            value={value()}
            onChange={(v) => props.pane.setSelection(decodeModelValue(v))}
            leading="cpu"
            placeholder="No model"
            searchPlaceholder="Search models…"
            emptyHint="No models — add an endpoint in settings"
            aria-label={`Model for ${props.label}`}
          />
        </div>
        <StatusFlag
          status={stream().sending() ? "info" : "idle"}
          dot={stream().sending()}
          pulse={stream().sending()}
        >
          {stream().sending() ? "Streaming" : "Idle"}
        </StatusFlag>
      </header>

      <div
        ref={scrollEl}
        onScroll={onScroll}
        class="min-h-0 flex-1 overflow-y-auto"
      >
        <Show
          when={stream().messages.length}
          fallback={
            <EmptyState
              icon="compare"
              message="No response yet"
              hint="Send a message below to compare this model."
            />
          }
        >
          <For each={stream().messages}>
            {(message) => (
              <MessageItem
                message={message}
                onResolveHostCommands={stream().resolveHostCommands}
                onOpenInView={setOpenKey}
                viewItems={viewItems}
                onReattach={() => {
                  if (message.runId)
                    void stream().reattachRun(message.runId, {
                      fromSeq: stream().lastSeq(),
                    });
                }}
                onRegenerate={() => void stream().regenerate(message.id)}
                onEditMessage={(id, text) => void stream().edit(id, text)}
                onSwitchVersion={(id, i) => void stream().switchVersion(id, i)}
                onTogglePin={() => void stream().toggleMessagePin(message.id)}
                onRewind={() => void stream().rewind(message.id)}
                onDelete={async () => {
                  if (
                    await confirm({
                      title: "Delete this message?",
                      detail: "This removes it and everything after it.",
                      confirmLabel: "Delete",
                      tone: "alert",
                    })
                  ) {
                    await stream().removeMessage(message.id);
                    toast.success("Message deleted");
                  }
                }}
              />
            )}
          </For>
        </Show>
      </div>

      {/* A pane runs real turns against real tools, so it can park on an approval or a
          question like any other run — and parks are answered in the dock, never on the
          transcript rail. Without this the pane would render nothing at all for one and
          wait forever. Per pane, not per screen: the two panes park independently, and
          each resumes its own run. */}
      <Show when={stream().park()}>
        {(park) => (
          <div class="shrink-0 px-2 pb-2">
            <ParkDock
              park={park()}
              draft={stream().parkDraft()}
              onDraft={stream().patchParkDraft}
              onStop={() => void stream().cancel()}
              onSubmit={(settlement) =>
                stream().resolvePark(park().messageId, settlement)
              }
            />
          </div>
        )}
      </Show>

      <Modal
        open={openKey() !== null}
        onClose={() => setOpenKey(null)}
        title={`${props.label} — VIEW`}
        class={wide() ? "max-w-[95vw]" : "max-w-4xl"}
      >
        <div class="h-[70vh] min-h-0">
          <ViewportPanel
            items={viewItems()}
            selectedKey={openKey()}
            onSelect={setOpenKey}
            activeTab={tab()}
            onSelectTab={setTab}
            fontStep={fontStep()}
            onFontStep={setFontStep}
            softWrap={softWrap()}
            onToggleWrap={() => setSoftWrap((on) => !on)}
            fullscreen={wide()}
            onToggleFullscreen={() => setWide((on) => !on)}
          />
        </div>
      </Modal>
    </div>
  );
}
