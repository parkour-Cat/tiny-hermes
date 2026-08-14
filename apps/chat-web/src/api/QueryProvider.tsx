import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useState } from "react";

import { isSessionLost } from "./messages";
import { useAuth } from "../auth/AuthProvider";

/**
 * The query client, with the one rule that has to hold for every request.
 *
 * A lost session is not a failure of the thing being asked for, so no page
 * should have to recognize it: the console forgets the user, and the router
 * takes it from there. It lives under `AuthProvider` so it can say so.
 */
export function QueryProvider({ children }: { children: ReactNode }) {
  const auth = useAuth();
  const [client] = useState(() => {
    const forgetOnLostSession = (error: unknown): void => {
      if (isSessionLost(error)) {
        auth.forget();
      }
    };
    return new QueryClient({
      defaultOptions: { queries: { retry: false } },
      queryCache: new QueryCache({ onError: forgetOnLostSession }),
      mutationCache: new MutationCache({ onError: forgetOnLostSession }),
    });
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
