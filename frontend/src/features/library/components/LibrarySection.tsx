import { createSignal, Show, type JSX } from "solid-js";

interface Open {
  id: string;
  editing: boolean;
}

/** A settings section two levels deep: the directory, and an item's read view
 *  (which edits in place). The open item lives here — the one piece of state no
 *  screen should own, since the directory must not know a detail exists and the
 *  detail must not know what it came from. It is deliberately not in the URL:
 *  `?settings=` addresses a category, and a specific item is a place you get
 *  to, not one you send someone. */
export function LibrarySection(props: {
  directory: (nav: {
    view: (id: string) => void;
    edit: (id: string) => void;
  }) => JSX.Element;
  detail: (
    id: string,
    nav: { back: () => void; editing: boolean },
  ) => JSX.Element;
}): JSX.Element {
  const [open, setOpen] = createSignal<Open | null>(null);
  return (
    <Show
      when={open()}
      keyed
      fallback={props.directory({
        view: (id) => setOpen({ id, editing: false }),
        edit: (id) => setOpen({ id, editing: true }),
      })}
    >
      {(o) =>
        props.detail(o.id, { back: () => setOpen(null), editing: o.editing })
      }
    </Show>
  );
}
