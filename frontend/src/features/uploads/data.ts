import {
  createEffect,
  createResource,
  createRoot,
  createSignal,
  onCleanup,
  untrack,
  type Accessor,
  type Resource,
} from "solid-js";
import { createStore, produce } from "solid-js/store";
import type { ComposerAttachment, ComposerAttachmentsApi } from "~/ui";
import { api } from "~/lib/api";
import type { Upload, UploadStatus } from "./model";

/* ── Backend DTOs → seam types ────────────────────────────────────────────── */

interface UploadSummaryOut {
  id: string;
  filename: string;
  mime: string;
  sizeBytes: number;
  status: UploadStatus;
  vision: boolean;
  extractor: string | null;
  hasText: boolean;
  note: string | null;
  kbExcluded: boolean;
  createdAt: string;
  updatedAt: string;
}

interface UploadOut {
  id: string;
  filename: string;
  mime: string;
  sizeBytes: number;
  status: UploadStatus;
  vision: boolean;
  extractor: string | null;
  extractedText: string | null;
  note: string | null;
  kbExcluded: boolean;
  createdAt: string;
  updatedAt: string;
}

function toUpload(dto: UploadSummaryOut): Upload {
  return {
    id: dto.id,
    name: dto.filename,
    mime: dto.mime,
    sizeBytes: dto.sizeBytes,
    status: dto.status,
    vision: dto.vision,
    extractor: dto.extractor ?? undefined,
    note: dto.note ?? undefined,
    kbExcluded: dto.kbExcluded,
  };
}

function toUploadDetail(dto: UploadOut): Upload {
  return {
    id: dto.id,
    name: dto.filename,
    mime: dto.mime,
    sizeBytes: dto.sizeBytes,
    status: dto.status,
    vision: dto.vision,
    extractor: dto.extractor ?? undefined,
    note: dto.note ?? undefined,
    extractedText: dto.extractedText ?? "",
    kbExcluded: dto.kbExcluded,
  };
}

/* ── List (the seam) ──────────────────────────────────────────────────────── */

const [listTick, setListTick] = createSignal(0);

async function fetchUploads(): Promise<Upload[]> {
  const rows = await api.get<UploadSummaryOut[]>("/uploads");
  return rows.map(toUpload);
}

// One shared list resource for every consumer (the composer's chips, the sent-message
// attachments) so they don't each spin a duplicate `/uploads` fetch.
// Created under a detached root (like the chat stream) so it outlives any single
// component and stays live across navigation; keyed on `listTick`.
let uploadsResource: Resource<Upload[]> | undefined;

export function useUploads(): Resource<Upload[]> {
  if (!uploadsResource) {
    uploadsResource = createRoot(() => {
      const [data] = createResource(listTick, fetchUploads);
      return data;
    });
  }
  return uploadsResource;
}

/** Invalidate the list after a mutation (or while polling in-flight extractions). */
export function refreshUploads(): void {
  setListTick((n) => n + 1);
}

/* ── Mutations ────────────────────────────────────────────────────────────── */

/** Upload one file. Returns the created (or recognized-duplicate) upload. */
async function uploadFile(file: File): Promise<Upload> {
  const form = new FormData();
  form.append("file", file, file.name);
  const dto = await api.postForm<UploadOut>("/uploads", form);
  refreshUploads();
  return toUploadDetail(dto);
}

async function retryUpload(id: string): Promise<void> {
  await api.post(`/uploads/${id}/retry`);
  refreshUploads();
}

/** Include or exclude a file's text from the knowledge base / retrieval corpus. */
async function setUploadKbExcluded(id: string, value: boolean): Promise<void> {
  await api.patch(`/uploads/${id}`, { kbExcluded: value });
  refreshUploads();
}

/* ── Composer attachments controller ──────────────────────────────────────────
   The shared Composer (`~/ui`) owns the attachment chips but can't reach this
   seam, so it takes this controller from the feature layer. Files upload the
   instant they're picked or dropped; the chat message only references the
   resulting upload ids, and each chip polls its extraction progress. */

/** Map an upload's lifecycle to the chip's coarser status. */
function toAttachmentStatus(
  status: UploadStatus,
): ComposerAttachment["status"] {
  if (status === "done") return "ready";
  if (status === "error") return "error";
  return "extracting";
}

/** The backend's own words for a failure, when an error carries any. */
function failureReason(err: unknown): string | undefined {
  return err instanceof Error && err.message ? err.message : undefined;
}

/**
 * Build a Composer attachment controller. `attach` uploads each file immediately
 * and tracks it as a chip; a single interval polls any still-extracting upload
 * until it settles. Toggling KB membership and removing are relayed to the seam.
 * `snapshot`/`restore` let the Composer put a message's files back when its send
 * is refused, and `retry` re-runs a failed chip. Bind once per composer; the poll
 * cleans itself up with the owning component.
 */
export function createComposerAttachments(): ComposerAttachmentsApi {
  const [items, setItems] = createStore<ComposerAttachment[]>([]);
  // The store proxy is itself reactive; the Composer reads it through this
  // accessor so its `For`/derivations track adds, removes, and field changes.
  const accessor: Accessor<ComposerAttachment[]> = () => items;

  const patch = (id: string, fn: (a: ComposerAttachment) => void): void => {
    setItems(
      produce((list) => {
        const a = list.find((x) => x.id === id);
        if (a) fn(a);
      }),
    );
  };

  // Track extraction off the shared uploads *list* — its summaries already carry
  // `status` with no extracted-text decrypt, so a chip's progress costs the same
  // one cheap list read every consumer shares, never a per-file detail
  // GET that would decrypt the whole document just to read a status enum.
  const uploads = useUploads();
  const inFlight = () => items.some((a) => a.status === "extracting");

  // Reconcile each still-extracting chip against the freshest list row whenever
  // the list resolves (re-runs after each `refreshUploads()` below). The list is
  // the only tracked dependency; `items` is read untracked so reconciling a chip
  // doesn't re-trigger this effect off its own write.
  createEffect(() => {
    const rows = uploads();
    if (!rows) return;
    const index = new Map(rows.map((u) => [u.id, u]));
    untrack(() => {
      for (const a of items) {
        if (a.status !== "extracting") continue;
        const row = index.get(a.id);
        if (row)
          patch(a.id, (x) => {
            x.status = toAttachmentStatus(row.status);
            x.kbExcluded = row.kbExcluded ?? false;
            // A failed extraction explains itself in `note` ("could not read the
            // file"); the chip shows it rather than a bare "Failed".
            x.error = x.status === "error" ? row.note : undefined;
          });
      }
    });
  });

  // Drive the list refresh only while something is extracting; the interval is
  // created on demand and torn down the moment every chip settles, so an idle
  // composer issues no polling traffic at all.
  let timer: ReturnType<typeof setInterval> | undefined;
  const stopPolling = () => {
    if (timer) {
      clearInterval(timer);
      timer = undefined;
    }
  };
  const ensurePolling = () => {
    if (timer || !inFlight()) return;
    timer = setInterval(() => {
      if (!inFlight()) {
        stopPolling();
        return;
      }
      refreshUploads();
    }, 1500);
  };
  onCleanup(stopPolling);

  // A chip whose POST never landed has no server row to retry, so the file itself is
  // kept against its provisional id until the upload succeeds.
  const unsent = new Map<string, File>();

  const startUpload = (tempId: string, file: File): void => {
    unsent.set(tempId, file);
    void uploadFile(file)
      .then((u) => {
        unsent.delete(tempId);
        patch(tempId, (x) => {
          x.id = u.id;
          x.name = u.name;
          x.status = toAttachmentStatus(u.status);
          x.kbExcluded = u.kbExcluded ?? false;
          x.error = x.status === "error" ? u.note : undefined;
        });
        // A still-extracting upload needs the list poll running to settle it.
        ensurePolling();
      })
      .catch((err: unknown) =>
        patch(tempId, (x) => {
          x.status = "error";
          x.error = failureReason(err);
        }),
      );
  };

  const attach = (files: File[]): void => {
    for (const file of files) {
      // A provisional chip id keyed off the uploading file, swapped for the real
      // upload id once the POST resolves. Lets the chip show "UPLOADING" instantly.
      const tempId = `pending-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      setItems(
        produce((list) =>
          list.push({
            id: tempId,
            name: file.name,
            status: "uploading",
            kbExcluded: false,
          }),
        ),
      );
      startUpload(tempId, file);
    }
  };

  const retry = (id: string): void => {
    const current = items.find((a) => a.id === id);
    if (current?.status !== "error") return;
    const file = unsent.get(id);
    if (file) {
      // The POST itself failed: there is nothing server-side to retry, so upload
      // the same file again under the same chip.
      patch(id, (x) => {
        x.status = "uploading";
        x.error = undefined;
      });
      startUpload(id, file);
      return;
    }
    // The file landed but extraction failed: the backend re-runs it, and the list
    // poll carries the chip from extracting to wherever it ends up.
    patch(id, (x) => {
      x.status = "extracting";
      x.error = undefined;
    });
    ensurePolling();
    void retryUpload(id).catch((err: unknown) =>
      patch(id, (x) => {
        x.status = "error";
        x.error = failureReason(err);
      }),
    );
  };

  const remove = (id: string): void => {
    unsent.delete(id);
    setItems((list) => list.filter((a) => a.id !== id));
  };

  // The Composer only snapshots a message whose files have all settled — a send with
  // any still in flight is held until they land — so a restore puts back finished
  // chips, and nothing in it needs an upload's outcome replayed.
  const snapshot = (): ComposerAttachment[] => items.map((a) => ({ ...a }));

  const restore = (saved: ComposerAttachment[]): void => {
    setItems(saved.map((a) => ({ ...a })));
    ensurePolling();
  };

  const toggleKbExcluded = (id: string): void => {
    const current = items.find((a) => a.id === id);
    if (!current) return;
    const next = !current.kbExcluded;
    patch(id, (x) => (x.kbExcluded = next)); // optimistic echo
    void setUploadKbExcluded(id, next).catch(() =>
      patch(id, (x) => (x.kbExcluded = current.kbExcluded)),
    );
  };

  const clear = (): void => {
    // The failed files kept for a retry go with their chips.
    unsent.clear();
    setItems([]);
  };

  return {
    items: accessor,
    attach,
    remove,
    toggleKbExcluded,
    clear,
    snapshot,
    restore,
    retry,
  };
}
