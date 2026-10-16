/**
 * One read about the open thread, fetched once for everyone who shows it.
 *
 * **`settled`, not the resource's own value.** A plain read goes `undefined` for the
 * length of a refetch, so a chip blinked out of the header and back in at exactly the
 * moment the operator looks up from a finished answer. `settled` holds the previous
 * answer until the new one lands. It also reads `undefined` before the first answer
 * instead of suspending, which a bare `.latest` would do — and the nearest boundary is
 * the whole route.
 *
 * **The held value is tagged with the thread it was read for.** A thread switch is a
 * refetch like any other, and `settled` would hand the previous thread's answer to the
 * new one until its own landed.
 */

import { createResource } from "solid-js";
import { settled } from "~/lib/resource";

export interface ThreadRead<T> {
  latest: () => T | undefined;
  refetch: () => void;
}

/** `wanted` is the thread to ask about, or null for none; `revision` is bumped by the
 *  caller when a turn settles, since that is when the agent has just changed something. */
export function createThreadRead<T>(
  wanted: () => string | null,
  revision: () => number,
  read: (id: string) => Promise<T>,
): ThreadRead<T> {
  const [resource, { refetch }] = createResource(
    () => {
      const id = wanted();
      return id === null ? undefined : ([id, revision()] as const);
    },
    async ([id]) => ({ id, value: await read(id) }),
  );
  return {
    latest: () => {
      const held = settled(resource);
      return held && held.id === wanted() ? held.value : undefined;
    },
    refetch: () => {
      if (wanted() !== null) void refetch();
    },
  };
}
