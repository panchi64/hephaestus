/**
 * Pointing the rail at a directory on the operator's machine.
 *
 * It lives on its own rather than inside whichever control happens to call it, so any
 * surface that offers a directory runs the same act and stages the same project.
 *
 * Any directory does. Projects are still the storage and the git machinery, but they are
 * not paperwork the operator files before they can start: the native chooser hands back
 * a host path, and the backend files it on first use, idempotently by the path itself.
 */

import { createSignal, type Accessor } from "solid-js";
import { toast } from "~/ui";
import { usePathPicker } from "~/lib/hostPicker";
import { pathLabel } from "~/lib/format";
import { ensureProjectForPath, type Project } from "~/lib/stores/projects";
import { setStagedProjectId } from "~/lib/stores/stagedProject";

/** What to call a directory, wherever it is named.
 *
 *  The rail's heading and the chat header's subtitle must agree — they are two views of
 *  one staged thread, and a directory called one thing in the rail and another above the
 *  composer is exactly the ambiguity `parent/name` was added to remove. So the rule is
 *  written once: the operator's own name for the project when they have renamed it away
 *  from the directory's, and otherwise `parent/name`, which is the smallest thing that
 *  tells two directories both called `frontend` apart. */
export function directoryLabel(project: Project): string {
  const { parent, name } = pathLabel(project.rootPath);
  return project.name === name && parent ? `${parent}/${name}` : project.name;
}

export interface AddDirectory {
  /** Open the chooser, file what comes back, and stage it as the project of the next thread.
   *  Resolves to null when the operator cancelled, when this host has no chooser, or
   *  when the backend refused the path. */
  add: () => Promise<Project | null>;
  busy: Accessor<boolean>;
}

export function useAddDirectory(): AddDirectory {
  const picker = usePathPicker();
  const [busy, setBusy] = createSignal(false);

  const add = async (): Promise<Project | null> => {
    // Guarded here rather than by each caller disabling its own control: `busy` is
    // offered for the paint, but two choosers open from one double-click would both
    // resolve, and the second would overwrite the directory the operator picked first.
    if (busy()) return null;
    const pick = picker();
    if (!pick) {
      // No native chooser on this host. The projects section still takes a typed path,
      // so say where to go rather than leaving a dead button.
      toast.error(
        "No folder chooser on this host — add the directory under projects",
      );
      return null;
    }
    setBusy(true);
    try {
      const path = await pick({
        mode: "directory",
        title: "Choose a directory",
      });
      if (!path) return null;
      const project = await ensureProjectForPath(path);
      setStagedProjectId(project.id);
      // Said once, here, rather than discovered halfway through a session: a worktree
      // is cut from the project's base ref, so work the operator has not committed in
      // their own checkout is invisible to the agent. The rail's heading carries the
      // same two facts as a marker, since a toast is gone in seconds and this is a
      // property of the directory rather than of the moment it was added.
      if (!project.repo.isGitRepo)
        toast.error(
          `${project.name} isn't a git repository yet — create one under projects before the agent can use a worktree there`,
        );
      else if ((project.repo.uncommittedChanges ?? 0) > 0)
        toast.info(
          `${project.repo.uncommittedChanges} uncommitted change${
            project.repo.uncommittedChanges === 1 ? "" : "s"
          } in ${project.name} won't be visible to the agent`,
        );
      return project;
    } catch (err) {
      toast.error(
        (err as { detail?: string })?.detail ?? "Couldn't open that directory",
      );
      return null;
    } finally {
      setBusy(false);
    }
  };

  return { add, busy };
}
