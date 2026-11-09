import { createServer } from "node:net";
import { networkInterfaces } from "node:os";
import { pathToFileURL } from "node:url";

function option(args, names) {
  let value;
  for (let index = 0; index < args.length; index++) {
    const arg = args[index];
    if (arg === "--") break;
    const name = names.find((name) => arg === name || arg.startsWith(`${name}=`)
      || (name.length === 2 && arg.startsWith(name) && arg.length > 2));
    if (!name) continue;
    if (value !== undefined) throw new Error(`Pass ${names[0]} only once.`);
    value = arg === name ? args[++index] : arg.slice(name.length + (arg[name.length] === "=" ? 1 : 0));
    if (!value || value.startsWith("-")) throw new Error(`${name} requires a value.`);
  }
  return value;
}

export function playgroundConfig(args, interfaces = networkInterfaces()) {
  const hostOption = option(args, ["--host"]);
  const portOption = option(args, ["--port", "-p"]);
  const serviceUrl = option(args, ["--service-url", "--su"]);
  const endpoint = option(args, ["--app-endpoint", "-e"]);
  const port = Number(portOption ?? "56150");
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("--port must be an integer between 1 and 65535.");
  }

  const lanAddress = () => {
    const entries = Object.entries(interfaces)
      .filter(([name]) => !/^(utun|tun|tap|docker|veth|br-|virbr|vEthernet)/i.test(name))
      .flatMap(([, entries]) => entries ?? []);
    const address = entries.find((entry) => entry.family === "IPv4" && !entry.internal)?.address;
    if (!address) throw new Error("No LAN IPv4 address found. Connect to a LAN or specify --host.");
    return address;
  };
  const host = hostOption ?? lanAddress();
  if (!/^[a-zA-Z0-9.:-]+$/.test(host)) throw new Error("--host must be an IP address or hostname.");
  const callbackHost = serviceUrl ? undefined : ["0.0.0.0", "::"].includes(host) ? lanAddress() : host;
  const urlHost = callbackHost?.includes(":") ? `[${callbackHost}]` : callbackHost;
  const defaults = [];
  if (!endpoint) defaults.push("-e", "http://localhost:8088/api/messages");
  if (!hostOption) defaults.push("--host", host);
  if (!portOption) defaults.push("--port", String(port));
  if (!serviceUrl) defaults.push("--service-url", `http://${urlHost}:${port}/_connector`);
  return { host, port, defaults };
}

async function checkPort(host, port) {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, resolve);
  });
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const { host, port, defaults } = playgroundConfig(process.argv.slice(2));
    // Playground falls back to another port when busy, leaving an explicit callback URL stale.
    await checkPort(host, port);
    if (defaults.length) process.stdout.write(`${defaults.join("\n")}\n`);
  } catch (error) {
    console.error(`error: ${error.message}`);
    process.exitCode = 1;
  }
}
