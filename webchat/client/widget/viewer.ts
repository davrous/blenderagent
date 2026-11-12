import { ArcRotateCamera } from "@babylonjs/core/Cameras/arcRotateCamera";
import { Color4 } from "@babylonjs/core/Maths/math.color";
import { Engine } from "@babylonjs/core/Engines/engine";
import type { AbstractMesh } from "@babylonjs/core/Meshes/abstractMesh";
import { HDRCubeTexture } from "@babylonjs/core/Materials/Textures/hdrCubeTexture";
import { Scene } from "@babylonjs/core/scene";
import { SceneLoader } from "@babylonjs/core/Loading/sceneLoader";
import { Vector3 } from "@babylonjs/core/Maths/math.vector";
import "@babylonjs/loaders/glTF";
import "@babylonjs/core/Helpers/sceneHelpers";

export const MAX_GLB_BYTES = 200 * 1024 * 1024;
export const FRAME_FILL = 0.95;
export const IDLE_ROTATE_MS = 5000;
type Bounds = { min: Vector3; max: Vector3; corners: readonly Vector3[] };

export function modelBounds(meshes: readonly AbstractMesh[]): Bounds {
  const boxes = meshes.filter((mesh) =>
    mesh.isVisible && mesh.visibility > 0 && mesh.isEnabled() && !mesh.infiniteDistance && mesh.getTotalVertices() > 0,
  ).map((mesh) => {
    mesh.computeWorldMatrix(true);
    const box = mesh.getBoundingInfo().boundingBox;
    const min = box.minimumWorld.clone(), max = box.maximumWorld.clone();
    if (![...min.asArray(), ...max.asArray()].every(Number.isFinite)) throw new Error("The snapshot has invalid model bounds.");
    return { min, max, size: max.subtract(min), corners: box.vectorsWorld.map((point) => point.clone()) };
  });
  if (!boxes.length) throw new Error("This snapshot contains no visible geometry.");
  const combine = (items: readonly Bounds[]): Bounds => ({
    min: items.reduce((min, box) => Vector3.Minimize(min, box.min), items[0].min.clone()),
    max: items.reduce((max, box) => Vector3.Maximize(max, box.max), items[0].max.clone()),
    corners: items.flatMap((box) => box.corners),
  });
  const subjects = boxes.filter(({ size }) => size.y > Math.max(size.x, size.z) * 0.002);
  if (!subjects.length) return combine(boxes);
  const subjectBounds = combine(subjects);
  const size = subjectBounds.max.subtract(subjectBounds.min);
  const extent = Math.max(size.x, size.y, size.z);
  // A disproportionately large, flat ground/water surface should not dwarf the actual subject.
  return combine(boxes.filter((box) => !(box.size.y <= Math.max(box.size.x, box.size.z) * 0.002
    && Math.max(box.size.x, box.size.z) > extent * 4)));
}

export function cameraFrame(bounds: Bounds, alpha: number, beta: number, fov: number, aspect: number) {
  if (!(aspect > 0) || !Number.isFinite(aspect) || !(fov > 0 && fov < Math.PI)) {
    throw new Error("The viewer dimensions are invalid.");
  }
  const target = bounds.min.add(bounds.max).scale(0.5);
  const direction = new Vector3(Math.cos(alpha) * Math.sin(beta), Math.cos(beta), Math.sin(alpha) * Math.sin(beta));
  const right = Vector3.Cross(Vector3.Up(), direction).normalize();
  const up = Vector3.Cross(direction, right).normalize();
  const vertical = Math.tan(fov / 2) * FRAME_FILL, horizontal = vertical * aspect;
  const diagonal = Vector3.Distance(bounds.min, bounds.max);
  let radius = Math.max(diagonal * 0.001, 1e-10);
  for (const corner of bounds.corners) {
    const point = corner.subtract(target);
    radius = Math.max(radius, Vector3.Dot(point, direction) + Math.max(
      Math.abs(Vector3.Dot(point, right)) / horizontal, Math.abs(Vector3.Dot(point, up)) / vertical,
    ));
  }
  return { target, radius, minZ: Math.max(diagonal / 10000, 1e-10), maxZ: radius + Math.max(diagonal, 1e-8) * 20 };
}

export function freshEnvironmentUrl(source: string): string {
  const url = new URL(source);
  // Existing widget frames can retain a truncated HDR response in their HTTP cache.
  url.searchParams.set("widget-load", crypto.randomUUID());
  return url.href;
}

// Logarithmic mapping so each slider step changes the distance by the same ratio.
export function zoomLevel(radius: number, lower: number, upper: number): number {
  if (!(lower > 0 && upper > lower && radius > 0)) return 0;
  return Math.min(1, Math.max(0, Math.log(upper / radius) / Math.log(upper / lower)));
}

export function zoomRadius(level: number, lower: number, upper: number): number {
  const clamped = Math.min(1, Math.max(0, Number.isFinite(level) ? level : 0));
  return upper * Math.pow(lower / upper, clamped);
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
  private zoomRange?: { near: number; far: number };
  private resize: ResizeObserver;
  private intersection: IntersectionObserver;
  private visible = true;
  private activeUntil = 0;
  private disposed = false;
  private reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  private initial?: { alpha: number; beta: number };
  private bounds?: Bounds;
  private autoRotate = !this.reducedMotion;
  private userControlled = false;
  private easing = false;
  private idleTimer?: ReturnType<typeof setTimeout>;
  private inspector?: typeof import("@babylonjs/inspector").Inspector;
  private inspectorVisible = false;

  constructor(private canvas: HTMLCanvasElement, onLost: () => void, private onZoom?: (level: number) => void) {
    this.engine = new Engine(canvas, true, {
      stencil: true, premultipliedAlpha: false, useHighPrecisionMatrix: true,
    });
    this.engine.hideLoadingUI();
    this.scene = new Scene(this.engine);
    this.scene.clearColor = new Color4(0.055, 0.063, 0.078, 1);
    this.resize = new ResizeObserver(() => {
      this.engine.resize();
      if (!this.userControlled) this.fit();
      this.wake();
    });
    this.resize.observe(canvas);
    this.intersection = new IntersectionObserver(([entry]) => {
      this.visible = entry.isIntersecting;
      if (this.visible) this.wake(); else this.engine.stopRenderLoop();
    });
    this.intersection.observe(canvas);
    this.engine.onContextLostObservable.add(() => { this.engine.stopRenderLoop(); onLost(); });
    document.addEventListener("visibilitychange", this.visibility);
    canvas.addEventListener("pointerdown", this.interact);
    canvas.addEventListener("pointermove", this.pointerMove);
    canvas.addEventListener("wheel", this.interact, { passive: true });
    canvas.addEventListener("keydown", this.key);
  }

  async load(bytes: ArrayBuffer, environmentUrl: string, signal: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const abort = () => reject(new Error("Loading timed out or the viewer was closed. Use Reload scene to retry."));
      if (signal.aborted) { abort(); return; }
      signal.addEventListener("abort", abort, { once: true });
      this.loadScene(bytes, environmentUrl).then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
    });
  }

  private async loadScene(bytes: ArrayBuffer, environmentUrl: string): Promise<void> {
    SceneLoader.ShowLoadingScreen = false;
    await SceneLoader.AppendAsync("", new File([bytes], "scene.glb", { type: "model/gltf-binary" }), this.scene);
    if (this.disposed) return;
    this.bounds = modelBounds(this.scene.meshes);
    const environment = await new Promise<HDRCubeTexture>((resolve, reject) => {
      const texture = new HDRCubeTexture(
        freshEnvironmentUrl(environmentUrl), this.scene, 256, false, true, false, true,
        () => resolve(texture),
        () => reject(new Error("The HDR lighting data could not be downloaded or decoded. Load scene again to retry.")),
      );
    });
    if (this.disposed) return;
    this.scene.environmentTexture = environment;
    const camera = new ArcRotateCamera("viewer-camera", Math.PI * 0.75, Math.PI / 3, 1, Vector3.Zero(), this.scene);
    this.scene.activeCamera = camera;
    this.camera = camera;
    this.fit();
    const diagonal = Math.max(Vector3.Distance(this.bounds.min, this.bounds.max), 1e-8);
    camera.lowerRadiusLimit = diagonal / 1000;
    camera.upperRadiusLimit = diagonal * 20;
    // The slider stays outside the bounding sphere; wheel, pinch and keys keep the full limits.
    this.zoomRange = { near: diagonal / 2, far: diagonal * 4 };
    camera.wheelDeltaPercentage = camera.pinchDeltaPercentage = 0.01;
    if (this.reducedMotion) camera.inertia = camera.panningInertia = 0;
    camera.attachControl(this.canvas, true);
    // Widget keyboard controls below avoid duplicate Babylon arrow handling.
    camera.inputs.removeByType("ArcRotateCameraKeyboardMoveInput");
    camera.onViewMatrixChangedObservable.add(() => this.onZoom?.(this.zoom));
    this.scene.animationGroups.forEach((group) => group.stop());
    this.scene.createDefaultSkybox(environment, true, (camera.maxZ - camera.minZ) / 2, 0.3, false);
    this.initial = { alpha: camera.alpha, beta: camera.beta };
    this.onZoom?.(this.zoom);
    this.wake();
  }

  private fit(blend = 1) {
    if (!this.camera || !this.bounds) return;
    const frame = cameraFrame(this.bounds, this.camera.alpha, this.camera.beta, this.camera.fov, this.engine.getAspectRatio(this.camera));
    const { alpha, beta, radius } = this.camera;
    this.camera.setTarget(blend >= 1 ? frame.target : Vector3.Lerp(this.camera.target, frame.target, blend));
    this.camera.alpha = alpha;
    this.camera.beta = beta;
    this.camera.radius = radius + (frame.radius - radius) * Math.min(blend, 1);
    this.camera.minZ = frame.minZ;
    this.camera.maxZ = frame.maxZ;
    this.camera.pinchPrecision = 200 / frame.radius;
    if (Math.abs(this.camera.radius - frame.radius) <= frame.radius * 1e-3
      && Vector3.Distance(this.camera.target, frame.target) <= frame.radius * 1e-3) this.easing = false;
  }

  async toggleInspector(host: HTMLElement): Promise<boolean> {
    this.interact();
    const { Inspector } = await import("@babylonjs/inspector");
    if (this.disposed) return false;
    this.inspector = Inspector;
    if (this.inspectorVisible) Inspector.Hide();
    else Inspector.Show(this.scene, {
      globalRoot: host, embedMode: true, overlay: true, enableClose: false, enablePopup: false,
      skipDefaultFontLoading: true,
    });
    this.inspectorVisible = !this.inspectorVisible;
    this.wake();
    return this.inspectorVisible;
  }

  get zoom(): number {
    const { camera, zoomRange } = this;
    if (!camera || !zoomRange) return 0;
    return zoomLevel(camera.radius, zoomRange.near, zoomRange.far);
  }

  setZoom(level: number) {
    const { camera, zoomRange } = this;
    if (!camera || !zoomRange) return;
    this.interact();
    camera.inertialRadiusOffset = 0;
    camera.radius = zoomRadius(level, zoomRange.near, zoomRange.far);
    this.wake();
  }

  reset() {
    if (!this.camera || !this.initial) return;
    this.interact();
    Object.assign(this.camera, {
      alpha: this.initial.alpha, beta: this.initial.beta,
      inertialAlphaOffset: 0, inertialBetaOffset: 0, inertialRadiusOffset: 0,
      inertialPanningX: 0, inertialPanningY: 0,
    });
    this.fit();
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
    this.interact();
    event.preventDefault();
    this.wake();
  };

  private visibility = () => {
    if (document.hidden) this.engine.stopRenderLoop(); else this.wake();
  };

  private render = () => {
    if (this.disposed || document.hidden || !this.visible
      || (!this.autoRotate && !this.inspectorVisible && performance.now() > this.activeUntil)) {
      this.engine.stopRenderLoop();
      return;
    }
    if (this.camera) {
      if (this.autoRotate) {
        const seconds = Math.min(this.engine.getDeltaTime() / 1000, 0.05);
        this.camera.alpha += Math.PI / 90 * seconds;
        // After idle, glide back to the full-frame view instead of snapping.
        this.fit(this.easing ? 1 - Math.exp(-seconds * 2.5) : 1);
      }
      this.camera.panningSensibility = 5000 / this.camera.radius;
      this.camera.speed = this.camera.radius * 0.2;
      this.scene.render();
    }
  };

  private interact = () => {
    this.autoRotate = false;
    this.easing = false;
    this.userControlled = true;
    clearTimeout(this.idleTimer);
    if (!this.reducedMotion && !this.disposed) this.idleTimer = setTimeout(this.resumeRotation, IDLE_ROTATE_MS);
    this.wake();
  };

  private pointerMove = (event: PointerEvent) => {
    if (event.buttons) this.interact(); else this.wake();
  };

  private resumeRotation = () => {
    const camera = this.camera;
    if (this.disposed || !camera) return;
    if (this.inspectorVisible) { this.interact(); return; }
    camera.inertialAlphaOffset = camera.inertialBetaOffset = camera.inertialRadiusOffset = 0;
    camera.inertialPanningX = camera.inertialPanningY = 0;
    this.userControlled = false;
    this.easing = true;
    this.autoRotate = true;
    this.wake();
  };

  private wake = () => {
    if (this.disposed || document.hidden || !this.visible) return;
    this.activeUntil = performance.now() + (this.reducedMotion ? 100 : 1500);
    this.engine.runRenderLoop(this.render);
  };

  dispose() {
    this.disposed = true;
    clearTimeout(this.idleTimer);
    this.resize.disconnect();
    this.intersection.disconnect();
    document.removeEventListener("visibilitychange", this.visibility);
    this.canvas.removeEventListener("pointerdown", this.interact);
    this.canvas.removeEventListener("pointermove", this.pointerMove);
    this.canvas.removeEventListener("wheel", this.interact);
    this.canvas.removeEventListener("keydown", this.key);
    this.engine.stopRenderLoop();
    if (this.inspectorVisible) this.inspector?.Hide();
    this.scene.dispose();
    this.engine.dispose();
  }
}
