/**
 * Which Agent this device opens first, among the ones the credential allows.
 *
 * Only this device: design §4.5.1 keeps nothing about an end user on the
 * platform beyond what a Run needs, so a preference like this has nowhere
 * else to live. And only among the allowed: a remembered alias the current
 * credential does not name is ignored, not honoured — the enterprise's
 * roster wins over a browser's memory.
 */
const KEY = "tiny-hermes-chat-default-agent";

export function loadDefaultAgent(): string | null {
  try {
    const stored = window.localStorage.getItem(KEY);
    return stored === null || stored === "" ? null : stored;
  } catch {
    return null;
  }
}

export function saveDefaultAgent(alias: string): void {
  try {
    window.localStorage.setItem(KEY, alias);
  } catch {
    // A blocked store only means the choice does not outlive this tab.
  }
}

export function chooseDefaultAgent<T extends { alias: string }>(
  agents: T[],
  preferred: string | null,
): T | undefined {
  if (preferred !== null) {
    const match = agents.find((agent) => agent.alias === preferred);
    if (match !== undefined) {
      return match;
    }
  }
  return agents[0];
}
