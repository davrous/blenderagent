import dotenv from "dotenv";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Load .env from webchat/ root (one level above server/)
dotenv.config({ path: path.resolve(__dirname, "../../.env") });

export type AgentMode = "local" | "foundry";

function required(name: string, value: string | undefined): string {
  if (!value || value.trim() === "") {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

const mode = (process.env.AGENT_MODE ?? "local").toLowerCase() as AgentMode;
if (mode !== "local" && mode !== "foundry") {
  throw new Error(`AGENT_MODE must be "local" or "foundry", got: ${mode}`);
}

const agentUrl =
  mode === "local"
    ? (process.env.AGENT_LOCAL_URL ?? "http://localhost:8088")
    : required("AGENT_FOUNDRY_URL", process.env.AGENT_FOUNDRY_URL);

export const config = {
  mode,
  agentUrl: agentUrl.replace(/\/+$/, ""),
  tokenScope: process.env.AGENT_TOKEN_SCOPE ?? "https://ai.azure.com/.default",
  modelName: process.env.MODEL_NAME ?? "BlenderSceneAgent",
  port: Number(process.env.PORT ?? 5174),
  // Vite dev server origin allowed for CORS
  clientOrigin: process.env.CLIENT_ORIGIN ?? "http://localhost:5173",
  // Hostname suffixes allowed for /api/blob proxy. Comma-separated.
  blobProxyAllowedHostSuffixes: (
    process.env.BLOB_PROXY_ALLOWED_HOSTS ?? ".blob.core.windows.net"
  )
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean),
};
