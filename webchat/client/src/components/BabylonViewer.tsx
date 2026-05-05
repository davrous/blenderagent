import { useEffect, useRef, useState } from "react";

interface Props {
  src: string;
  filename?: string;
  onClose: () => void;
}

export function BabylonViewer({ src, filename, onClose }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    let disposed = false;
    let engine: import("@babylonjs/core").Engine | null = null;
    let scene: import("@babylonjs/core").Scene | null = null;
    let onResize: (() => void) | null = null;

    (async () => {
      try {
        const BABYLON = await import("@babylonjs/core");
        // Side-effect import: registers the glTF/glb loader plugin.
        await import("@babylonjs/loaders/glTF");
        if (disposed) return;

        const {
          Engine,
          Scene,
          ArcRotateCamera,
          HemisphericLight,
          Vector3,
          Color4,
          SceneLoader,
        } = BABYLON;

        // We render our own loading overlay; suppress Babylon's full-screen
        // default loading screen which otherwise flashes briefly under React
        // 18 StrictMode (effects run mount → cleanup → mount in dev, so the
        // first engine is created then immediately disposed, adding and then
        // removing the global loading overlay).
        SceneLoader.ShowLoadingScreen = false;

        engine = new Engine(canvas, true, { preserveDrawingBuffer: true, stencil: true });
        engine.hideLoadingUI();
        scene = new Scene(engine);
        scene.clearColor = new Color4(0.055, 0.063, 0.078, 1);

        const camera = new ArcRotateCamera(
          "camera",
          Math.PI / 2.5,
          Math.PI / 2.5,
          5,
          Vector3.Zero(),
          scene,
        );
        camera.attachControl(canvas, true);
        camera.wheelDeltaPercentage = 0.01;
        camera.pinchDeltaPercentage = 0.01;
        camera.minZ = 0.01;

        const light = new HemisphericLight("light", new Vector3(0, 1, 0), scene);
        light.intensity = 1.1;

        engine.runRenderLoop(() => {
          scene?.render();
        });

        onResize = () => engine?.resize();
        window.addEventListener("resize", onResize);

        // Cross-origin URLs (e.g. Azure Blob SAS links) usually don't expose
        // CORS headers, so we route them through the same-origin /api/blob
        // proxy. We then fetch the bytes and hand Babylon a File object so the
        // glTF loader picks the right plugin from the extension.
        const original = new URL(src, window.location.href);
        const isCrossOrigin = original.origin !== window.location.origin;
        const loadUrl = isCrossOrigin
          ? `/api/blob?url=${encodeURIComponent(original.toString())}`
          : original.toString();

        const resp = await fetch(loadUrl);
        if (!resp.ok) {
          throw new Error(`Fetch failed: ${resp.status} ${resp.statusText}`);
        }
        const blob = await resp.blob();
        if (disposed) return;
        const file = new File([blob], filename ?? "scene.glb", {
          type: "model/gltf-binary",
        });
        // Babylon accepts a File as the second argument and uses file.name to
        // resolve the loader plugin.
        await SceneLoader.AppendAsync("", file as unknown as string, scene);
        if (disposed || !scene) return;

        // Auto-frame the camera using world AABB of all meshes.
        const meshes = scene.meshes.filter((m) => m.getTotalVertices && m.getTotalVertices() > 0);
        if (meshes.length > 0) {
          let min = meshes[0].getBoundingInfo().boundingBox.minimumWorld.clone();
          let max = meshes[0].getBoundingInfo().boundingBox.maximumWorld.clone();
          for (const m of meshes) {
            const bb = m.getBoundingInfo().boundingBox;
            min = Vector3.Minimize(min, bb.minimumWorld);
            max = Vector3.Maximize(max, bb.maximumWorld);
          }
          const center = min.add(max).scale(0.5);
          const extent = max.subtract(min);
          const radius = Math.max(extent.x, extent.y, extent.z) * 1.6 || 5;
          camera.setTarget(center);
          camera.radius = radius;
          camera.lowerRadiusLimit = radius * 0.05;
          camera.upperRadiusLimit = radius * 10;
          camera.minZ = Math.max(0.01, radius * 0.001);
          camera.maxZ = radius * 100;
        }

        setLoading(false);
      } catch (err) {
        if (disposed) return;
        console.error("BabylonViewer load failed:", err);
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      }
    })();

    return () => {
      disposed = true;
      if (onResize) window.removeEventListener("resize", onResize);
      try {
        scene?.dispose();
      } catch {
        /* ignore */
      }
      try {
        engine?.stopRenderLoop();
        engine?.dispose();
      } catch {
        /* ignore */
      }
    };
  }, [src]);

  return (
    <div
      className="babylon-viewer"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="3D scene viewer"
    >
      <div className="babylon-viewer-card" onClick={(e) => e.stopPropagation()}>
        <div className="babylon-viewer-header">
          <span className="babylon-viewer-title">
            <span aria-hidden>🧊</span>
            <span>{filename ?? "3D scene"}</span>
          </span>
          <button
            className="babylon-viewer-close"
            onClick={onClose}
            aria-label="Close 3D viewer"
            type="button"
          >
            ×
          </button>
        </div>
        <div className="babylon-viewer-stage">
          <canvas ref={canvasRef} className="babylon-viewer-canvas" />
          {loading && !error && (
            <div className="babylon-viewer-overlay">
              <div className="babylon-viewer-spinner" aria-hidden />
              <div>Loading 3D scene…</div>
            </div>
          )}
          {error && (
            <div className="babylon-viewer-overlay babylon-viewer-overlay-error">
              <div>⚠️ Failed to load scene</div>
              <div className="babylon-viewer-error-msg">{error}</div>
            </div>
          )}
        </div>
        <div className="babylon-viewer-hint">
          Drag to orbit · Right-drag to pan · Scroll to zoom · Esc to close
        </div>
      </div>
    </div>
  );
}
