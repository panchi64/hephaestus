import { createContext, useContext, type Accessor } from "solid-js";

/** The thread the room is showing, for a control deep in a card that acts on it. Null
 *  where there is no single thread, as in a compare pane. */
export const ConversationContext = createContext<Accessor<string | null>>(
  () => null,
);

export const useConversationId = (): Accessor<string | null> =>
  useContext(ConversationContext);
