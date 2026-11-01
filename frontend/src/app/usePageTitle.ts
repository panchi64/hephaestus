import { createEffect } from "solid-js";
import { useLocation } from "@solidjs/router";
import { itemForPath } from "./nav";

const BRAND = "Hephaestus";

/** Routes that aren't in the sidebar nav. */
const STATIC_TITLES: Record<string, string> = {
  "/": "Overview",
  "/login": "Sign in",
  "/signup": "Sign up",
  "/bench": "Bench",
  "/dev/onboarding": "Onboarding bench",
};

function titleFor(pathname: string): string {
  if (STATIC_TITLES[pathname]) return STATIC_TITLES[pathname];
  // The same longest-prefix resolution the rail uses, so a detail route
  // (`/rag/src-007`) is titled by the surface it belongs to and the title can
  // never disagree with the highlighted row.
  return itemForPath(pathname)?.label ?? "Not Found";
}

/**
 * Keeps `document.title` in sync with the current route — centrally, from the
 * nav model — so no screen has to set its own title. Call once at the app root.
 */
export function usePageTitle(): void {
  const location = useLocation();
  createEffect(() => {
    document.title = `${titleFor(location.pathname)} · ${BRAND}`;
  });
}
