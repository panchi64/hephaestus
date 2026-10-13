/** Reports which ids are new since the previous reading of a list.
 *
 *  Seeded with the list as it was first mounted, so a thread opening never
 *  counts its history as arriving. An id is reported once, ever: a row that
 *  leaves the list and comes back (a settled call moving from the pinned run
 *  into the fold) is a move, not an arrival. */
export function createArrivalTracker(
  initial: readonly string[],
): (ids: readonly string[]) => ReadonlySet<string> {
  const known = new Set(initial);
  return (ids) => {
    const fresh = new Set<string>();
    for (const id of ids) {
      if (known.has(id)) continue;
      known.add(id);
      fresh.add(id);
    }
    return fresh;
  };
}
