/** Where on screen an event happened, read from the `data-ambient-source` anchors. */

export interface Point {
  x: number;
  y: number;
}

function anchor(name: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(
    `[data-ambient-source="${CSS.escape(name)}"]`,
  );
}

function centre(rect: DOMRect): Point {
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

/** The rail's inner edge at its middle, or the viewport's centre with no rail. */
function railEdge(): Point {
  const rail = document.querySelector(".heph-rail")?.getBoundingClientRect();
  if (!rail) return { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  return { x: rail.right, y: rail.top + rail.height / 2 };
}

/** A thread's ring starts at its rail row. A row folded away or scrolled out of the
 *  rail starts it at the rail's edge instead. An ask on the open thread starts at the
 *  park dock, which is where the operator answers it. */
export function threadSource(threadId: string, asking: boolean): Point {
  const row = anchor(`thread:${threadId}`);
  if (asking && row?.hasAttribute("data-selected")) {
    const park = anchor("park")?.getBoundingClientRect();
    if (park && park.width > 0) return centre(park);
  }
  const rect = row?.getBoundingClientRect();
  const rail = document.querySelector(".heph-rail")?.getBoundingClientRect();
  if (
    !rect ||
    rect.height === 0 ||
    (rail && (rect.bottom < rail.top || rect.top > rail.bottom))
  )
    return railEdge();
  return centre(rect);
}

function openRow(): HTMLElement | null {
  return document.querySelector<HTMLElement>(
    '[data-ambient-source^="thread:"][data-selected]',
  );
}

/** Whether this thread is the one open in the chat room. */
export function isOpenThread(threadId: string): boolean {
  return openRow()?.dataset.ambientSource === `thread:${threadId}`;
}

/** The open thread's source. A thread just started has no row yet, so its ring starts
 *  at the composer the operator sent from. */
export function openThreadSource(asking: boolean): Point {
  const name = openRow()?.dataset.ambientSource;
  if (name) return threadSource(name.slice("thread:".length), asking);
  const composer = document
    .querySelector(".heph-composer")
    ?.getBoundingClientRect();
  return composer && composer.width > 0 ? centre(composer) : railEdge();
}

/** The notification bell, for what arrives through it. */
export function bellSource(): Point {
  const bell = anchor("bell")?.getBoundingClientRect();
  return bell && bell.width > 0 ? centre(bell) : railEdge();
}
