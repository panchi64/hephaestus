import { Router } from "@solidjs/router";
import { FileRoutes } from "@solidjs/start/router";
import { Suspense } from "solid-js";
import { ThemeProvider, Toaster, ConfirmHost, DitherField } from "~/ui";
import { usePageTitle } from "~/app/usePageTitle";
import { useFavicon } from "~/app/useFavicon";
import { AppSplash } from "~/app/AppSplash";
import { useSession } from "~/lib/stores/session";
import { ambientLoad, ambientPulses } from "~/lib/stores/ambient";
import "./app.css";

/**
 * Root: theme bootstrap + per-route document title + Suspense + the ambient field
 * behind every page (§11.1). Per-section chrome (app shell vs bare auth) is applied
 * by the route-group layout files in src/routes, not here.
 *
 * The root Suspense has a fallback because it is the boundary the rail's own cold
 * reads land on — the project switcher and the thread list sit outside AppShell's
 * inner boundary. Without one, the first paint after unlock is a blank frame.
 */
export default function App() {
  return (
    <Router
      root={(props) => {
        usePageTitle();
        useFavicon();
        const session = useSession();
        return (
          <ThemeProvider>
            <DitherField load={ambientLoad} pulses={ambientPulses} />
            <Suspense
              fallback={
                // A cold load before unlock keeps boot's label: nothing is waking yet.
                <AppSplash
                  label={session.isAuthenticated ? "Waking Talos…" : undefined}
                />
              }
            >
              {props.children}
            </Suspense>
            <Toaster />
            <ConfirmHost />
          </ThemeProvider>
        );
      }}
    >
      <FileRoutes />
    </Router>
  );
}
