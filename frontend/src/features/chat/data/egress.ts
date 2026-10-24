import { api } from "~/lib/api";

/** Let the thread's machine reach `domain`; resolves to every domain it may now reach. */
export async function allowDomain(
  conversationId: string,
  domain: string,
): Promise<string[]> {
  const res = await api.post<{ allowed: string[] }>(
    `/operator/conversations/${conversationId}/egress`,
    { domain },
  );
  return res.allowed;
}
