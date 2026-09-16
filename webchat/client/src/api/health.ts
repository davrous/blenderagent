export interface Health {
  mode: string;
  agentUrl: string;
  model: string;
  voiceEnabled?: boolean;
  mediaEnabled?: boolean;
  mediaDisabledReason?: string;
}

export function loadHealth(onHealth: (health: Health) => void, onUnavailable: () => void): () => void {
  let stopped = false;
  let retryDelay = 1000;
  let retryTimer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController;

  async function check() {
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/health", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`Health request failed (${response.status})`);
      const health = await response.json();
      if (!health || typeof health.mode !== "string" || typeof health.agentUrl !== "string" || typeof health.model !== "string"
        || (health.mediaEnabled !== undefined && typeof health.mediaEnabled !== "boolean")
        || (health.voiceEnabled !== undefined && typeof health.voiceEnabled !== "boolean")) {
        throw new Error("Invalid health response");
      }
      if (!stopped) onHealth(health);
    } catch {
      if (!stopped) {
        onUnavailable();
        retryTimer = setTimeout(() => void check(), retryDelay);
        retryDelay = Math.min(retryDelay * 2, 5000);
      }
    } finally {
      clearTimeout(timeout);
    }
  }

  void check();
  return () => {
    stopped = true;
    clearTimeout(retryTimer);
    controller.abort();
  };
}