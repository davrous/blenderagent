import { DefaultAzureCredential } from "@azure/identity";
import { config } from "./config.js";

let credential: DefaultAzureCredential | null = null;
let cached: { token: string; expiresOnTimestamp: number } | null = null;

// Refresh token 5 minutes before expiry
const REFRESH_BUFFER_MS = 5 * 60 * 1000;

export async function getBearerToken(): Promise<string | null> {
  if (config.mode !== "foundry") {
    return null;
  }

  const now = Date.now();
  if (cached && cached.expiresOnTimestamp - now > REFRESH_BUFFER_MS) {
    return cached.token;
  }

  if (!credential) {
    credential = new DefaultAzureCredential();
  }

  const result = await credential.getToken(config.tokenScope);
  if (!result) {
    throw new Error(
      `Failed to acquire token for scope ${config.tokenScope}. Run 'az login' or check your managed identity.`,
    );
  }

  cached = {
    token: result.token,
    expiresOnTimestamp: result.expiresOnTimestamp,
  };
  return result.token;
}
