import { createResource, type Accessor } from "solid-js";
import { settled } from "~/lib/resource";
import { api } from "~/lib/api";
import { toast } from "~/ui";

/** What `PathInput` calls: open a chooser and resolve to a path (or null when the
 *  operator cancelled). */
export type PathPicker = (opts: {
  mode: "file" | "directory";
  title?: string;
  extensions?: string[] | null;
}) => Promise<string | null>;

interface PickerAvailabilityDTO {
  available: boolean;
  reason?: string | null;
}

/** Whether this host can open a native file/folder dialog. */
async function fetchPickerAvailability(): Promise<PickerAvailabilityDTO> {
  const dto = await api.get<PickerAvailabilityDTO>("/host/file-picker");
  return { available: dto.available, reason: dto.reason ?? null };
}

/** Open a native chooser on the host and return the absolute path, or null when
 *  the operator cancelled. A browser cannot produce a host path, so the backend —
 *  which runs on their machine — opens the dialog and hands the path back. */
async function pickPath(input: {
  mode: "file" | "directory";
  title?: string;
  startDir?: string | null;
  extensions?: string[] | null;
}): Promise<string | null> {
  const dto = await api.post<{ path: string | null }>("/host/file-picker", {
    mode: input.mode,
    title: input.title ?? "Choose",
    start_dir: input.startDir ?? null,
    extensions: input.extensions ?? null,
  });
  return dto.path;
}

/**
 * A `PathInput`'s BROWSE handler, or `undefined` when this host has no native
 * chooser — the control hides itself and the typed field carries on working. The
 * availability probe is cheap and answers once per mount.
 *
 * It lived in the model-serving module, because picking a models directory was
 * the first thing that needed it. Serving is gone and this is not about models
 * at all: it is how any field that wants an absolute host path gets one, and
 * Projects has always been its other caller.
 */
export function usePathPicker(): Accessor<PathPicker | undefined> {
  const [availability] = createResource(fetchPickerAvailability);
  return () => {
    if (!settled(availability)?.available) return undefined;
    return async (opts) => {
      try {
        return await pickPath(opts);
      } catch (err) {
        // A chooser that can't open (a macOS host with no GUI session advertises
        // osascript but can't show a dialog) has to say so — a BROWSE button that
        // silently does nothing is worse than not offering one.
        toast.error(
          (err as { detail?: string })?.detail ??
            "Couldn't open a file chooser — type the path instead",
        );
        return null;
      }
    };
  };
}
