type JsonObject = Record<string, unknown>;

export function object(value: unknown): JsonObject | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as JsonObject : undefined;
}

export class WidgetBridge {
  onContext?: (context: JsonObject) => void;
  private count = 0;
  private connected?: Promise<JsonObject>;
  private origin?: string;
  private pending = new Map<string, {
    resolve: (value: JsonObject) => void;
    reject: (error: Error) => void;
    timer: ReturnType<typeof setTimeout>;
  }>();

  constructor(private host: Window = window, private timeout = 15000) {
    host.addEventListener("message", this.receive);
  }

  private receive = (event: MessageEvent) => {
    if (event.source !== this.host.parent || (this.origin && event.origin !== this.origin)) return;
    let data: unknown = event.data;
    if (typeof data === "string") {
      try { data = JSON.parse(data); } catch { return; }
    }
    const message = object(data);
    if (!message || message.jsonrpc !== "2.0") return;
    if (this.origin && message.method === "ui/notifications/host-context-changed") {
      const context = object(message.params);
      if (context) this.onContext?.(context);
      return;
    }
    if (typeof message.id !== "string") return;
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) {
      pending.reject(new Error("Teams could not complete the request. Please retry."));
      return;
    }
    this.origin ??= event.origin;
    pending.resolve(object(message.result) ?? {});
  };

  notify(method: string, params: JsonObject) {
    this.host.parent.postMessage(
      { jsonrpc: "2.0", method, params }, this.origin && this.origin !== "null" ? this.origin : "*",
    );
  }

  private request(method: string, params: JsonObject): Promise<JsonObject> {
    const id = `scene-widget-${++this.count}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("Teams did not respond. The agent may be waking up; please retry."));
      }, this.timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.host.parent.postMessage(
        { jsonrpc: "2.0", id, method, params }, this.origin && this.origin !== "null" ? this.origin : "*",
      );
    });
  }

  connect(): Promise<JsonObject> {
    this.connected ??= this.request("ui/initialize", {
      protocolVersion: "2026-01-26",
      appInfo: { name: "3D viewer", version: "1.1.0" },
      appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
    }).then((result) => {
      this.notify("ui/notifications/initialized", {});
      return result;
    }).catch((error: unknown) => {
      this.connected = undefined;
      throw error;
    });
    return this.connected;
  }

  async call(name: "scene_widget_ping" | "load_scene_snapshot", id: string): Promise<JsonObject> {
    await this.connect();
    const result = await this.request("tools/call", { name, arguments: { id } });
    if (result.isError === true) {
      const content = Array.isArray(result.content) ? result.content : [];
      const text = content.map(object).find((item) => item?.type === "text")?.text;
      throw new Error(typeof text === "string" ? text : "The snapshot is unavailable. Export the scene again.");
    }
    const data = object(result.structuredContent);
    if (!data || data.version !== 1 || data.id !== id) {
      throw new Error("The agent returned an unsupported snapshot response.");
    }
    return data;
  }

  async displayMode(mode: "inline" | "fullscreen"): Promise<"inline" | "fullscreen"> {
    await this.connect();
    const result = await this.request("ui/request-display-mode", { mode });
    if (result.mode !== mode) throw new Error("Teams could not switch this widget to the requested display mode.");
    return mode;
  }

  dispose() {
    this.host.removeEventListener("message", this.receive);
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.reject(new Error("The scene viewer was closed."));
    }
    this.pending.clear();
  }
}

export function reportHeight(root: HTMLElement, bridge: WidgetBridge): () => void {
  let previous: number | undefined;
  const report = () => {
    if (root.dataset.displayMode === "fullscreen") return;
    const height = root.getBoundingClientRect().height;
    if (previous === height && window.innerHeight >= height) return;
    previous = height;
    bridge.notify("ui/notifications/size-changed", { height });
  };
  const observer = new ResizeObserver(report);
  observer.observe(root);
  report();
  return () => observer.disconnect();
}
