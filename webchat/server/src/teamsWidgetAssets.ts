import express, { type Express } from "express";
import cors from "cors";
import path from "node:path";
import { fileURLToPath } from "node:url";

export function registerWidgetAssets(app: Express, rendererOrigin: string | undefined) {
  if (rendererOrigin && !/^https:\/\/[a-f0-9]{32}\.widget-renderer\.usercontent\.microsoft$/.test(rendererOrigin)) {
    throw new Error("TEAMS_WIDGET_RENDERER_ORIGIN must be the exact Teams widget renderer HTTPS origin.");
  }
  app.use(
    "/teams-widget",
    cors({ origin: rendererOrigin || false, methods: ["GET", "HEAD"] }),
    express.static(path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../client/dist/teams-widget"), {
      maxAge: "1h",
      fallthrough: false,
      index: false,
      dotfiles: "deny",
    }),
  );
}
