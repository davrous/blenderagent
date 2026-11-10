import { ArcRotateCamera } from "@babylonjs/core/Cameras/arcRotateCamera";
import { Color4 } from "@babylonjs/core/Maths/math.color";
import { Engine } from "@babylonjs/core/Engines/engine";
import { FramingBehavior } from "@babylonjs/core/Behaviors/Cameras/framingBehavior";
import { HDRCubeTexture } from "@babylonjs/core/Materials/Textures/hdrCubeTexture";
import { Scene } from "@babylonjs/core/scene";
import { SceneLoader } from "@babylonjs/core/Loading/sceneLoader";
import { Vector3 } from "@babylonjs/core/Maths/math.vector";
import "@babylonjs/loaders/glTF";
import "@babylonjs/core/Helpers/sceneHelpers";

export const MAX_GLB_BYTES = 200 * 1024 * 1024;

export function freshEnvironmentUrl(source: string): string {
  const url = new URL(source);
  // Existing widget frames can retain a truncated HDR response in their HTTP cache.
  url.searchParams.set("widget-load", crypto.randomUUID());
  return url.href;
}

export function validateGlb(bytes: ArrayBuffer): void {
  const header = new DataView(bytes);
  if (bytes.byteLength < 20 || header.getUint32(0, true) !== 0x46546c67
    || header.getUint32(4, true) !== 2 || header.getUint32(8, true) !== bytes.byteLength
    || header.getUint32(16, true) !== 0x4e4f534a) {
    throw new Error("This file is not a valid GLB 2.0 snapshot.");
  }
  const length = header.getUint32(12, true);
  if (length > bytes.byteLength - 20 || length > 16 * 1024 * 1024) {
    throw new Error("The GLB metadata is invalid or too large.");
  }
  const document: unknown = JSON.parse(new TextDecoder().decode(new Uint8Array(bytes, 20, length)));
  if (!document || typeof document !== "object" || Array.isArray(document)) {
    throw new Error("The GLB document is invalid.");
  }
  const data = document as Record<string, unknown>;
  for (const field of ["buffers", "images"]) {
    const resources = data[field];
    if (resources !== undefined && !Array.isArray(resources)) throw new Error("The GLB resources are invalid.");
    for (const resource of resources ?? []) {
      if (!resource || typeof resource !== "object") throw new Error("The GLB resource is invalid.");
      const uri: unknown = resource.uri;
      if (uri !== undefined && (typeof uri !== "string" || !uri.startsWith("data:"))) {
        throw new Error("Only self-contained GLB snapshots are supported. Export the scene again.");
      }
    }
  }
  if (Array.isArray(data.extensionsUsed) && data.extensionsUsed.some(
    (extension) => ["KHR_draco_mesh_compression", "KHR_texture_basisu", "EXT_meshopt_compression"].includes(extension),
  )) {
    throw new Error("Compressed GLB extensions are not supported by this viewer. Export without compression.");
  }
}

export async function fetchGlb(url: string, size: number, signal: AbortSignal): Promise<ArrayBuffer> {
  const response = await fetch(url, { signal, credentials: "omit", redirect: "error", cache: "no-store" });
  if (!response.ok || !response.body) {
    throw new Error(`The snapshot could not be downloaded (${response.status}). Load scene again to renew access.`);
  }
  const length = response.headers.get("Content-Length");
  if (length && (!/^\d+$/.test(length) || Number(length) !== size)) throw new Error("The snapshot size has changed.");
  const reader = response.body.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > size || total > MAX_GLB_BYTES) throw new Error("The snapshot exceeds the download size limit.");
      parts.push(value);
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  if (total !== size) throw new Error("The snapshot download was incomplete. Please retry.");
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
  validateGlb(bytes.buffer);
  return bytes.buffer;
}

export class SceneViewer {
  private engine: Engine;
  private scene: Scene;
  private camera?: ArcRotateCamera;
  private resize: ResizeObserver;
  private intersection: IntersectionObserver;
  private visible = true;
  private activeUntil = 0;
  private disposed = false;
  private reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  private initial?: { alpha: number; beta: number; radius: number; target: Vector3 };

  constructor(private canvas: HTMLCanvasElement, onLost: () => void) {
    this.engine = new Engine(canvas, true, {
      stencil: true, premultipliedAlpha: false, useHighPrecisionMatrix: true,
    });
    this.engine.hideLoadingUI();
    this.scene = new Scene(this.engine);
    this.scene.clearColor = new Color4(0.055, 0.063, 0.078, 1);
    this.resize = new ResizeObserver(() => { this.engine.resize(); this.wake(); });
    this.resize.observe(canvas);
    this.intersection = new IntersectionObserver(([entry]) => {
      this.visible = entry.isIntersecting;
      if (this.visible) this.wake(); else this.engine.stopRenderLoop();
    });
    this.intersection.observe(canvas);
    this.engine.onContextLostObservable.add(() => { this.engine.stopRenderLoop(); onLost(); });
    document.addEventListener("visibilitychange", this.visibility);
    canvas.addEventListener("pointerdown", this.wake);
    canvas.addEventListener("pointermove", this.wake);
    canvas.addEventListener("wheel", this.wake);
    canvas.addEventListener("keydown", this.key);
  }

  async load(bytes: ArrayBuffer, environmentUrl: string, signal: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const abort = () => reject(new Error("Loading timed out or the viewer was closed. Load scene again to retry."));
      if (signal.aborted) { abort(); return; }
      signal.addEventListener("abort", abort, { once: true });
      this.loadScene(bytes, environmentUrl).then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
    });
  }

  private async loadScene(bytes: ArrayBuffer, environmentUrl: string): Promise<void> {
    SceneLoader.ShowLoadingScreen = false;
    await SceneLoader.AppendAsync("", new File([bytes], "scene.glb", { type: "model/gltf-binary" }), this.scene);
    if (this.disposed) return;
    if (!this.scene.meshes.some((mesh) => mesh.getTotalVertices() > 0)) throw new Error("This snapshot contains no visible geometry.");
    const environment = await new Promise<HDRCubeTexture>((resolve, reject) => {
      const texture = new HDRCubeTexture(
        freshEnvironmentUrl(environmentUrl), this.scene, 256, false, true, false, true,
        () => resolve(texture),
        () => reject(new Error("The HDR lighting data could not be downloaded or decoded. Load scene again to retry.")),
      );
    });
    if (this.disposed) return;
    this.scene.environmentTexture = environment;
    this.scene.createDefaultCamera(true, true, true);
    const camera = this.scene.activeCamera;
    if (!(camera instanceof ArcRotateCamera)) throw new Error("The orbit camera could not be created.");
    this.camera = camera;
    camera.alpha += Math.PI;
    camera.useFramingBehavior = true;
    const framing = camera.getBehaviorByName("Framing");
    if (framing instanceof FramingBehavior) {
      framing.framingTime = 0;
      framing.elevationReturnTime = -1;
      const bounds = this.scene.getWorldExtends((mesh) => mesh.isVisible && mesh.isEnabled());
      camera.lowerRadiusLimit = null;
      framing.zoomOnBoundingInfo(bounds.min, bounds.max);
    }
    camera.radius = Math.max(camera.radius, 0.01);
    camera.lowerRadiusLimit = camera.radius / 100;
    camera.upperRadiusLimit = camera.radius * 5;
    camera.pinchPrecision = 200 / camera.radius;
    camera.wheelDeltaPercentage = camera.pinchDeltaPercentage = 0.01;
    if (this.reducedMotion) camera.inertia = camera.panningInertia = 0;
    camera.attachControl(this.canvas, true);
    // Widget keyboard controls below avoid duplicate Babylon arrow handling.
    camera.inputs.removeByType("ArcRotateCameraKeyboardMoveInput");
    this.scene.animationGroups.forEach((group) => group.stop());
    this.scene.createDefaultSkybox(environment, true, (camera.maxZ - camera.minZ) / 2, 0.3, false);
    this.initial = { alpha: camera.alpha, beta: camera.beta, radius: camera.radius, target: camera.target.clone() };
    this.wake();
  }

  reset() {
    if (!this.camera || !this.initial) return;
    Object.assign(this.camera, {
      alpha: this.initial.alpha, beta: this.initial.beta, radius: this.initial.radius,
      inertialAlphaOffset: 0, inertialBetaOffset: 0, inertialRadiusOffset: 0,
      inertialPanningX: 0, inertialPanningY: 0,
    });
    this.camera.setTarget(this.initial.target.clone());
    this.wake();
  }

  private key = (event: KeyboardEvent) => {
    const camera = this.camera;
    if (!camera) return;
    const direction = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -1, ArrowDown: 1 }[event.key];
    if (event.key === "Home") this.reset();
    else if (event.key === "+" || event.key === "=") camera.radius = Math.max(camera.lowerRadiusLimit ?? 0.001, camera.radius * 0.9);
    else if (event.key === "-") camera.radius = Math.min(camera.upperRadiusLimit ?? Infinity, camera.radius * 1.1);
    else if (direction !== undefined) {
      if (event.shiftKey) {
        const inverse = camera.getViewMatrix().clone().invert();
        const axis = event.key === "ArrowLeft" || event.key === "ArrowRight" ? Vector3.Right() : Vector3.Up();
        const delta = Vector3.TransformNormal(axis, inverse).scale(direction * camera.radius * 0.04);
        camera.setTarget(camera.target.add(delta));
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") camera.alpha += direction * 0.1;
      else camera.beta = Math.max(0.02, Math.min(Math.PI - 0.02, camera.beta + direction * 0.1));
    } else return;
    event.preventDefault();
    this.wake();
  };

  private visibility = () => {
    if (document.hidden) this.engine.stopRenderLoop(); else this.wake();
  };

  private render = () => {
    if (this.disposed || document.hidden || !this.visible || performance.now() > this.activeUntil) {
      this.engine.stopRenderLoop();
      return;
    }
    if (this.camera) {
      this.camera.panningSensibility = 5000 / this.camera.radius;
      this.camera.speed = this.camera.radius * 0.2;
      this.scene.render();
    }
  };

  private wake = () => {
    if (this.disposed || document.hidden || !this.visible) return;
    this.activeUntil = performance.now() + (this.reducedMotion ? 100 : 1500);
    this.engine.runRenderLoop(this.render);
  };

  dispose() {
    this.disposed = true;
    this.resize.disconnect();
    this.intersection.disconnect();
    document.removeEventListener("visibilitychange", this.visibility);
    this.canvas.removeEventListener("pointerdown", this.wake);
    this.canvas.removeEventListener("pointermove", this.wake);
    this.canvas.removeEventListener("wheel", this.wake);
    this.canvas.removeEventListener("keydown", this.key);
    this.engine.stopRenderLoop();
    this.scene.dispose();
    this.engine.dispose();
  }
}
