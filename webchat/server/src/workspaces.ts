import { createHash, randomUUID } from "node:crypto";
import { BlobServiceClient, BlobSASPermissions, SASProtocol, generateBlobSASQueryParameters, type UserDelegationKey } from "@azure/storage-blob";
import { DefaultAzureCredential } from "@azure/identity";
import type { Express, Request, Response } from "express";
import { config } from "./config.js";
import { browserKey, isUuid, JOB_ID_RE, mediaEnabled, readBrowserKey, requireOrigin } from "./mediaSecurity.js";

export interface WorkspaceBinding {
  agentSessionId?: string;
  agentVersion?: string;
  foundryConversationId?: string;
  expired?: boolean;
}
export interface WorkspaceResource {
  id: string;
  kind: "image" | "video" | "audio" | "blend" | "glb";
  name: string;
  blob: string;
  created_at: number;
  job_id?: string;
}
export interface WorkspaceTurn {
  id: string;
  user: string;
  assistant: string;
  status: "done" | "error";
  created_at: number;
  audio_blob?: string;
}
interface ArchiveJob {
  id: string;
  viewport_url?: string;
  poster_url?: string;
  preview_url?: string;
  output_url?: string;
  scene_url?: string;
}
export interface Workspace {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  binding: WorkspaceBinding;
  turns: WorkspaceTurn[];
  resources: WorkspaceResource[];
  job_ids: string[];
  previous_response_id: string | null;
  scene_expected?: boolean;
  recovery_note?: string;
}
export interface ArchiveStorage {
  read(name: string): Promise<{ value: unknown; etag: string } | null>;
  write(name: string, value: unknown, etag?: string): Promise<void>;
  url(blob: string): Promise<string>;
}
interface WorkspaceSummary {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  resource_count: number;
  thumbnail_blob?: string;
}

function statusCode(error: unknown): number | undefined {
  return error && typeof error === "object" && "statusCode" in error && typeof error.statusCode === "number"
    ? error.statusCode : undefined;
}

class BlobArchiveStorage implements ArchiveStorage {
  private service = new BlobServiceClient(`https://${config.storageAccountName}.blob.core.windows.net`, new DefaultAzureCredential());
  private container = this.service.getContainerClient("screenshots");
  private privateChecked = false;
  private delegation: UserDelegationKey | null = null;
  private delegationAt = 0;

  private async ensurePrivate(): Promise<void> {
    if (this.privateChecked) return;
    const properties = await this.container.getProperties();
    if (properties.blobPublicAccess) throw new Error("The workspace archive requires a private screenshots container.");
    this.privateChecked = true;
  }
  async read(name: string): Promise<{ value: unknown; etag: string } | null> {
    await this.ensurePrivate();
    const blob = this.container.getBlockBlobClient(name);
    try {
      const properties = await blob.getProperties();
      if (!properties.etag) throw new Error("Workspace archive is missing its version.");
      if ((properties.contentLength ?? 0) > 2 * 1024 * 1024) throw new Error("Workspace archive exceeds 2 MiB.");
      const data = await blob.downloadToBuffer();
      const value: unknown = JSON.parse(data.toString("utf8"));
      return { value, etag: properties.etag };
    } catch (error) {
      if (statusCode(error) === 404) return null;
      throw error;
    }
  }
  async write(name: string, value: unknown, etag?: string): Promise<void> {
    await this.ensurePrivate();
    const data = Buffer.from(JSON.stringify(value));
    if (data.length > 2 * 1024 * 1024) throw new Error("Workspace archive exceeds 2 MiB. Keep this work open and export it before starting another.");
    await this.container.getBlockBlobClient(name).uploadData(data, {
      conditions: etag ? { ifMatch: etag } : { ifNoneMatch: "*" },
      blobHTTPHeaders: { blobContentType: "application/json", blobCacheControl: "no-store" },
    });
  }
  async url(blob: string): Promise<string> {
    await this.ensurePrivate();
    if (!this.delegation || Date.now() - this.delegationAt > 10 * 60_000) {
      this.delegation = await this.service.getUserDelegationKey(new Date(Date.now() - 5 * 60_000), new Date(Date.now() + 48 * 3600_000));
      this.delegationAt = Date.now();
    }
    const sas = generateBlobSASQueryParameters({
      containerName: "screenshots", blobName: blob, permissions: BlobSASPermissions.parse("r"),
      protocol: SASProtocol.Https, startsOn: new Date(Date.now() - 5 * 60_000), expiresOn: new Date(Date.now() + 24 * 3600_000),
    }, this.delegation, config.storageAccountName).toString();
    return `https://${config.storageAccountName}.blob.core.windows.net/screenshots/${blob.split("/").map(encodeURIComponent).join("/")}?${sas}`;
  }
}

export function galleryEnabled(): boolean {
  return mediaEnabled(config.mediaControlSecret) && /^[a-z0-9]{3,24}$/.test(config.storageAccountName);
}
export function workspaceOwner(key: string): string {
  return createHash("sha256").update(`${key}:${config.mode}:${config.agentUrl}:${config.agentName}`).digest("hex");
}
export function ownerFromCookie(cookie: string | undefined): string | undefined {
  if (!galleryEnabled()) return undefined;
  const key = readBrowserKey(cookie, config.mediaControlSecret);
  return key ? workspaceOwner(key) : undefined;
}
function requestOwner(req: Request, res: Response): string {
  return workspaceOwner(browserKey(req, res, config.mediaControlSecret, new URL(config.clientOrigin).protocol === "https:"));
}

function recordPath(owner: string, id: string): string {
  if (!/^[a-f0-9]{64}$/.test(owner) || !isUuid(id)) throw new Error("Invalid workspace identity.");
  return `webchat-workspaces/${owner}/${id.toLowerCase()}.json`;
}
function validWorkspace(value: unknown, id: string): value is Workspace {
  if (!value || typeof value !== "object") return false;
  return "id" in value && value.id === id && "title" in value && typeof value.title === "string"
    && "created_at" in value && typeof value.created_at === "number"
    && "updated_at" in value && typeof value.updated_at === "number"
    && "turns" in value && Array.isArray(value.turns) && value.turns.every(validTurn)
    && "resources" in value && Array.isArray(value.resources) && value.resources.every(validResource)
    && "job_ids" in value && Array.isArray(value.job_ids) && value.job_ids.every((id) => typeof id === "string" && JOB_ID_RE.test(id))
    && "binding" in value && !!value.binding && typeof value.binding === "object"
    && "previous_response_id" in value && (value.previous_response_id === null || typeof value.previous_response_id === "string");
}
function validTurn(value: unknown): value is WorkspaceTurn {
  return !!value && typeof value === "object" && "id" in value && typeof value.id === "string"
    && "user" in value && typeof value.user === "string" && "assistant" in value && typeof value.assistant === "string"
    && "status" in value && (value.status === "done" || value.status === "error")
    && "created_at" in value && typeof value.created_at === "number" && Number.isFinite(value.created_at);
}
function validResource(value: unknown): value is WorkspaceResource {
  return !!value && typeof value === "object" && "id" in value && typeof value.id === "string"
    && "kind" in value && ["image", "video", "audio", "blend", "glb"].includes(String(value.kind))
    && "name" in value && typeof value.name === "string" && "blob" in value && typeof value.blob === "string"
    && /^[A-Za-z0-9_./-]{1,512}$/.test(value.blob) && !value.blob.split("/").some((part) => ["", ".", ".."].includes(part))
    && "created_at" in value && typeof value.created_at === "number" && Number.isFinite(value.created_at);
}

export class WorkspaceArchive {
  constructor(private storage: ArchiveStorage) {}

  async claim(owner: string, id: string): Promise<void> {
    recordPath(owner, id);
    const target = createHash("sha256").update(`${config.mode}:${config.agentUrl}:${config.agentName}`).digest("hex").slice(0, 32);
    const path = `webchat-workspaces/owners/${target}/${id}.json`;
    const existing = await this.storage.read(path);
    if (existing) {
      if (existing.value !== owner) throw new Error("This workspace is not available for this browser.");
      return;
    }
    try { await this.storage.write(path, owner); }
    catch (error) {
      if (![409, 412].includes(statusCode(error) ?? 0)) throw error;
      if ((await this.storage.read(path))?.value !== owner) throw new Error("This workspace is not available for this browser.");
    }
  }

  async get(owner: string, id: string): Promise<Workspace | null> {
    const stored = await this.storage.read(recordPath(owner, id));
    if (!stored) return null;
    if (!validWorkspace(stored.value, id)) throw new Error("Invalid stored workspace.");
    return stored.value;
  }
  async update(owner: string, id: string, change: (workspace: Workspace) => void): Promise<Workspace> {
    for (let attempt = 0; attempt < 4; attempt++) {
      const path = recordPath(owner, id);
      const stored = await this.storage.read(path);
      if (stored && !validWorkspace(stored.value, id)) throw new Error("Invalid stored workspace.");
      const workspace: Workspace = stored && validWorkspace(stored.value, id) ? stored.value : {
        id, title: "Nouvelle scène", created_at: Date.now(), updated_at: Date.now(),
        binding: {}, turns: [], resources: [], job_ids: [], previous_response_id: null,
      };
      change(workspace);
      workspace.updated_at = Date.now();
      try {
        await this.storage.write(path, workspace, stored?.etag);
        await this.index(owner, workspace);
        return workspace;
      } catch (error) {
        if (![409, 412].includes(statusCode(error) ?? 0) || attempt === 3) throw error;
      }
    }
    throw new Error("Workspace changed concurrently. Refresh the gallery.");
  }
  private async index(owner: string, workspace: Workspace): Promise<void> {
    const path = `webchat-workspaces/${owner}/index.json`;
    const images = workspace.resources.filter((resource) => resource.kind === "image");
    const rendered = images.filter((resource) => /render|poster/i.test(resource.blob));
    const thumbnail = rendered.at(-1) ?? images.at(-1);
    const summary: WorkspaceSummary = {
      id: workspace.id, title: workspace.title, created_at: workspace.created_at, updated_at: workspace.updated_at,
      resource_count: workspace.resources.length, ...(thumbnail ? { thumbnail_blob: thumbnail.blob } : {}),
    };
    for (let attempt = 0; attempt < 4; attempt++) {
      const stored = await this.storage.read(path);
      if (stored && !Array.isArray(stored.value)) throw new Error("Invalid workspace index.");
      const summaries: WorkspaceSummary[] = Array.isArray(stored?.value) ? stored.value : [];
      const next = [summary, ...summaries.filter((entry) => entry.id !== workspace.id)]
        .sort((a, b) => b.updated_at - a.updated_at);
      try { await this.storage.write(path, next, stored?.etag); return; }
      catch (error) { if (![409, 412].includes(statusCode(error) ?? 0) || attempt === 3) throw error; }
    }
  }
  async list(owner: string) {
    const stored = await this.storage.read(`webchat-workspaces/${owner}/index.json`);
    if (stored && !Array.isArray(stored.value)) throw new Error("Invalid workspace index.");
    const entries: WorkspaceSummary[] = Array.isArray(stored?.value) ? stored.value : [];
    return Promise.all(entries.map(async (entry) => ({
      id: entry.id, title: entry.title, created_at: entry.created_at, updated_at: entry.updated_at,
      resource_count: entry.resource_count,
      ...(entry.thumbnail_blob ? { thumbnail_url: await this.storage.url(entry.thumbnail_blob) } : {}),
    })));
  }
  async detail(owner: string, id: string) {
    const workspace = await this.get(owner, id);
    if (!workspace) throw new Error("This workspace is not available for this browser.");
    const urls = new Map<string, string>();
    for (const resource of workspace.resources) urls.set(resource.blob, await this.storage.url(resource.blob));
    const refreshText = (text: string) => text.replace(/https?:\/\/[^\s<>)"'`]+/g, (url) => {
      const blob = archiveBlobName(url);
      return blob && urls.has(blob) ? urls.get(blob)! : url;
    });
    return {
      ...workspace,
      resource_count: workspace.resources.length,
      turns: workspace.turns.map((turn) => ({
        ...turn, user: refreshText(turn.user), assistant: refreshText(turn.assistant),
        ...(turn.audio_blob && urls.has(turn.audio_blob) ? { audio_url: urls.get(turn.audio_blob) } : {}),
      })),
      resources: workspace.resources.map((resource) => ({ ...resource, url: urls.get(resource.blob) })),
    };
  }
  async turn(owner: string, id: string, turn: WorkspaceTurn, replyId: string | null, sceneExpected = false): Promise<void> {
    await this.update(owner, id, (workspace) => {
      if (!workspace.turns.some((entry) => entry.id === turn.id)) workspace.turns.push(turn);
      workspace.previous_response_id = replyId;
      if (sceneExpected) workspace.scene_expected = true;
      if (workspace.title === "Nouvelle scène" && turn.user.trim()) workspace.title = turn.user.trim().slice(0, 90);
      collectResources(workspace, turn.assistant, turn.created_at);
      const movie = turn.user.startsWith("Extrait musical") ? /\*\*([^*\n]+)\*\*/.exec(turn.assistant)?.[1] : null;
      if (movie && workspace.title.startsWith("Extrait musical")) workspace.title = movie.slice(0, 90);
      if (turn.audio_blob) addResource(workspace, turn.audio_blob, turn.created_at);
    });
  }
  async job(owner: string, id: string, job: ArchiveJob): Promise<void> {
    await this.update(owner, id, (workspace) => {
      if (!workspace.job_ids.includes(job.id)) workspace.job_ids.push(job.id);
      for (const field of ["viewport_url", "poster_url", "preview_url", "output_url", "scene_url"] as const) {
        const blob = archiveBlobName(job[field]);
        if (blob) addResource(workspace, blob, Date.now(), job.id);
      }
    });
  }
  async audio(owner: string, id: string, url: string): Promise<void> {
    const blob = archiveBlobName(url);
    if (!blob) throw new Error("The musical audio does not belong to the configured artifact store.");
    await this.update(owner, id, (workspace) => addResource(workspace, blob, Date.now()));
  }
}

function archiveBlobName(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.hostname !== `${config.storageAccountName}.blob.core.windows.net`) return undefined;
    const path = decodeURIComponent(url.pathname);
    if (!path.startsWith("/screenshots/")) return undefined;
    const blob = path.slice("/screenshots/".length);
    return /^[A-Za-z0-9_./-]{1,512}$/.test(blob) && !blob.split("/").some((part) => ["", ".", ".."].includes(part)) ? blob : undefined;
  } catch { return undefined; }
}
function addResource(workspace: Workspace, blob: string, createdAt: number, jobId?: string): void {
  const extension = blob.split(".").at(-1)?.toLowerCase();
  const kind = ["png", "jpg", "jpeg", "webp"].includes(extension ?? "") ? "image"
    : extension === "mp4" ? "video" : ["wav", "mp3", "ogg"].includes(extension ?? "") ? "audio"
      : extension === "blend" ? "blend" : extension === "glb" ? "glb" : null;
  if (!kind) return;
  const existing = workspace.resources.find((entry) => entry.blob === blob);
  if (existing) { if (jobId) existing.job_id = jobId; return; }
  workspace.resources.push({ id: createHash("sha256").update(blob).digest("hex").slice(0, 32), kind,
    name: blob.split("/").at(-1) ?? blob, blob, created_at: createdAt, ...(jobId ? { job_id: jobId } : {}) });
}
function collectResources(workspace: Workspace, text: string, createdAt: number): void {
  for (const match of text.matchAll(/https?:\/\/[^\s<>)"'`]+/g)) {
    const blob = archiveBlobName(match[0]);
    if (blob) addResource(workspace, blob, createdAt);
  }
  for (const match of text.matchAll(/```videojob\s*\n([\s\S]*?)```/gi)) {
    const value: unknown = JSON.parse(match[1]);
    if (value && typeof value === "object" && "id" in value && typeof value.id === "string" && JOB_ID_RE.test(value.id)
      && !workspace.job_ids.includes(value.id)) workspace.job_ids.push(value.id);
  }
}

let archive: WorkspaceArchive | undefined;
const owners = new Map<string, string>();
export function getWorkspaceArchive(): WorkspaceArchive {
  if (!galleryEnabled()) throw new Error("Gallery requires configured private storage and MEDIA_CONTROL_SECRET.");
  return archive ??= new WorkspaceArchive(new BlobArchiveStorage());
}
export async function registerWorkspace(owner: string | undefined, id: string): Promise<void> {
  if (!owner) return;
  if (owners.has(id) && owners.get(id) !== owner) throw new Error("This workspace belongs to another browser.");
  await getWorkspaceArchive().claim(owner, id);
  await getWorkspaceArchive().update(owner, id, () => {});
  owners.set(id, owner);
}
export function workspaceRegistered(id: string): boolean { return owners.has(id); }
export async function workspaceHistory(id: string): Promise<Array<{ role: "user" | "assistant"; content: string }>> {
  const owner = owners.get(id);
  const workspace = owner ? await getWorkspaceArchive().get(owner, id) : null;
  return workspace?.turns.filter((turn) => turn.status === "done").flatMap((turn) => [
    { role: "user" as const, content: turn.user }, { role: "assistant" as const, content: turn.assistant },
  ]).filter((item) => item.content.trim()).slice(-80) ?? [];
}
export async function workspaceSceneExpected(id: string): Promise<boolean> {
  const owner = owners.get(id);
  return !!(owner && (await getWorkspaceArchive().get(owner, id))?.scene_expected);
}
export async function storedBinding(id: string): Promise<WorkspaceBinding | undefined> {
  const owner = owners.get(id);
  return owner ? (await getWorkspaceArchive().get(owner, id))?.binding : undefined;
}
export async function persistBinding(id: string, changes: WorkspaceBinding): Promise<void> {
  const owner = owners.get(id);
  if (!owner) return;
  const previous = await storedBinding(id);
  if (previous && Object.entries(changes).every(([key, value]) => previous[key as keyof WorkspaceBinding] === value)) return;
  await getWorkspaceArchive().update(owner, id, (workspace) => { Object.assign(workspace.binding, changes); });
}
export async function forgetConversationBinding(id: string): Promise<void> {
  const owner = owners.get(id);
  if (owner) await getWorkspaceArchive().update(owner, id, (workspace) => { delete workspace.binding.foundryConversationId; workspace.previous_response_id = null; });
}
export async function recoverWorkspaceHistory(id: string, messages: Array<{ role: "user" | "assistant"; content: string }>): Promise<void> {
  const owner = owners.get(id);
  if (!owner || !messages.length) return;
  await getWorkspaceArchive().update(owner, id, (workspace) => {
    if (workspace.turns.length) return;
    let user = "";
    let assistant = "";
    const save = () => {
      if (!user && !assistant) return;
      const createdAt = workspace.created_at;
      workspace.turns.push({ id: randomUUID(), user, assistant, status: assistant ? "done" : "error", created_at: createdAt });
      collectResources(workspace, assistant, createdAt);
      if (workspace.title === "Nouvelle scène") workspace.title = user.slice(0, 90) || "Ancienne réalisation";
    };
    for (const message of messages) {
      if (message.role === "user") {
        save(); user = message.content; assistant = "";
        if (user.startsWith("BLENDER_MEDIA_V1:")) {
          const part = user.slice("BLENDER_MEDIA_V1:".length).split(".")[0];
          const decoded = JSON.parse(Buffer.from(part, "base64url").toString());
          if (typeof decoded.text === "string") user = decoded.text;
        }
      } else assistant += `${assistant ? "\n\n" : ""}${message.content}`;
    }
    save();
    workspace.scene_expected = true;
  });
}
export async function workspaceRecoveryWarning(id: string, message: string): Promise<void> {
  const owner = owners.get(id);
  if (owner) await getWorkspaceArchive().update(owner, id, (workspace) => { workspace.recovery_note = message; });
}
export async function archiveJob(id: string, job: ArchiveJob): Promise<void> {
  const owner = owners.get(id);
  if (owner) await getWorkspaceArchive().job(owner, id, job);
}
export async function archiveInputAudio(id: string, url: string): Promise<void> {
  const owner = owners.get(id);
  if (owner) await getWorkspaceArchive().audio(owner, id, url);
}
export async function archiveTurn(id: string, user: string, assistant: string, responseId: string | null, status: "done" | "error" = "done", audioUrl?: string, turnId = randomUUID(), sceneExpected = false): Promise<void> {
  const owner = owners.get(id);
  if (!owner) return;
  const audio = archiveBlobName(audioUrl);
  await getWorkspaceArchive().turn(owner, id, {
    id: turnId, user, assistant, status, created_at: Date.now(), ...(audio ? { audio_blob: audio } : {}),
  }, responseId, sceneExpected);
}

export class TurnArchiveCollector {
  private decoder = new TextDecoder();
  private buffer = "";
  reply = "";
  responseId: string | null = null;
  failed = false;
  feed(bytes: Uint8Array): void {
    this.buffer += this.decoder.decode(bytes, { stream: true });
    let separator: RegExpExecArray | null;
    while ((separator = /\r?\n\r?\n/.exec(this.buffer))) {
      const frame = this.buffer.slice(0, separator.index);
      this.buffer = this.buffer.slice(separator.index + separator[0].length);
      const data = frame.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
      if (!data || data === "[DONE]") continue;
      const value = JSON.parse(data);
      const event = frame.split(/\r?\n/).find((line) => line.startsWith("event:"))?.slice(6).trim() || value.type;
      if (event === "response.output_text.delta" && typeof value.delta === "string") this.reply += value.delta;
      if (event === "response.completed") this.responseId = typeof value.response?.id === "string" ? value.response.id : null;
      if (event === "error" || event === "response.failed" || event === "response.incomplete") this.failed = true;
    }
  }
}

export function registerWorkspaceRoutes(app: Express, onRegister?: (id: string) => Promise<void>): void {
  app.use("/api/workspaces", (_req, res, next) => {
    res.setHeader("Cache-Control", "no-store");
    if (!galleryEnabled()) { res.status(503).json({ error: "Galerie indisponible : configurez le stockage privé et MEDIA_CONTROL_SECRET." }); return; }
    next();
  });
  app.get("/api/workspaces", async (req, res) => {
    try { res.json(await getWorkspaceArchive().list(requestOwner(req, res))); }
    catch (error) { console.error("[workspaces] list failed", error); res.status(502).json({ error: error instanceof Error ? error.message : "Gallery failed" }); }
  });
  app.post("/api/workspaces", requireOrigin(config.clientOrigin), async (req, res) => {
    try {
      const supplied = req.body?.conversation_id ?? randomUUID();
      if (!isUuid(supplied)) throw new Error("Invalid conversation ID.");
      const id = supplied.toLowerCase();
      const owner = requestOwner(req, res);
      await registerWorkspace(owner, id);
      if (onRegister) await onRegister(id);
      res.status(201).json(await getWorkspaceArchive().detail(owner, id));
    } catch (error) { console.error("[workspaces] create failed", error); res.status(400).json({ error: error instanceof Error ? error.message : "Workspace creation failed" }); }
  });
  app.get("/api/workspaces/:id", async (req, res) => {
    try {
      const id = String(req.params.id).toLowerCase();
      if (!isUuid(id)) throw new Error("Invalid conversation ID.");
      const owner = requestOwner(req, res);
      const result = await getWorkspaceArchive().detail(owner, id);
      owners.set(id, owner);
      res.json(result);
    } catch (error) { console.error("[workspaces] read failed", error); res.status(404).json({ error: error instanceof Error ? error.message : "Workspace unavailable" }); }
  });
}
