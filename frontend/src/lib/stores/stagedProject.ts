/**
 * The project the next new conversation is filed under. Outside the chat room because the
 * rail and the directory chooser set it too; read only by the send that creates a thread.
 * Memory-only, so a reload stages a thread with no project.
 */

import { createSignal } from "solid-js";

const [stagedProjectId, setStagedProjectId] = createSignal<string | undefined>(
  undefined,
);

export { stagedProjectId, setStagedProjectId };
