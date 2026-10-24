import type { CommandBoundaryFacts } from "./model";

export interface BoundaryReadout {
  reach?: string;
  ranWithNetwork: boolean;
  fence?: "fenced" | "unfenced";
  unfencedReason?: string;
}

/** What the boundary line prints, or null when the result carries neither a declared
 *  reach nor a fence verdict. The host-reach command tool reports the verdict alone. */
export function boundaryReadout(
  facts: CommandBoundaryFacts,
): BoundaryReadout | null {
  if (!facts.reach && facts.fenced === undefined) return null;
  return {
    reach: facts.reach,
    ranWithNetwork: facts.reach === "offline" && facts.network === true,
    fence:
      facts.fenced === undefined
        ? undefined
        : facts.fenced
          ? "fenced"
          : "unfenced",
    unfencedReason: facts.fenced === false ? facts.unfencedReason : undefined,
  };
}

/** The refused host the operator can allow for the thread. Only a VM command has a
 *  machine whose egress is theirs to widen; a host-reach refusal gets no button. */
export function allowableHost(facts: CommandBoundaryFacts): string | undefined {
  return facts.reach === "host" ? undefined : facts.deniedHost;
}
