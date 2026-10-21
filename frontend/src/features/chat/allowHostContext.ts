import { createContext, useContext } from "solid-js";

/** Let the thread a card belongs to reach a refused host. Provided by whichever surface
 *  knows that thread, so the card itself holds no conversation id and makes no request.
 *  Null where there is no single thread, as in a compare pane: the card offers nothing. */
export type AllowHost = (host: string) => Promise<void>;

export const AllowHostContext = createContext<AllowHost | null>(null);

export const useAllowHost = (): AllowHost | null =>
  useContext(AllowHostContext);
