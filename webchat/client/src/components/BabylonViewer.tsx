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
        // Side-effect import: registers sceneHelpers for createDefaultSkybox.
        await import("@babylonjs/core/Helpers/sceneHelpers");
        if (disposed) return;

        const {
          Engine,
          Scene,
          Color4,
          SceneLoader,
          HDRCubeTexture,
        } = BABYLON;
        type ArcRotateCameraT = import("@babylonjs/core").ArcRotateCamera;
        type FramingBehaviorT = import("@babylonjs/core").FramingBehavior;

        // We render our own loading overlay; suppress Babylon's full-screen
        // default loading screen which otherwise flashes briefly under React
        // 18 StrictMode (effects run mount → cleanup → mount in dev, so the
        // first engine is created then immediately disposed, adding and then
        // removing the global loading overlay).
        SceneLoader.ShowLoadingScreen = false;

        // Engine settings mirror the Babylon.js Sandbox
        // (packages/tools/sandbox/src/components/renderingZone.tsx).
        engine = new Engine(canvas, true, {
          preserveDrawingBuffer: true,
          stencil: true,
          premultipliedAlpha: false,
          useHighPrecisionMatrix: true,
        });
        engine.hideLoadingUI();
        scene = new Scene(engine);
        scene.clearColor = new Color4(0.055, 0.063, 0.078, 1);

        // Same HDR file Blender loads via PolyHaven (see scene_manager.py).
        // Served by the proxy under /api/assets so it works in dev (via the
        // Vite /api proxy) and in production. Constructor args mirror the
        // Sandbox's EnvironmentTools.LoadSkyboxPathTexture exactly:
        //   (url, scene, size=256, noMipmap=false, generateHarmonics=true,
        //    gammaSpace=false, prefilterOnLoad=true, onLoad, onError,
        //    supersample, prefilterIrradianceOnLoad=true, prefilterUsingCdf=true)
        const envTexture = new HDRCubeTexture(
          "/api/assets/studio_small_09_1k.hdr",
          scene,
          256,
          false,
          true,
          false,
          true,
          undefined,
          undefined,
          undefined,
          true,
          true,
        );
        scene.environmentTexture = envTexture;

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

        // ── Camera setup mirrors Sandbox.prepareCamera() ──
        // createDefaultCamera(true) creates an ArcRotateCamera and frames it
        // around the world bounds. Then we rotate by π for glTF's +Z forward
        // convention and use FramingBehavior.zoomOnBoundingInfo for a tighter,
        // sandbox-identical fit.
        scene.createDefaultCamera(true, true, true);
        const camera = scene.activeCamera as ArcRotateCameraT;
        // glTF assets use a +Z forward convention while the default camera
        // faces +Z; rotate so we look at the front of the asset.
        camera.alpha += Math.PI;

        camera.useFramingBehavior = true;
        const framingBehavior = camera.getBehaviorByName("Framing") as FramingBehaviorT | null;
        if (framingBehavior) {
          framingBehavior.framingTime = 0;
          framingBehavior.elevationReturnTime = -1;
        }

        if (scene.meshes.length) {
          camera.lowerRadiusLimit = null;
          const worldExtends = scene.getWorldExtends((mesh) => mesh.isVisible && mesh.isEnabled());
          framingBehavior?.zoomOnBoundingInfo(worldExtends.min, worldExtends.max);
        }

        camera.pinchPrecision = 200 / camera.radius;
        camera.upperRadiusLimit = 5 * camera.radius;
        camera.wheelDeltaPercentage = 0.01;
        camera.pinchDeltaPercentage = 0.01;
        camera.attachControl(canvas, true);

        // Skybox after the camera so we can size it relative to the camera's
        // active range — same formula as the Sandbox.
        scene.createDefaultSkybox(
          envTexture,
          true,
          (camera.maxZ - camera.minZ) / 2,
          0.3,
          false,
        );

        engine.runRenderLoop(() => {
          if (!scene || !scene.activeCamera) return;
          const cam = scene.activeCamera as ArcRotateCameraT;
          // Adapt camera sensibility based on distance to the model
          // (same logic as Sandbox.onSceneLoaded render loop).
          cam.panningSensibility = 5000 / cam.radius;
          cam.speed = cam.radius * 0.2;
          scene.render();
        });

        onResize = () => engine?.resize();
        window.addEventListener("resize", onResize);

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
