import { config } from "./config.js";
import { getBearerToken } from "./auth.js";

/**
 * Foundry hosted-agent session manager.
 *
 * Mirrors the behavior of `AIProjectClient.beta.agents.create_session` /
 * `delete_session` from the azure-ai-projects Python SDK.
 *
 * Two caches:
 * 1. `latestVersion` per agent (TTL ~5 min) — resolved via GET /agents/{name}.
 * 2. `agentSessionId` per `conversationId` — created via POST /endpoint/sessions.
 */

const VERSION_TTL_MS = 5 * 60 * 1000;

interface VersionCacheEntry {
  version: string;
  fetchedAt: number;
}

interface SessionCacheEntry {
  agentSessionId: string;
  isolationKey: string;
  agentVersion: string;
  createdAt: number;
}

let versionCache: VersionCacheEntry | null = null;
const sessionCache = new Map<string, SessionCacheEntry>();
// Foundry Responses `conversation` (conv_...) per browser conversation id. This
// is DISTINCT from the hosted-agent session: the session gives compute/scene
// affinity, while the conversation gives platform-managed history AND is what
// the Foundry portal groups traces by (gen_ai.conversation.id). Sending only a
// session leaves gen_ai.conversation.id empty, so webchat turns never show up
// as a conversation in the portal / Monitor tab.
const conversationCache = new Map<string, string>();

function buildHeaders(token: string, contentType?: string): Record<string, string> {
  const h: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    "Foundry-Features": config.foundryFeaturesHeader,
    Accept: "application/json",
  };
  if (contentType) h["Content-Type"] = contentType;
  return h;
}

function withApiVersion(url: string): string {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}api-version=${encodeURIComponent(config.apiVersion)}`;
}

async function ensureToken(): Promise<string> {
  const token = await getBearerToken();
  if (!token) {
    throw new Error(
      "Failed to acquire Azure credential. Run 'az login' or check managed identity.",
    );
  }
  return token;
}

async function fetchLatestVersion(): Promise<string> {
  const now = Date.now();
  if (versionCache && now - versionCache.fetchedAt < VERSION_TTL_MS) {
    return versionCache.version;
  }

  const token = await ensureToken();
  const url = withApiVersion(config.foundryAgentBase);

  const res = await fetch(url, {
    method: "GET",
    headers: buildHeaders(token),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `Failed to fetch agent ${config.agentName}: ${res.status} ${text.slice(0, 500)}`,
    );
  }

  const body = (await res.json()) as {
    versions?: Record<string, { version?: string }>;
  };
  const latest = body.versions?.latest?.version;
  if (!latest) {
    throw new Error(
      `Agent ${config.agentName} response did not include versions.latest.version`,
    );
  }

  versionCache = { version: latest, fetchedAt: now };
  return latest;
}

export async function getOrCreateSession(
  conversationId: string,
): Promise<{ agentSessionId: string; agentVersion: string }> {
  const cached = sessionCache.get(conversationId);
  if (cached) {
    console.log(
      `[sessions] cache hit: conversation=${conversationId} agent_session_id=${cached.agentSessionId}`,
    );
    return {
      agentSessionId: cached.agentSessionId,
      agentVersion: cached.agentVersion,
    };
  }

  const agentVersion = await fetchLatestVersion();
  const token = await ensureToken();

  const url = withApiVersion(`${config.foundryAgentBase}/endpoint/sessions`);
  const isolationKey = conversationId;

  const res = await fetch(url, {
    method: "POST",
    headers: {
      ...buildHeaders(token, "application/json"),
      "x-session-isolation-key": isolationKey,
    },
    body: JSON.stringify({
      version_indicator: {
        type: "version_ref",
        agent_version: agentVersion,
      },
    }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `Failed to create session: ${res.status} ${text.slice(0, 500)}`,
    );
  }

  const body = (await res.json()) as { agent_session_id?: string };
  const agentSessionId = body.agent_session_id;
  if (!agentSessionId) {
    throw new Error("Session create response missing agent_session_id");
  }

  sessionCache.set(conversationId, {
    agentSessionId,
    isolationKey,
    agentVersion,
    createdAt: Date.now(),
  });

  console.log(
    `[sessions] created agent_session_id=${agentSessionId} for conversation=${conversationId} (agentVersion=${agentVersion})`,
  );

  return { agentSessionId, agentVersion };
}

export function evictSession(conversationId: string): void {
  sessionCache.delete(conversationId);
}

/**
 * Get (or lazily create) the Foundry Responses `conversation` id (conv_...) for
 * a browser conversation. Both the text and voice paths call this with the same
 * browser conversation id, so they resolve to ONE Foundry conversation — which
 * is what makes turns appear (and group) in the portal traces / Monitor tab.
 */
export async function getOrCreateConversation(
  conversationId: string,
): Promise<string> {
  const cached = conversationCache.get(conversationId);
  if (cached) return cached;

  const token = await ensureToken();
  const url = withApiVersion(
    `${config.foundryAgentBase}/endpoint/protocols/openai/conversations`,
  );

  const res = await fetch(url, {
    method: "POST",
    headers: buildHeaders(token, "application/json"),
    body: JSON.stringify({ metadata: { webchat_conversation_id: conversationId } }),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `Failed to create conversation: ${res.status} ${text.slice(0, 500)}`,
    );
  }

  const body = (await res.json()) as { id?: string };
  const foundryConversationId = body.id;
  if (!foundryConversationId) {
    throw new Error("Conversation create response missing id");
  }

  conversationCache.set(conversationId, foundryConversationId);
  console.log(
    `[conversations] created ${foundryConversationId} for conversation=${conversationId}`,
  );
  return foundryConversationId;
}

export function evictConversation(conversationId: string): void {
  conversationCache.delete(conversationId);
}

export async function deleteSession(conversationId: string): Promise<void> {
  const entry = sessionCache.get(conversationId);
  if (!entry) return;

  sessionCache.delete(conversationId);

  const token = await ensureToken();
  const url = withApiVersion(
    `${config.foundryAgentBase}/endpoint/sessions/${encodeURIComponent(entry.agentSessionId)}`,
  );

  const res = await fetch(url, {
    method: "DELETE",
    headers: {
      ...buildHeaders(token),
      "x-session-isolation-key": entry.isolationKey,
    },
  });

  // 204 expected, 404 tolerated (already gone).
  if (!res.ok && res.status !== 404) {
    const text = await res.text().catch(() => "");
    console.error(
      `[sessions] Delete returned ${res.status}: ${text.slice(0, 500)}`,
    );
  }
}

export function getSessionInfo(conversationId: string): SessionCacheEntry | null {
  return sessionCache.get(conversationId) ?? null;
}
