import { copyFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const output = fileURLToPath(new URL("./dist/teams-widget/v1/", import.meta.url));

export default defineConfig({
  publicDir: false,
  plugins: [{
    name: "widget-environment",
    closeBundle() {
      copyFileSync(
        fileURLToPath(new URL("../server/assets/studio_small_09_1k.hdr", import.meta.url)),
        `${output}studio_small_09_1k.hdr`,
      );
    },
  }],
  build: {
    outDir: output,
    emptyOutDir: true,
    lib: {
      entry: fileURLToPath(new URL("./widget/main.ts", import.meta.url)),
      formats: ["iife"],
      name: "BlenderSceneWidget",
      fileName: () => "widget.js",
    },
    rollupOptions: { output: { assetFileNames: "widget.[ext]", inlineDynamicImports: true } },
  },
});
