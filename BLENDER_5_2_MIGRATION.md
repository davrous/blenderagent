# Blender 5.2.2 LTS Migration Analysis

## Executive summary

Upgrade the Foundry Hosted Agent container from Blender 4.4.3 to Blender 5.2.2 LTS without changing the existing Responses, Activity, or `invocations_ws` protocol architecture.

This is a moderate, test-driven migration rather than a rewrite. The Foundry host composition, BlenderMCP-compatible JSON-over-TCP transport, scene manager, and most generated `bpy` code can remain. It is not safe to change only the version in the Dockerfile: Blender 5.x introduces breaking Python API changes in rendering and image output, changes Eevee behavior, raises graphics requirements, and writes `.blend` files that Blender 4.4 cannot reopen.

### Approved decisions

- **Target:** Blender 5.2.2 LTS.
- **Rollout:** Direct replacement after local and container validation.
- **Persisted scenes:** Do not migrate Blender 4.4 scenes. Start clean on Blender 5.2.
- **Foundry resources:** Remain within `2 vCPU / 4 GiB`, the largest currently supported Hosted Agent sandbox.
- **Architecture:** Preserve the three-protocol Agent Server host and TCP-based Blender control path.

## Current architecture

- `Dockerfile` installs Blender 4.4.3 on Ubuntu 24.04.
- `entrypoint.sh` starts Xvfb on display `:99`, starts Blender, supervises both processes, and starts the Agent Server immediately for Foundry readiness.
- The main Blender process is UI-less but not launched with `--background`; it uses a virtual display because viewport screenshots call `bpy.ops.render.opengl`.
- `video_jobs.py` already launches separate Blender workers with `--background` for animation rendering.
- `blender_startup.py` runs inside Blender, exposes a BlenderMCP-compatible TCP server on port 9876, and schedules `bpy` operations on Blender's main thread.
- `blender_connection.py` is a version-independent JSON-over-TCP client with retry and process-recovery behavior.
- `scene_manager.py` persists one scene per Foundry session under `$HOME`, which Foundry restores after an idle pause.
- `main.py` provides fixed Blender tools plus arbitrary generated Blender Python through `execute_blender_code`.
- The agent instructions explicitly target Blender 4.4 and contain Blender 4.x compatibility guidance.
- The hosted version declares `2.0` CPU and `4.0Gi` memory in `agent.yaml`.

## Feasibility

The transport and hosting layers are not coupled to Blender 4.4. The principal migration work is in Blender-side compatibility, generated-code instructions, tests, and scene-version handling.

The highest-risk areas are:

1. Eevee engine and sampling API changes.
2. Image output configuration changes.
3. Viewport/OpenGL behavior with Blender 5.2, Mesa, and Xvfb.
4. Agent-generated code that is instructed to use Blender 4.x APIs.
5. Incompatible persisted `.blend` files.
6. Staying reliable within the fixed 4 GiB Foundry memory ceiling.

## Benefits of Blender 5.2

### Support and maintainability

- Blender 5.2 is an LTS line supported through July 2028.
- New Blender documentation, examples, fixes, and ecosystem knowledge increasingly target 5.x.
- Moving now avoids accumulating more generated-code guidance around an older API.

### Cycles improvements

- Cycles can share geometry memory with Blender and synchronize geometry more efficiently.
- The optional Cycles texture cache can significantly reduce memory use and startup time for scenes containing many large textures.
- Volume rendering is more physically correct and avoids several blocky or biased artifacts.
- Subsurface scattering and displacement/normal behavior are more accurate.

### Eevee and viewport improvements

- Better screen-space ray tracing and reflection contact sharpness.
- More robust Fast GI and ambient occlusion.
- Reduced BSDF banding and more deterministic bump mapping.
- Multiple shadow, refraction, normal, and light-culling fixes.
- Instancing-heavy scenes can be substantially faster. Blender reports up to 2x for Eevee and up to 3x for Workbench/overlay in favorable cases.
- Shader compilation has been optimized, although published gains primarily use physical GPUs.

### Headless operation

Blender 5.2 continues to support `--background`. Blender 5.2 also adds `gpu.init()` for explicitly initializing the GPU backend when GPU APIs are needed in background mode.

## Drawbacks and compatibility changes

### Python API changes affecting this repository

- Eevee's engine identifier changed from `BLENDER_EEVEE_NEXT` to `BLENDER_EEVEE`.
- Blender 5.0 requires `ImageFormatSettings.media_type` to be selected before setting `file_format`.
- `blender_video.py` uses `scene.eevee.taa_render_samples`; the Blender 5.2 sampling API must be probed and updated.
- Deprecated BGL APIs are removed.
- Legacy compositor access such as `scene.node_tree` is removed; Blender 5.x uses `scene.compositing_node_group`.
- Direct dict-like access to runtime-defined `bpy.props` storage is no longer supported.
- Blender 5.2 changes Geometry Nodes modifier inputs from IDProperty-style access to RNA properties.
- The fallback Poly Haven importer still calls legacy OBJ and FBX operators. Their Blender 5.2 equivalents and arguments must be tested.

### Scene compatibility

Blender 5.x `.blend` files can only be opened by Blender 4.5 or later. Blender 4.4 reports them as invalid. Once a scene is saved by Blender 5.2, the current Blender 4.4 image cannot safely resume it.

The selected policy is to start clean on the 5.2 version and never migrate a persisted 4.4 scene in place.

### Rendering differences

- Corrected Eevee energy conservation can make some scenes appear darker.
- GI/AO, reflections, shadows, refraction, and bump mapping may differ because previous behavior contained bugs or approximations.
- Cycles volume and subsurface rendering can be more accurate but slower.
- Output should be treated as visually compatible, not pixel-identical.

### Curve and hair risk

Blender 5.0 began honoring the curve `resolution` attribute. Some older/default hair configurations can generate up to 15 times more geometry. Agent-generated scripts must cap curve and hair resolution to prevent memory exhaustion.

## Foundry micro-VM resource analysis

Microsoft Foundry Hosted Agents currently support:

| CPU | Memory |
| --- | --- |
| 0.5 vCPU | 1 GiB |
| 1 vCPU | 2 GiB |
| 2 vCPU | 4 GiB |

This project already uses the maximum `2 vCPU / 4 GiB` sandbox. A Blender 5.2 memory regression cannot be solved by selecting a larger Hosted Agent tier.

Every conversation runs in its own VM-isolated sandbox. CPU and memory billing applies to every active session, so resource usage is multiplied by concurrent conversations. Foundry persists `$HOME` after the idle timeout and restores it when the session resumes.

Foundry recommends raising an allocation when sustained peaks exceed roughly 70%. Because no larger tier exists here, use approximately 2.8 GiB as the desired sustained-memory ceiling, leaving headroom for transient peaks and the Python Agent Server.

### Expected memory impact

Treat the upgrade as resource-neutral until measured:

- Blender 5.2 may have a slightly larger idle/runtime baseline.
- Geometry-heavy Cycles scenes may use less memory.
- Texture-heavy Cycles scenes may use substantially less memory when the texture cache is enabled.
- Eevee uses slightly less video memory, but this container may use software graphics rather than dedicated VRAM.
- Common small procedural scenes may see little benefit because their texture and geometry sets are already bounded.

Do not enable the Cycles texture cache globally at first. It creates `.tx` files, consumes the per-session disk budget shared by the container and `$HOME`, and can impose a small rendering-performance penalty.

### Expected speed impact

#### Cycles CPU

Do not assume a general speedup. The code explicitly selects CPU Cycles, and two vCPUs remain the dominant constraint.

Possible improvements:

- Faster geometry synchronization.
- Faster startup for texture-heavy scenes with a texture cache.
- Lower memory pressure can avoid swapping, failure, or repeated recovery.

Possible regressions:

- Texture streaming can make rendering slightly slower.
- Physically improved volume and subsurface algorithms can cost more render time.

#### Eevee and viewport

Blender 5.x contains shader compilation, instancing, GI, and viewport optimizations. However, many published results use modern physical GPUs. Under Xvfb and Mesa software graphics:

- Gains may be smaller.
- Rendering may remain CPU-bound.
- A graphics compatibility regression is possible even if Blender starts successfully.
- Instancing-heavy scenes are the strongest likely performance win.

#### Cold start

The Blender 5.2 distribution is larger, so image transfer and process startup may be slightly slower. Shader compilation improvements may offset first-render latency. Measure the complete Foundry cold-start path rather than Blender startup in isolation.

## Expected quality impact

### Cycles

Simple opaque procedural scenes at identical samples should look broadly similar. Improvements are most visible in:

- Volumes and overlapping smoke.
- Subsurface materials.
- Displacement combined with normal maps.
- Some denoising and shading cases.

Those improvements do not mean that every image is sharper or more photorealistic. Quality is still primarily controlled by samples, lighting, materials, resolution, and denoising.

### Eevee

Blender 5.2 should provide better quality in targeted areas:

- Screen-space reflections and GI.
- Reflection contact sharpness.
- Ambient occlusion robustness.
- Reduced shading banding.
- Normal and bump consistency.
- Shadow, refraction, and light-culling correctness.

The fixes can intentionally change the appearance of old scenes.

### Workbench screenshots

New MatCaps and instancing optimizations can improve readability and responsiveness. They do not convert the software-rendered viewport capture into a final-quality render.

## Migration implementation plan

### 1. Make the runtime reproducible

- Update `Dockerfile` to install Blender 5.2.2 from the `Blender5.2` release directory.
- Separate the release series and patch version in build arguments.
- Verify the official SHA-256 checksum during the image build.
- Log Blender version, bundled Python version, render engine identifiers, graphics backend, and OpenGL capabilities at startup.
- Correct stale version references in `README.md`.

### 2. Validate the headless architecture

- Test the existing Xvfb-backed launch first because `bpy.ops.render.opengl` depends on a viewport/display context.
- Add non-interactive flags such as `--noaudio` and `--disable-autoexec` where appropriate.
- Do not convert the main Blender socket-server process to `--background` unless the screenshot path passes.
- If using GPU APIs in background mode, call `gpu.init()` before GPU-dependent work.
- Keep animation workers in `--background`.
- Preserve the current readiness and supervisor behavior.

### 3. Add a Blender compatibility module

Centralize:

- `bpy.app.version` detection.
- Eevee engine selection.
- Image media type and file-format configuration.
- Eevee sample configuration.
- OBJ, FBX, and glTF import/export operator selection.
- Startup diagnostics and supported-version validation.

Use these helpers from `blender_startup.py`, `blender_video.py`, and generated render snippets instead of scattering version checks.

### 4. Update fixed tools and generated-code instructions

- Replace `BLENDER_EEVEE_NEXT` with `BLENDER_EEVEE` in defaults, validation, rendering code, and instructions.
- Update screenshot and render paths for Blender 5.2 image-format requirements.
- Update animation Eevee sampling.
- Probe and update glTF, OBJ, and FBX operators.
- Review fixed material and node code against Blender 5.x removals.
- Rewrite the compatibility section in the system prompt for Blender 5.2.
- Add explicit guidance for runtime-defined properties, compositor nodes, Geometry Nodes modifier inputs, and curve/hair limits.
- Change error hints that currently direct the model to Blender 4.x documentation.

### 5. Enforce a clean scene-version boundary

- Store a Blender runtime/schema version in `$HOME/.blender_session_state`.
- Compare the persisted major version with the current runtime at startup.
- For a 4.x-to-5.x mismatch, remove or archive only the known persisted `scene.blend`, reset to a clean scene, and emit an explicit diagnostic event.
- Never open and resave the previous 4.4 scene in place.
- Document that 4.4 rollback is not supported for a conversation after it saves a 5.x scene.

### 6. Add an executable Blender 5.2 smoke suite

Test:

- Exact Blender version.
- Socket server startup and JSON command round-trip.
- Primitive creation and object inspection.
- Material and node creation.
- Viewport screenshot dimensions and output.
- Eevee still rendering.
- Cycles CPU still rendering.
- Workbench and Eevee animation frames.
- `.blend` save and reopen inside 5.2.
- GLB import and export.
- OBJ and FBX operator availability.
- Poly Haven material setup with deterministic local fixtures where possible.
- Explicit failure of obsolete engine identifiers and APIs.

Extend `devTools/test_blender_video.py` to use the compatibility helper.

### 7. Benchmark Blender 4.4.3 against 5.2.2

Run both images with identical:

- `--cpus=2`.
- `--memory=4g`.
- Seeded scenes and assets.
- Resolution and sample counts.
- Thread limits.
- Software graphics configuration.

Record:

| Metric | Required workloads |
| --- | --- |
| Container readiness | Fresh startup and idle-style restart |
| Blender socket readiness | Initial startup and supervised restart |
| Idle RSS | Blender plus Agent Server |
| Peak RSS | Screenshot, Eevee, Cycles, GLB import, texture, animation |
| Wall-clock time | First screenshot and each render |
| CPU usage | Startup and render peaks |
| Disk growth | Scenes, frames, images, caches, optional `.tx` files |
| Stability | Process exits, OOM, socket reconnects, recovery |
| Output | Dimensions, file size, validity, visual differences |

Fail the migration if:

- Representative sustained memory exceeds approximately 2.8 GiB without recoverable optimization.
- The container is OOM-terminated.
- Viewport screenshots are unreliable.
- Blender cannot recover cleanly after an idle-style restart.
- Render or import regressions cannot be corrected without materially reducing supported behavior.

### 8. Validate all repository surfaces

Exercise:

- Generated primitive/material scripts.
- Arbitrary Blender Python.
- Viewport screenshots.
- Eevee and Cycles renders.
- Microsoft GLB library imports.
- Poly Haven textures.
- Animation rendering and encoding.
- Scene and GLB downloads.
- Scene persistence and reset behavior.

Run the focused repository checks:

```text
python devTools/test_voice_pipeline.py
python devTools/test_activity_history.py
cd webchat && npm run build
node ../devTools/test_voice_relay.mjs
```

Run the new Blender smoke suite and video test inside the built Linux image.

### 9. Deploy and monitor

- Tag the known-good 4.4 and candidate 5.2.2 images distinctly.
- Deploy 5.2.2 only after all compatibility and resource gates pass.
- Use a fresh hosted conversation; do not reuse a session with a persisted 4.4 scene.
- Monitor Blender exits, socket reconnects, screenshot failures, render errors, cold-start duration, memory availability, and disk growth.
- Foundry routes 100% of endpoint traffic to one immutable version; traffic splitting is not available.
- Roll back only for fresh/unconverted sessions. Treat saved 5.x scene state as incompatible with 4.4.

## Fallback if 4 GiB is insufficient

Do not silently reduce quality or remove recovery guarantees.

Preferred fallback:

1. Keep interactive scene manipulation, screenshots, and lightweight previews in the Hosted Agent.
2. Save a bounded `.blend` or GLB snapshot.
3. Submit high-resolution or heavy Cycles rendering to separate render compute.
4. Return the completed artifact through the existing storage and job-status paths.

Other mitigations before externalizing rendering:

- Cap geometry, curves, hair resolution, texture dimensions, render samples, frames, and output resolution.
- Prefer Eevee for previews and Cycles only for confirmed final renders.
- Enable the texture cache only for texture-heavy scenes that demonstrate a measured memory benefit.
- Clean temporary frames and caches after successful upload.

## Expected files to change

- `Dockerfile`
- `entrypoint.sh`
- `blender_startup.py`
- `blender_video.py`
- `main.py`
- `media_analysis.py`
- `media_control.py`
- `scene_manager.py`
- `devTools/test_blender_video.py`
- A new Blender compatibility module
- A new Blender 5.2 smoke-test script under `devTools/`
- `README.md`
- `agent.yaml` only for configuration changes; no larger Hosted Agent memory tier is currently available

## Acceptance criteria

- The built image reports Blender 5.2.2 and verifies its download checksum.
- The Agent Server reaches readiness within the Hosted Agent startup window.
- The Blender TCP server starts, survives supervision, and restores after an idle-style process restart.
- Fixed tools and representative model-generated scripts run without deprecated API errors.
- Screenshots, Eevee/Cycles renders, animations, imports, exports, materials, and textures produce valid artifacts.
- Persisted Blender 4.x state is never loaded into the 5.2 runtime.
- Representative sustained memory stays below approximately 70% of the 4 GiB sandbox, with safe transient headroom.
- No workload causes OOM termination or uncontrolled disk growth.
- Responses, Activity, and `invocations_ws` behavior remains unchanged.

## Authoritative references

- [Blender 5.2 download directory](https://download.blender.org/release/Blender5.2/)
- [Blender 5.0 Python API changes](https://developer.blender.org/docs/release_notes/5.0/python_api/)
- [Blender 5.1 Python API changes](https://developer.blender.org/docs/release_notes/5.1/python_api/)
- [Blender 5.2 Python API changes](https://developer.blender.org/docs/release_notes/5.2/python_api/)
- [Blender compatibility changes](https://developer.blender.org/docs/release_notes/compatibility/)
- [Blender 5.0 Cycles changes](https://developer.blender.org/docs/release_notes/5.0/cycles/)
- [Blender 5.0 Eevee changes](https://developer.blender.org/docs/release_notes/5.0/eevee/)
- [Blender 5.2 Cycles changes](https://developer.blender.org/docs/release_notes/5.2/cycles/)
- [Blender 5.2 Eevee changes](https://developer.blender.org/docs/release_notes/5.2/eevee/)
- [Blender system requirements](https://www.blender.org/download/requirements/)
- [Foundry Hosted Agents platform details](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#platform-details)
