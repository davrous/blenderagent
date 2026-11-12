import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { once } from "node:events";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

const require = createRequire(new URL("../webchat/package.json", import.meta.url));
const { register } = await import(pathToFileURL(require.resolve("tsx/esm/api")).href);
register();
const { WidgetBridge, reportHeight } = await import("../webchat/client/widget/bridge.ts");
const { validateGlb, fetchGlb, freshEnvironmentUrl, modelBounds, cameraFrame, SceneViewer, zoomLevel, zoomRadius, FRAME_FILL } = await import("../webchat/client/widget/viewer.ts");
const { NullEngine } = await import(pathToFileURL(require.resolve("@babylonjs/core/Engines/nullEngine.js")).href);
const { Scene } = await import(pathToFileURL(require.resolve("@babylonjs/core/scene.js")).href);
const { MeshBuilder } = await import(pathToFileURL(require.resolve("@babylonjs/core/Meshes/meshBuilder.js")).href);
const { TransformNode } = await import(pathToFileURL(require.resolve("@babylonjs/core/Meshes/transformNode.js")).href);
const { Vector3 } = await import(pathToFileURL(require.resolve("@babylonjs/core/Maths/math.vector.js")).href);
const { ArcRotateCamera } = await import(pathToFileURL(require.resolve("@babylonjs/core/Cameras/arcRotateCamera.js")).href);
const { registerWidgetAssets } = await import("../webchat/server/src/teamsWidgetAssets.ts");
const express = require("express");
const id = "a".repeat(32);
const botMri = "28:27d67f5b-b7cc-4935-b979-0fdb47ccf365";
const rendererHash = createHash("sha256").update(botMri).digest("hex").slice(0, 32);
const bareIdHash = createHash("sha256").update(botMri.slice(3)).digest("hex").slice(0, 32);
const renderer = `https://${rendererHash}.widget-renderer.usercontent.microsoft`;
const bareIdOrigin = `https://${bareIdHash}.widget-renderer.usercontent.microsoft`;
assert.equal(rendererHash, "ecf78bca578133f43cc5a936854c832d");

function glb(overrides = {}) {
  const positions = new Float32Array([
    -1, -1, -1, 1, -1, -1, 1, 1, -1, -1, 1, -1,
    -1, -1, 1, 1, -1, 1, 1, 1, 1, -1, 1, 1,
  ]);
  const normals = new Float32Array(Array.from(positions, (value) => value / Math.sqrt(3)));
  const indices = new Uint16Array([
    0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7,
    0, 1, 5, 0, 5, 4, 3, 7, 6, 3, 6, 2,
    0, 4, 7, 0, 7, 3, 1, 2, 6, 1, 6, 5,
  ]);
  const binary = Buffer.concat([Buffer.from(positions.buffer), Buffer.from(normals.buffer), Buffer.from(indices.buffer)]);
  const json = Buffer.from(JSON.stringify({
    asset: { version: "2.0" }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0, NORMAL: 1 }, indices: 2, material: 0 }] }],
    materials: [{ pbrMetallicRoughness: { baseColorFactor: [1, 0.2, 0.05, 1], metallicFactor: 0, roughnessFactor: 0.6 } }],
    buffers: [{ byteLength: binary.length }],
    bufferViews: [
      { buffer: 0, byteOffset: 0, byteLength: 96, target: 34962 },
      { buffer: 0, byteOffset: 96, byteLength: 96, target: 34962 },
      { buffer: 0, byteOffset: 192, byteLength: 72, target: 34963 },
    ],
    accessors: [
      { bufferView: 0, componentType: 5126, count: 8, type: "VEC3", min: [-1, -1, -1], max: [1, 1, 1] },
      { bufferView: 1, componentType: 5126, count: 8, type: "VEC3" },
      { bufferView: 2, componentType: 5123, count: 36, type: "SCALAR" },
    ],
    ...overrides,
  }));
  const padded = Buffer.alloc(Math.ceil(json.length / 4) * 4, 32);
  json.copy(padded);
  const header = Buffer.alloc(20);
  [0x46546c67, 2, 20 + padded.length + 8 + binary.length, padded.length, 0x4e4f534a]
    .forEach((value, index) => header.writeUInt32LE(value, index * 4));
  const binHeader = Buffer.alloc(8);
  binHeader.writeUInt32LE(binary.length);
  binHeader.writeUInt32LE(0x004e4942, 4);
  return Buffer.concat([header, padded, binHeader, binary]);
}
const arrayBuffer = (buffer) => buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
const cube = glb();

class Host extends EventTarget {
  sent = [];
  parent = { postMessage: (message, origin) => this.sent.push({ message, origin }) };
  reply(message, { origin = "https://teams.microsoft.com", source = this.parent, string = false } = {}) {
    const event = new Event("message");
    Object.assign(event, { data: string ? JSON.stringify(message) : message, source, origin });
    this.dispatchEvent(event);
  }
}
const host = new Host();
const bridge = new WidgetBridge(host, 50);
const connection = bridge.connect();
const initial = host.sent.at(-1).message;
assert.equal(initial.method, "ui/initialize");
assert.equal(initial.params.protocolVersion, "2026-01-26");
assert.deepEqual(initial.params.appCapabilities.availableDisplayModes, ["inline", "fullscreen"]);
let connected = false;
connection.then(() => connected = true);
host.reply({ jsonrpc: "2.0", id: initial.id, result: {} }, { source: {} });
await Promise.resolve();
assert.equal(connected, false, "Only the actual parent can complete the handshake.");
host.reply({ jsonrpc: "2.0", id: initial.id, result: { hostContext: { theme: "dark" } } }, { string: true });
assert.equal((await connection).hostContext.theme, "dark");
assert.equal(host.sent.at(-1).message.method, "ui/notifications/initialized");
let theme;
bridge.onContext = (context) => theme = context.theme;
host.reply({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme: "light" } });
assert.equal(theme, "light");
host.reply({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme: "dark" } }, { origin: "https://untrusted.example" });
assert.equal(theme, "light");

const call = bridge.call("scene_widget_ping", id);
await Promise.resolve();
await Promise.resolve();
const request = host.sent.at(-1).message;
assert.deepEqual(request.params, { name: "scene_widget_ping", arguments: { id } });
let completed = false;
call.then(() => completed = true);
host.reply({ jsonrpc: "2.0", id: request.id, result: { structuredContent: { version: 1, id } } }, { origin: "https://untrusted.example" });
await Promise.resolve();
assert.equal(completed, false);
host.reply({ jsonrpc: "2.0", id: request.id, result: { structuredContent: { version: 1, id }, isError: false } });
assert.deepEqual(await call, { version: 1, id });
const failed = bridge.call("load_scene_snapshot", id);
await Promise.resolve();
await Promise.resolve();
host.reply({ jsonrpc: "2.0", id: host.sent.at(-1).message.id, result: {
  isError: true, content: [{ type: "text", text: "The scene was cleared." }], structuredContent: {},
} });
await assert.rejects(failed, /scene was cleared/);

for (const mode of ["fullscreen", "inline"]) {
  const changing = bridge.displayMode(mode);
  await Promise.resolve();
  await Promise.resolve();
  const request = host.sent.at(-1).message;
  assert.equal(request.method, "ui/request-display-mode");
  assert.deepEqual(request.params, { mode });
  host.reply({ jsonrpc: "2.0", id: request.id, result: { mode } });
  assert.equal(await changing, mode);
}
const refused = bridge.displayMode("fullscreen");
await Promise.resolve();
await Promise.resolve();
host.reply({ jsonrpc: "2.0", id: host.sent.at(-1).message.id, result: { mode: "inline" } });
await assert.rejects(refused, /display mode/);

let observer;
globalThis.ResizeObserver = class {
  constructor(callback) { this.callback = callback; observer = this; }
  observe() {}
  disconnect() { this.disconnected = true; }
};
globalThis.window = { innerHeight: 1000 };
let height = 318.625;
const root = { dataset: {}, getBoundingClientRect: () => ({ height }) };
const stopHeight = reportHeight(root, bridge);
assert.equal(host.sent.at(-1).message.params.height, height);
height = 215.375;
observer.callback();
assert.equal(host.sent.at(-1).message.params.height, height, "Height can shrink without rounding.");
root.dataset.displayMode = "fullscreen";
const messagesBeforeFullscreen = host.sent.length;
height = 900;
observer.callback();
assert.equal(host.sent.length, messagesBeforeFullscreen, "Full-screen layouts must not resize their host.");
root.dataset.displayMode = "inline";
observer.callback();
assert.equal(host.sent.at(-1).message.params.height, height);
stopHeight();
assert.equal(observer.disconnected, true);
delete globalThis.window;
delete globalThis.ResizeObserver;
bridge.dispose();

for (const aspect of [0.5, 1, 2.5]) {
  const engine = new NullEngine({ renderWidth: 600 * aspect, renderHeight: 600 });
  try {
    for (const scale of [0.0001, 1, 10000]) for (const elongated of [false, true]) {
      const scene = new Scene(engine);
      const parent = new TransformNode("imported-root", scene);
      parent.position.set(1000 * scale, 200 * scale, -400 * scale);
      parent.scaling.set(2, 0.5, -1);
      parent.rotation.y = 0.3;
      const mesh = MeshBuilder.CreateBox("subject", { size: 2 }, scene);
      mesh.parent = parent;
      mesh.scaling.set((elongated ? 20 : 1) * scale, scale, scale);
      mesh.position.set(201 * scale, -73 * scale, 500 * scale);
      mesh.rotation.y = 0.6;
      const bounds = modelBounds(scene.meshes);
      for (const alpha of [0, Math.PI / 4, 3.8]) {
        const frame = cameraFrame(bounds, alpha, Math.PI / 3, 0.8, aspect);
        const camera = new ArcRotateCamera("fit", alpha, Math.PI / 3, frame.radius, frame.target, scene);
        camera.fov = 0.8;
        camera.minZ = frame.minZ;
        camera.maxZ = frame.maxZ;
        const matrix = camera.getViewMatrix(true).multiply(camera.getProjectionMatrix(true));
        let occupancy = 0;
        const vertices = mesh.getVerticesData("position");
        for (let index = 0; index < vertices.length; index += 3) {
          const world = Vector3.TransformCoordinates(Vector3.FromArray(vertices, index), mesh.getWorldMatrix());
          const point = Vector3.TransformCoordinates(world, matrix);
          occupancy = Math.max(occupancy, Math.abs(point.x), Math.abs(point.y));
          assert.ok(Math.abs(point.x) <= FRAME_FILL + 0.006 && Math.abs(point.y) <= FRAME_FILL + 0.006, "Every model vertex fits with a margin.");
          assert.ok(point.z > 0 && point.z < 1, "The model fits between the clipping planes.");
        }
        assert.ok(occupancy > FRAME_FILL - 0.02, "The fitted model fills the viewport instead of remaining distant.");
        camera.dispose();
      }
      scene.dispose();
    }
    const scene = new Scene(engine);
    const subject = MeshBuilder.CreateBox("subject", { size: 2 }, scene);
    const ground = MeshBuilder.CreateGround("support", { width: 2000, height: 2000 }, scene);
    ground.position.y = -1;
    assert.deepEqual(modelBounds(scene.meshes), modelBounds([subject]), "Oversized flat ground must not dwarf the subject.");
    assert.equal(modelBounds([ground]).max.x, 1000, "A standalone flat model is still framed in full.");
    ground.scaling.setAll(0.003);
    assert.ok(Math.abs(modelBounds(scene.meshes).max.x - 3) < 1e-5, "Ordinary scenery remains part of the framing.");
    subject.isVisible = ground.isVisible = false;
    assert.throws(() => modelBounds(scene.meshes), /no visible geometry/);
    scene.dispose();
  } finally { engine.dispose(); }
}
console.log("Camera framing fits translated, scaled and elongated models in narrow and wide viewports.");

const retryHost = new Host();
const retryBridge = new WidgetBridge(retryHost, 10);
await assert.rejects(retryBridge.connect(), /did not respond/);
const retry = retryBridge.connect();
retryHost.reply({ jsonrpc: "2.0", id: retryHost.sent.at(-1).message.id, result: {} });
await retry;
const closing = retryBridge.call("scene_widget_ping", id);
await Promise.resolve();
await Promise.resolve();
retryBridge.dispose();
await assert.rejects(closing, /closed/);

validateGlb(arrayBuffer(cube));
assert.equal(zoomRadius(0, 0.01, 100), 100);
assert.ok(Math.abs(zoomRadius(1, 0.01, 100) - 0.01) < 1e-9);
assert.ok(Math.abs(zoomRadius(0.5, 0.01, 100) - 1) < 1e-9, "The zoom slider is logarithmic.");
assert.equal(zoomRadius(5, 0.01, 100), zoomRadius(1, 0.01, 100));
assert.equal(zoomRadius(Number.NaN, 0.01, 100), 100);
for (const level of [0, 0.25, 0.5, 0.9, 1]) {
  assert.ok(Math.abs(zoomLevel(zoomRadius(level, 0.01, 100), 0.01, 100) - level) < 1e-9);
}
assert.equal(zoomLevel(1000, 0.01, 100), 0);
assert.equal(zoomLevel(0.001, 0.01, 100), 1);
const environmentSource = "https://assets.example/studio.hdr?revision=1";
const firstEnvironment = new URL(freshEnvironmentUrl(environmentSource));
const nextEnvironment = new URL(freshEnvironmentUrl(environmentSource));
assert.equal(firstEnvironment.origin, "https://assets.example");
assert.equal(firstEnvironment.pathname, "/studio.hdr");
assert.equal(firstEnvironment.searchParams.get("revision"), "1");
assert.match(firstEnvironment.searchParams.get("widget-load"), /^[a-f0-9-]{36}$/);
assert.notEqual(firstEnvironment.href, nextEnvironment.href, "Every HDR attempt bypasses a previously truncated cache entry.");
const abortedLoad = new AbortController();
const stalled = SceneViewer.prototype.load.call(
  { loadScene: () => new Promise(() => {}) }, new ArrayBuffer(0), "https://assets.example/env.hdr", abortedLoad.signal,
);
abortedLoad.abort();
await assert.rejects(stalled, /timed out/);
assert.throws(() => validateGlb(new ArrayBuffer(4)), /valid GLB/);
assert.throws(() => validateGlb(arrayBuffer(glb({ images: [{ uri: "https://untrusted.example/texture.png" }] }))), /self-contained/);
assert.throws(() => validateGlb(arrayBuffer(glb({ extensionsUsed: ["KHR_draco_mesh_compression"] }))), /Compressed/);
const realFetch = globalThis.fetch;
try {
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.credentials, "omit");
    assert.equal(options.redirect, "error");
    return new Response(cube, { headers: { "Content-Length": String(cube.length) } });
  };
  assert.equal((await fetchGlb("https://storage.example/scene.glb", cube.length, new AbortController().signal)).byteLength, cube.length);
  globalThis.fetch = async () => new Response(cube);
  await assert.rejects(fetchGlb("https://storage.example/scene.glb", 20, new AbortController().signal), /size limit/);
  globalThis.fetch = async () => new Response(cube.subarray(0, -4));
  await assert.rejects(fetchGlb("https://storage.example/scene.glb", cube.length, new AbortController().signal), /incomplete/);
  globalThis.fetch = async () => new Response("", { status: 403 });
  await assert.rejects(fetchGlb("https://storage.example/scene.glb", cube.length, new AbortController().signal), /403/);
} finally {
  globalThis.fetch = realFetch;
}

const app = express();
assert.throws(() => registerWidgetAssets(app, "https://untrusted.example"), /exact Teams/);
registerWidgetAssets(app, renderer);
app.use((error, _req, res, next) => {
  if (error?.status === 404) res.sendStatus(404);
  else next(error);
});
const server = app.listen(0, "127.0.0.1");
await once(server, "listening");
const base = `http://127.0.0.1:${server.address().port}`;
try {
  for (const file of ["widget.js", "widget.css", "studio_small_09_1k.hdr"]) {
    const response = await fetch(`${base}/teams-widget/v1/${file}`, { method: "HEAD", headers: { Origin: renderer } });
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("access-control-allow-origin"), renderer);
    assert.ok(Number(response.headers.get("content-length")) > 0);
    assert.equal(response.headers.get("cache-control"), file.endsWith(".hdr") ? "no-store" : "no-cache, must-revalidate");
  }
  const hdr = await fetch(`${base}/teams-widget/v1/studio_small_09_1k.hdr`);
  const hdrBytes = Buffer.from(await hdr.arrayBuffer());
  const sourceHdr = await readFile(new URL("../webchat/server/assets/studio_small_09_1k.hdr", import.meta.url));
  assert.deepEqual(hdrBytes, sourceHdr, "The complete HDR must be served, not a cached one-byte prefix.");
  assert.equal(hdrBytes.subarray(0, 10).toString(), "#?RADIANCE");
  for (const origin of ["https://untrusted.example", bareIdOrigin]) {
    const response = await fetch(`${base}/teams-widget/v1/widget.js`, { method: "HEAD", headers: { Origin: origin } });
    assert.notEqual(response.headers.get("access-control-allow-origin"), origin);
  }
  assert.equal((await fetch(`${base}/teams-widget/v1/`)).status, 404);
  console.log("Teams widget protocol, GLB bounds and asset CORS checks passed.");
} catch (error) {
  server.close();
  throw error;
}

const packageDirectory = await mkdtemp(path.join(tmpdir(), "teams-widget-package-"));
try {
  const result = spawnSync(process.execPath, [
    fileURLToPath(new URL("./package_teams_widget.mjs", import.meta.url)), packageDirectory,
  ], { env: { ...process.env, TEAMS_WIDGET_RENDERER_ORIGIN: renderer }, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const config = JSON.parse(await readFile(path.join(packageDirectory, "staticwebapp.config.json"), "utf8"));
  assert.equal(config.globalHeaders["Access-Control-Allow-Origin"], renderer);
  assert.notEqual(config.globalHeaders["Access-Control-Allow-Origin"], bareIdOrigin);
  assert.equal(config.mimeTypes[".hdr"], "application/octet-stream");
  assert.equal(config.routes.find((route) => route.route === "/teams-widget/v1/studio_small_09_1k.hdr").headers["Cache-Control"], "no-store");
  assert.equal(config.routes.find((route) => route.route === "/teams-widget/*").headers["Cache-Control"], "no-cache, must-revalidate");
  assert.deepEqual((await readdir(packageDirectory)).sort(),
    ["health.json", "index.html", "staticwebapp.config.json", "teams-widget"]);
  assert.deepEqual((await readdir(path.join(packageDirectory, "teams-widget", "v1"))).sort(),
    ["studio_small_09_1k.hdr", "widget.css", "widget.js"]);
  console.log("Static Web Apps package contains public assets only and exact renderer CORS.");
} finally {
  await rm(packageDirectory, { recursive: true });
}

const template = await readFile(new URL("../webchat/client/widget/widget.html", import.meta.url), "utf8");
assert.match(template, /<h2>3D viewer<\/h2>/);
assert.match(template, /id="load"[^>]*>Reload scene<\/button>/);
assert.match(template, /Loading your 3D scene automatically/);
assert.doesNotMatch(template, /id="download"|Download GLB/);
for (const label of ["Check connection", "Reset view", "Show Babylon inspector", "Enter full screen", "Zoom out", "Zoom level", "Zoom in"]) {
  assert.ok(template.includes(`aria-label="${label}"`), `The ${label} icon needs an accessible name.`);
}
assert.match(template, /aria-controls="viewer-inspector"/);
assert.doesNotMatch(template, /id="inspector-host"/, "The viewer must not reuse Babylon's reserved inspector host ID.");
console.log("3D viewer title, icon labels and inspector host are valid; no redundant download button.");

if (process.argv.includes("--serve")) {
  const environmentBytes = await readFile(new URL("../webchat/server/assets/studio_small_09_1k.hdr", import.meta.url));
  let poisonEnvironment = true;
  let environmentRequests = 0;
  app.get("/widget-test/environment.hdr", (req, res) => {
    environmentRequests++;
    const poison = poisonEnvironment && req.query["widget-load"] === undefined;
    if (poison) poisonEnvironment = false;
    res.setHeader("Cache-Control", poison ? "public, max-age=3600" : "no-store");
    res.type("application/octet-stream").send(poison ? Buffer.from("#") : environmentBytes);
  });
  app.get("/widget-test/environment-status", (_req, res) => res.json({ requests: environmentRequests }));
  const html = template.replace("__CONFIG__", JSON.stringify({
    id, storageOrigin: "https://widgettest.blob.core.windows.net",
    environmentUrl: `${base}/widget-test/environment.hdr`,
  })).replaceAll("__ASSET_BASE__", `${base}/teams-widget/v1`)
    .replace("<script defer src=", '<script src="/widget-test/mock.js"></script>\n  <script defer src=');
  app.get("/widget-test/mock.js", (_req, res) => res.type("js").send(`
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, options) => realFetch(
      typeof input === "string" && input.startsWith("https://widgettest.blob.core.windows.net/")
        ? "/widget-test/cube.glb" : input, options);
    window.widgetErrors = [];
    window.addEventListener("error", event => window.widgetErrors.push(event.message));
    window.addEventListener("unhandledrejection", event => window.widgetErrors.push(String(event.reason)));
  `));
  app.get("/widget-test/frame", (_req, res) => {
    res.setHeader("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' https://widgettest.blob.core.windows.net; img-src 'self' blob: data:; font-src data:; worker-src blob:; base-uri 'none'; frame-ancestors 'self'");
    const content = _req.query.legacy === "1" ? html.replace(/<main\b[\s\S]*?<\/main>/,
      '<main id="root" aria-label="Blender scene"><h2>Blender scene</h2><button id="load">Load scene</button><a id="download">Download GLB</a></main>') : html;
    res.type("html").send(content);
  });
  app.get("/widget-test/cube.glb", (_req, res) => res.type("model/gltf-binary").send(cube));
  app.get("/widget-test", (req, res) => res.type("html").send(`<!doctype html>
    <html><title>Teams widget browser fixture</title><body style="margin:0;max-width:760px">
    <iframe title="Scene widget" src="/widget-test/frame${req.query.legacy === "1" ? "?legacy=1" : ""}" style="border:0;width:100%;height:500px"></iframe>
    <script>
    window.widgetCalls = []; window.widgetHeights = []; window.widgetDisplayModes = [];
    window.failSnapshot = false; window.failFullscreen = false;
    window.addEventListener("message", (event) => {
      const frame = document.querySelector("iframe");
      if (event.source !== frame.contentWindow) return;
      const message = event.data;
      if (message.method === "ui/notifications/size-changed") {
        window.widgetHeights.push(message.params.height);
        frame.style.height = message.params.height + "px";
      }
      if (!message.id) return;
      let result = {};
      if (message.method === "ui/initialize") result = { protocolVersion: "2026-01-26",
        hostContext: { theme: "dark", displayMode: "inline", availableDisplayModes: ["inline", "fullscreen"] } };
      if (message.method === "ui/request-display-mode") {
        window.widgetDisplayModes.push(message.params.mode);
        if (window.failFullscreen) {
          event.source.postMessage({ jsonrpc:"2.0", id:message.id, error:{code:-32601,message:"Full screen is unavailable."} }, event.origin);
          return;
        }
        result = { mode: message.params.mode };
        frame.style.height = result.mode === "fullscreen" ? "90vh" : "500px";
        event.source.postMessage({ jsonrpc:"2.0", method:"ui/notifications/host-context-changed", params:{displayMode:result.mode} }, event.origin);
      }
      if (message.method === "tools/call") {
        window.widgetCalls.push(message.params);
        result = window.failSnapshot
          ? { isError: true, structuredContent: {}, content: [{ type:"text", text:"The scene was cleared." }] }
          : { isError: false, content: [], structuredContent: { version: 1, id: "${id}",
              size: ${cube.length}, url: "https://widgettest.blob.core.windows.net/screenshots/scenes/snapshot.glb?sig=fixture" } };
      }
      event.source.postMessage({ jsonrpc:"2.0", id: message.id, result }, event.origin);
    });
    </script></body></html>`));
  console.log(`Browser fixture ready: ${base}/widget-test`);
} else {
  server.close();
  await once(server, "close");
}
