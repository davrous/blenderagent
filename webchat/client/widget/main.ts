import { object, reportHeight, WidgetBridge } from "./bridge";
import { fetchGlb, MAX_GLB_BYTES, SceneViewer } from "./viewer";
import "./style.css";

const root = document.getElementById("root");
const canvas = document.getElementById("scene");
const load = document.getElementById("load");
const reset = document.getElementById("reset");
const ping = document.getElementById("ping");
const download = document.getElementById("download");
const status = document.getElementById("status");
const error = document.getElementById("error");
const placeholder = document.getElementById("placeholder");
if (!(root instanceof HTMLElement) || !(canvas instanceof HTMLCanvasElement)
  || !(load instanceof HTMLButtonElement) || !(reset instanceof HTMLButtonElement)
  || !(ping instanceof HTMLButtonElement) || !(download instanceof HTMLAnchorElement)
  || !status || !error || !placeholder) {
  throw new Error("The scene widget document is incomplete.");
}

const bridge = new WidgetBridge();
let viewer: SceneViewer | undefined;
let controller: AbortController | undefined;
let stopHeight: (() => void) | undefined;
let disposed = false;
let busy = false;

function showError(message: string) {
  error!.textContent = message;
  error!.hidden = false;
  status!.textContent = "The scene viewer needs attention.";
}

function theme(context: Record<string, unknown> | undefined) {
  const value = context?.theme;
  if (value === "dark" || value === "light" || value === "high-contrast") root!.dataset.theme = value;
}
bridge.onContext = theme;

async function connect() {
  const result = await bridge.connect();
  theme(object(result.hostContext));
  stopHeight ??= reportHeight(root!, bridge);
}

async function boot() {
  try {
    await connect();
    if (!disposed) status!.textContent = "Ready. Load the snapshot to explore it.";
  } catch {
    if (!disposed) showError("Could not connect to Teams. Use Check connection or Load scene to retry.");
  }
}

function config() {
  const value: unknown = JSON.parse(document.getElementById("scene-widget-config")?.textContent ?? "");
  const data = object(value);
  if (!data || typeof data.id !== "string" || !/^[a-f0-9]{32}$/.test(data.id)
    || typeof data.storageOrigin !== "string" || typeof data.environmentUrl !== "string") {
    throw new Error("The widget configuration is invalid. Ask the agent for a new export.");
  }
  return { id: data.id, storageOrigin: data.storageOrigin, environmentUrl: data.environmentUrl };
}

load.addEventListener("click", async () => {
  if (busy || disposed) return;
  busy = true;
  load.disabled = ping.disabled = true;
  reset.disabled = true;
  error.hidden = true;
  download.hidden = true;
  download.removeAttribute("href");
  placeholder.hidden = false;
  status.textContent = "Authorizing and downloading the snapshot...";
  controller?.abort();
  controller = new AbortController();
  viewer?.dispose();
  viewer = undefined;
  const timeout = setTimeout(() => controller?.abort(), 90000);
  try {
    const settings = config();
    await connect();
    const data = await bridge.call("load_scene_snapshot", settings.id);
    if (disposed) return;
    if (typeof data.url !== "string" || typeof data.size !== "number" || !Number.isInteger(data.size)
      || data.size <= 0 || data.size > MAX_GLB_BYTES) throw new Error("The snapshot metadata is invalid or too large.");
    const url = new URL(data.url);
    if (url.protocol !== "https:" || url.origin !== settings.storageOrigin || url.username || url.password
      || url.hash || !/^\/screenshots\/scenes\/[A-Za-z0-9_/-]+\.glb$/.test(url.pathname)) {
      throw new Error("The agent returned an invalid snapshot address.");
    }
    // Keep an authorized download available even if WebGL or lighting fails.
    download.href = url.href;
    download.hidden = false;
    const bytes = await fetchGlb(url.href, data.size, controller.signal);
    if (disposed) return;
    status.textContent = "Preparing 3D geometry and lighting...";
    viewer = new SceneViewer(canvas, () => {
      if (!disposed) {
        reset.disabled = true;
        showError("The graphics context was lost. Load scene again to recreate the viewer, or download the GLB.");
      }
    });
    await viewer.load(bytes, settings.environmentUrl, controller.signal);
    if (disposed) return;
    placeholder.hidden = true;
    reset.disabled = false;
    load.textContent = "Reload scene";
    status.textContent = "Scene ready. This is the exported snapshot, not a live Blender viewport.";
  } catch (reason) {
    viewer?.dispose();
    viewer = undefined;
    if (!disposed) {
      const message = reason instanceof Error ? reason.message.slice(0, 300) : "The snapshot could not be loaded.";
      showError(`${message} You can retry loading or use the conversation's download link.`);
    }
  } finally {
    clearTimeout(timeout);
    busy = false;
    if (!disposed) load.disabled = ping.disabled = false;
  }
});

reset.addEventListener("click", () => { viewer?.reset(); canvas.focus(); });
ping.addEventListener("click", async () => {
  if (busy || disposed) return;
  ping.disabled = true;
  error.hidden = true;
  try {
    await connect();
    await bridge.call("scene_widget_ping", config().id);
    if (!disposed) status.textContent = "Teams and the agent are connected. Load scene when ready.";
  } catch (reason) {
    if (!disposed) showError(reason instanceof Error ? reason.message.slice(0, 300) : "The connection check failed.");
  } finally {
    if (!disposed) ping.disabled = false;
  }
});

window.addEventListener("pagehide", () => {
  disposed = true;
  controller?.abort();
  stopHeight?.();
  bridge.dispose();
  viewer?.dispose();
}, { once: true });

void boot();
