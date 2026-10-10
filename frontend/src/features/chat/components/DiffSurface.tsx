import { For, Show, createMemo, createSignal, type JSX } from "solid-js";
import {
  Button,
  Collapse,
  DiffView,
  EmptyState,
  Icon,
  Segmented,
  Text,
  confirm,
  toast,
  type TextTone,
} from "~/ui";
import { isApiError } from "~/lib/api";
import { relativeTime } from "~/lib/format";
import { refreshProjects } from "~/lib/stores/projects";
import {
  discardBranch,
  mergeBranch,
  type BranchState,
  type FileChange,
} from "../data";
import { PaneToolbar } from "./PaneFrame";

/**
 * What a code thread has changed, and the two ways it ends.
 *
 * This was a modal over a raw `<pre>`, opened from the header chip. Two things were
 * wrong with that. A patch is something you read *against* the conversation that
 * produced it — a dialog covers the very transcript you are checking it against — and
 * a unified diff rendered as preformatted text is the one thing in the product with a
 * purpose-built renderer sitting unused three files away.
 *
 * **MERGE is the operator's approval.** It is the only action in the product that
 * writes their own checkout, which is exactly why it is a button they press and not a
 * tool the agent can call: everything the agent does stays on a branch they can throw
 * away, and the one step off that branch is theirs. Moving the patch into a pane does
 * not move that decision — the buttons come with it.
 *
 * **The triage is the backend's ruling, rendered.** A fourteen-file diff read top to
 * bottom is fourteen files in git's order, which is alphabetical and therefore says
 * nothing about which of them can hurt. The backend now ships each file's category,
 * its risk band and a phrase saying why, already sorted — and this surface's whole
 * contract is to render that order and that verdict **verbatim**. It never sorts, never
 * re-bands, and never infers a category from a path. The one thing it derives is where
 * to draw a separator, and it derives that from two adjacent rows disagreeing about a
 * risk the server assigned — a rule about the list it was handed, not a second opinion
 * on its contents.
 */

/* Every map below is keyed by plain `string` and falls back, although the seam
 * types these three fields as closed unions. The union is the frontend's read of
 * a wire field, not a guarantee about it: a backend that grows an eighth category
 * would otherwise render a blank cell, and a blank cell beside a file the server
 * flagged is the worst possible failure for this surface. An unrecognized value
 * shows its own slug instead. */

/** Ordered high → elevated → normal, which is the order the server sends rows
 *  in. `normal` names no band of its own — the run it heads is the residue, and
 *  captioning it "normal risk" spends a heading saying nothing. A risk the
 *  server grows later still gets one, so an unknown verdict stays visible. */
const RISK_BAND: Record<string, { label: string; tone: TextTone }> = {
  high: { label: "High risk", tone: "alert" },
  elevated: { label: "Elevated risk", tone: "warn" },
  normal: { label: "The rest", tone: "dim" },
};

/** The word beside each row's marker. Never a tone on its own (§12): the status
 *  is spelled out, and the colour only reinforces it. */
const STATUS_WORD: Record<string, { word: string; tone: TextTone }> = {
  added: { word: "ADD", tone: "nominal" },
  modified: { word: "MOD", tone: "default" },
  deleted: { word: "DEL", tone: "alert" },
  renamed: { word: "REN", tone: "info" },
  copied: { word: "CPY", tone: "info" },
  "type-changed": { word: "TYPE", tone: "warn" },
  unmerged: { word: "CONF", tone: "warn" },
};

/** A status the server grows later still reads: its own slug, upper-cased. */
function statusOf(status: string): { word: string; tone: TextTone } {
  return (
    STATUS_WORD[status] ?? {
      word: status.toUpperCase().slice(0, 6),
      tone: "dim",
    }
  );
}

/** Uppercased for the plate voice, with the server's own slug left legible —
 *  this is a machine label naming a class, not a phrase. */
function bandOf(risk: string): { label: string; tone: TextTone } {
  return RISK_BAND[risk] ?? { label: `${risk} risk`, tone: "dim" };
}

function FileRow(props: {
  file: FileChange;
  patch: string;
  /** Held by the list, keyed by path — a refetch rebuilds every row, and a row
   *  that owned its own disclosure would snap shut on each one. */
  open: boolean;
  onToggle: () => void;
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  const open = (): boolean => props.open;
  const status = createMemo(() => statusOf(props.file.status));
  const deleted = () => props.file.status === "deleted";

  return (
    <div class="border-b border-line">
      <button
        type="button"
        onClick={() => props.onToggle()}
        aria-expanded={open()}
        class="flex w-full min-w-0 flex-col gap-1 px-3 py-2 text-left hover:bg-raised"
      >
        <div class="flex min-w-0 items-baseline gap-2">
          <Text
            variant="meta"
            tone={status().tone}
            class="w-10 shrink-0 tabular-nums"
          >
            {status().word}
          </Text>
          <Text
            variant="body"
            tone={deleted() ? "dim" : "bright"}
            class={
              deleted()
                ? "min-w-0 flex-1 truncate line-through"
                : "min-w-0 flex-1 truncate"
            }
          >
            {props.file.path}
          </Text>
          <Show
            when={!props.file.binary}
            fallback={
              <Text variant="meta" tone="dim" class="shrink-0">
                BINARY
              </Text>
            }
          >
            <Text variant="code" class="shrink-0 tabular-nums">
              <Text variant="code" tone="nominal">
                +{props.file.insertions}
              </Text>{" "}
              <Text variant="code" tone="alert">
                −{props.file.deletions}
              </Text>
            </Text>
          </Show>
          <Icon
            name={open() ? "chevron-down" : "chevron-right"}
            size={12}
            class="shrink-0 text-dim"
          />
        </div>

        {/* A rename's old path is the half that answers "where did this go?",
            and nothing else on the row carries it. */}
        <Show when={props.file.oldPath}>
          {(from) => (
            <Text
              variant="micro"
              tone="dim"
              class="min-w-0 truncate pl-6 @md:pl-12"
            >
              was {from()}
            </Text>
          )}
        </Show>

        <div class="flex min-w-0 flex-wrap items-baseline gap-x-2 pl-6 @md:pl-12">
          <Text variant="plate" tone="dim" class="shrink-0">
            {props.file.category}
          </Text>
          {/* The server's phrase, verbatim. */}
          <Text
            variant="micro"
            tone={deleted() ? "alert" : "dim"}
            class="min-w-0 flex-1"
          >
            {props.file.reason}
          </Text>
        </div>
      </button>

      <Collapse open={open()}>
        {/* A bounded peek, with its own scroller — the one place a diff nests
            inside another scroll region, and capped precisely so it cannot
            swallow the list it was opened from. A file too long to read here is
            read in PATCH. */}
        <div class="border-t border-line">
          <DiffView
            diff={props.patch}
            file={props.file.path}
            layout="hunks"
            fontStep={props.fontStep}
            softWrap={props.softWrap}
            class="max-h-80"
          />
        </div>
      </Collapse>
    </div>
  );
}

/** The risk-ranked list, in the order it arrived. A heading is drawn wherever
 *  the row's risk differs from the one above it — so the bands are exactly the
 *  server's runs, and a list that arrives in some other order would render as
 *  that order with more headings, never as a re-sort. */
function FileList(props: {
  files: FileChange[];
  patch: string;
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  const banded = createMemo(() => {
    const runs = new Map<string, number>();
    for (const f of props.files) runs.set(f.risk, (runs.get(f.risk) ?? 0) + 1);
    return props.files.map((file, i) => ({
      file,
      /** First of its run, so it opens a band. */
      opens: i === 0 || props.files[i - 1].risk !== file.risk,
      count: runs.get(file.risk) ?? 0,
    }));
  });

  const [open, setOpen] = createSignal<ReadonlySet<string>>(new Set());
  const toggle = (path: string): void => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(path)) next.add(path);
      return next;
    });
  };

  return (
    <div class="h-full overflow-auto scrollbar-thin">
      <For each={banded()}>
        {(row) => (
          <>
            <Show when={row.opens}>
              {/* Above the sticky bands an expanded diff brings with it — those
                  stick inside their own row's scroller, and without this the
                  later DOM order would paint them over the band. */}
              <div class="sticky top-0 z-30 flex items-baseline gap-2 border-y border-line bg-raised px-3 py-1">
                <Text variant="plate" tone={bandOf(row.file.risk).tone}>
                  {bandOf(row.file.risk).label}
                </Text>
                <Text variant="micro" tone="dim" class="ml-auto tabular-nums">
                  {row.count}
                </Text>
              </div>
            </Show>
            <FileRow
              file={row.file}
              patch={props.patch}
              open={open().has(row.file.path)}
              onToggle={() => toggle(row.file.path)}
              fontStep={props.fontStep}
              softWrap={props.softWrap}
            />
          </>
        )}
      </For>
    </div>
  );
}

export function DiffSurface(props: {
  branch: () => BranchState | null | undefined;
  onChanged: () => void;
  fontStep?: number;
  softWrap?: boolean;
}): JSX.Element {
  const [busy, setBusy] = createSignal(false);
  const [tab, setTab] = createSignal<"files" | "patch">("files");

  const stat = (b: BranchState): string => `+${b.insertions} −${b.deletions}`;

  /** `files` is [] when there is no branch yet, and absent entirely from a
   *  backend that predates it — either way there is no list to render. */
  const files = (b: BranchState): FileChange[] => b.files ?? [];

  const merge = async (b: BranchState): Promise<void> => {
    setBusy(true);
    try {
      await mergeBranch(b.conversationId);
      toast.success("Merged into your working tree.");
      props.onChanged();
      // The project's uncommitted count and current branch just changed.
      refreshProjects();
    } catch (err) {
      toast.error(
        isApiError(err)
          ? // A conflict is git's own message and the operator's to resolve —
            // paraphrasing it would make it less actionable, not more.
            err.detail
          : "Unable to merge this branch.",
      );
    } finally {
      setBusy(false);
    }
  };

  const discard = async (b: BranchState): Promise<void> => {
    const ok = await confirm({
      title: "Discard this branch?",
      detail:
        "Everything the agent changed in this conversation is thrown away. Your own working tree is untouched either way.",
      confirmLabel: "Discard",
      tone: "alert",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await discardBranch(b.conversationId);
      toast.success("Branch discarded.");
      props.onChanged();
    } catch (err) {
      toast.error(
        isApiError(err) ? err.detail : "Unable to discard this branch.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Show
      when={props.branch()}
      fallback={
        <EmptyState
          icon="branch"
          message="No branch"
          hint="Only a code thread works on a branch of its own."
        />
      }
    >
      {(b) => (
        <div class="flex h-full min-h-0 flex-col">
          <div class="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
            <Text variant="micro" tone="dim" class="min-w-0 truncate">
              {b().filesChanged} file{b().filesChanged === 1 ? "" : "s"} against{" "}
              {b().baseRef} · {stat(b())}
            </Text>

            <Show when={b().ahead}>
              {(ahead) => (
                <Text variant="micro" tone="dim" class="shrink-0">
                  {ahead()} COMMIT{ahead() === 1 ? "" : "S"}
                </Text>
              )}
            </Show>

            {/* Staleness, where the merge is pressed. `behind` is the number
                that decides whether this patch still applies to the thing it
                claims to change; a 0 says nothing and stays absent, so the
                warn tone only ever appears on a branch that has actually
                drifted. */}
            <Show when={b().behind}>
              {(behind) => (
                <Text variant="micro" tone="warn" class="shrink-0">
                  {behind()} behind {b().baseRef}
                </Text>
              )}
            </Show>
            <Show when={b().lastCommitAt}>
              {(at) => (
                <Text variant="micro" tone="dim" class="shrink-0">
                  Last commit {relativeTime(at())}
                </Text>
              )}
            </Show>
          </div>

          {/* Which view of the change is a control over this pane, so it rides
              the pane's header; the figures above stay with the body they
              describe. */}
          <Show when={files(b()).length}>
            <PaneToolbar>
              <Segmented
                aria-label="What to read"
                fill={false}
                value={tab()}
                onChange={setTab}
                options={[
                  {
                    value: "files",
                    label: "Files",
                    description: "Every changed file, ranked by risk.",
                  },
                  {
                    value: "patch",
                    label: "Patch",
                    description: "The whole diff, end to end.",
                  },
                ]}
              />
            </PaneToolbar>
          </Show>

          <div class="min-h-0 flex-1">
            <Show
              when={b().patch || files(b()).length}
              fallback={
                <EmptyState
                  icon="branch"
                  message="Nothing changed yet"
                  hint="The agent has not written anything on this branch."
                />
              }
            >
              {/* Without per-file rows there is no list to show, so the patch
                  is the only arm — which is also what an older backend gives. */}
              <Show
                when={files(b()).length && tab() === "files"}
                fallback={
                  <DiffView
                    diff={b().patch}
                    fontStep={props.fontStep}
                    softWrap={props.softWrap}
                    class="h-full"
                  />
                }
              >
                <FileList
                  files={files(b())}
                  patch={b().patch}
                  fontStep={props.fontStep}
                  softWrap={props.softWrap}
                />
              </Show>
            </Show>
          </div>

          {/* The two endings, at the bottom where a decision belongs — after the
              thing being decided about, not above it. */}
          <div class="flex shrink-0 flex-wrap items-center gap-2 px-3 py-2">
            <Button
              onClick={() => void merge(b())}
              disabled={busy() || !b().filesChanged}
            >
              Merge
            </Button>
            <Button
              variant="danger"
              onClick={() => void discard(b())}
              disabled={busy()}
            >
              Discard
            </Button>
            <Text variant="micro" tone="dim">
              Merge is the only thing that writes your own working tree.
            </Text>
          </div>
        </div>
      )}
    </Show>
  );
}
