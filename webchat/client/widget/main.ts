import { object, reportHeight, WidgetBridge } from "./bridge";
import { fetchGlb, MAX_GLB_BYTES, SceneViewer } from "./viewer";
import template from "./widget.html?raw";
import "./style.css";

const root = document.getElementById("root");
const content = new DOMParser().parseFromString(template, "text/html").getElementById("root");
if (!(root instanceof HTMLElement) || !content) throw new Error("The scene widget document is incomplete.");
// Previously sent Teams messages retain their original HTML bootstrap.
root.replaceChildren(...Array.from(content.childNodes));
root.setAttribute("aria-label", "3D viewer");
document.title = "3D viewer";
const canvas = document.getElementById("scene");
const load = document.getElementById("load");
const reset = document.getElementById("reset");
const ping = document.getElementById("ping");
const fullscreen = document.getElementById("fullscreen");
const inspector = document.getElementById("inspector");
const inspectorHost = document.getElementById("viewer-inspector");
const status = document.getElementById("status");
const error = document.getElementById("error");
const placeholder = document.getElementById("placeholder");
if (!(root instanceof HTMLElement) || !(canvas instanceof HTMLCanvasElement)
  || !(load instanceof HTMLButtonElement) || !(reset instanceof HTMLButtonElement)
  || !(ping instanceof HTMLButtonElement) || !(fullscreen instanceof HTMLButtonElement)
  || !(inspector instanceof HTMLButtonElement) || !(inspectorHost instanceof HTMLElement)
  || !status || !error || !placeholder) {
  throw new Error("The scene widget document is incomplete.");
}

const bridge = new WidgetBridge();
let viewer: SceneViewer | undefined;
let controller: AbortController | undefined;
let stopHeight: (() => void) | undefined;
let disposed = false;
let busy = false;
let initialized = false;
let fullscreenSupported = true;
let switchingDisplayMode = false;

function showError(message: string) {
  error!.textContent = message;
  error!.hidden = false;
  status!.textContent = "The scene viewer needs attention.";
}

function theme(context: Record<string, unknown> | undefined) {
  const value = context?.theme;
  if (value === "dark" || value === "light" || value === "high-contrast") root!.dataset.theme = value;
  if (context?.displayMode === "inline" || context?.displayMode === "fullscreen") displayMode(context.displayMode);
  if (fullscreen instanceof HTMLButtonElement && Array.isArray(context?.availableDisplayModes)) {
    fullscreenSupported = context.availableDisplayModes.includes("fullscreen");
    displayMode(root!.dataset.displayMode === "fullscreen" ? "fullscreen" : "inline");
  }
}
bridge.onContext = theme;

function displayMode(mode: "inline" | "fullscreen") {
  root!.dataset.displayMode = mode;
  fullscreen!.setAttribute("aria-pressed", String(mode === "fullscreen"));
  fullscreen!.setAttribute("aria-label", mode === "fullscreen" ? "Exit full screen" : "Enter full screen");
  if (fullscreen instanceof HTMLButtonElement) {
    fullscreen.disabled = switchingDisplayMode || (mode !== "fullscreen" && !fullscreenSupported);
    fullscreen.title = mode === "fullscreen" ? "Return to the conversation"
      : fullscreenSupported ? "Expand in Teams" : "Full screen is unavailable in this Teams client";
  }
}

function inspectorState(visible: boolean) {
  inspectorHost!.hidden = !visible;
  inspector!.setAttribute("aria-pressed", String(visible));
  inspector!.setAttribute("aria-expanded", String(visible));
  inspector!.setAttribute("aria-label", visible ? "Hide Babylon inspector" : "Show Babylon inspector");
}

async function connect() {
  const result = await bridge.connect();
  if (!initialized) {
    theme(object(result.hostContext));
    initialized = true;
  }
  stopHeight ??= reportHeight(root!, bridge);
}

async function boot() {
  try {
    await connect();
    if (!disposed && !busy && !viewer) status!.textContent = "Ready. Load the snapshot to explore it.";
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
  reset.disabled = inspector.disabled = true;
  inspectorState(false);
  error.hidden = true;
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
    const bytes = await fetchGlb(url.href, data.size, controller.signal);
    if (disposed) return;
    status.textContent = "Preparing 3D geometry and lighting...";
    viewer = new SceneViewer(canvas, () => {
      if (!disposed) {
        reset.disabled = inspector.disabled = true;
        showError("The graphics context was lost. Load scene again to recreate the viewer, or download the GLB.");
      }
    });
    await viewer.load(bytes, settings.environmentUrl, controller.signal);
    if (disposed) return;
    placeholder.hidden = true;
    reset.disabled = inspector.disabled = false;
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
fullscreen.addEventListener("click", async () => {
  if (switchingDisplayMode || disposed) return;
  switchingDisplayMode = true;
  fullscreen.disabled = true;
  error.hidden = true;
  try {
    displayMode(await bridge.displayMode(root.dataset.displayMode === "fullscreen" ? "inline" : "fullscreen"));
  } catch (reason) {
    if (!disposed) showError(reason instanceof Error ? reason.message : "Teams could not expand the viewer.");
  } finally {
    switchingDisplayMode = false;
    if (!disposed) displayMode(root.dataset.displayMode === "fullscreen" ? "fullscreen" : "inline");
  }
});
inspector.addEventListener("click", async () => {
  const active = viewer;
  if (!active || disposed) return;
  inspector.disabled = true;
  error.hidden = true;
  inspectorHost.hidden = false;
  try {
    const visible = await active.toggleInspector(inspectorHost);
    if (!disposed && viewer === active) inspectorState(visible);
  } catch {
    if (!disposed && viewer === active) {
      inspectorState(false);
      showError("The Babylon inspector could not be opened. Reload the scene and retry.");
    }
  } finally {
    if (!disposed && viewer === active) inspector.disabled = false;
  }
});
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
