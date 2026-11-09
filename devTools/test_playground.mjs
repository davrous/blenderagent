import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { playgroundConfig } from "./playground_args.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const interfaces = {
  lo0: [{ family: "IPv4", address: "127.0.0.1", internal: true }],
  utun0: [{ family: "IPv4", address: "10.10.10.10", internal: false }],
  en0: [
    { family: "IPv6", address: "fe80::1", internal: false },
    { family: "IPv4", address: "192.0.2.10", internal: false },
  ],
};

const defaultConfig = playgroundConfig([], interfaces);
assert.deepEqual(defaultConfig, {
  host: "192.0.2.10",
  port: 56150,
  defaults: ["-e", "http://localhost:8088/api/messages", "--host", "192.0.2.10",
    "--port", "56150", "--service-url", "http://192.0.2.10:56150/_connector"],
});
for (const portArgs of [
  ["--port", "56151"], ["-p", "56151"], ["--port=56151"], ["-p56151"], ["-p=56151"],
]) {
  const config = playgroundConfig(portArgs, interfaces);
  assert.equal(config.port, 56151);
  assert.equal(config.defaults.at(-1), "http://192.0.2.10:56151/_connector");
  assert.ok(!config.defaults.includes("--port"));
}
assert.deepEqual(playgroundConfig([
  "--host=192.0.2.20", "-p", "56151", "--su", "http://192.0.2.20:56151/_connector",
  "--app-endpoint=http://localhost:8088/api/messages",
], {}).defaults, []);
assert.ok(!playgroundConfig(["-ehttp://localhost:8088/api/messages"], interfaces).defaults.includes("-e"));
assert.equal(playgroundConfig(["--host", "192.0.2.20"], {}).defaults.at(-1),
  "http://192.0.2.20:56150/_connector");
assert.equal(playgroundConfig(["--host", "0.0.0.0"], interfaces).defaults.at(-1),
  "http://192.0.2.10:56150/_connector");
assert.equal(playgroundConfig(["--host", "::1"], {}).defaults.at(-1),
  "http://[::1]:56150/_connector");
for (const args of [
  ["--host"], ["--port"], ["--port", "0"], ["--port", "65536"],
  ["--port", "abc"], ["--port", "1.5"], ["--host", "bad\nhost"],
  ["--host", "192.0.2.10", "--host", "192.0.2.20"],
  ["-p", "56150", "--port", "56151"],
  ["--service-url", "http://one", "--su", "http://two"],
]) assert.throws(() => playgroundConfig(args, interfaces), Error);
assert.deepEqual(playgroundConfig(["--", "--host", "ignored"], interfaces), defaultConfig);
assert.throws(() => playgroundConfig([], { lo0: interfaces.lo0, utun0: interfaces.utun0 }),
  /No LAN IPv4/);
console.log("PASS LAN selection, VPN exclusion, overrides, matching callback ports and invalid arguments");

const server = createServer();
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;
try {
  const occupied = spawnSync(process.execPath, [
    join(root, "devTools/playground_args.mjs"), "--host", "127.0.0.1", "--port", String(port),
  ], { encoding: "utf8", timeout: 5000 });
  assert.ifError(occupied.error);
  assert.equal(occupied.status, 1);
  assert.match(occupied.stderr, /EADDRINUSE/);
  assert.equal(occupied.stdout, "");
} finally {
  await new Promise((resolve) => server.close(resolve));
}
console.log("PASS occupied port fails explicitly instead of producing a stale callback URL");

const dir = mkdtempSync(join(tmpdir(), "blender-playground-test-"));
try {
  writeFileSync(join(dir, "curl"), '#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_ARGS"\nexit "$CURL_STATUS"\n');
  writeFileSync(join(dir, "agentsplayground"), '#!/bin/sh\nprintf "%s\\n" "$@" > "$PG_ARGS"\nexit "$PG_STATUS"\n');
  chmodSync(join(dir, "curl"), 0o755);
  chmodSync(join(dir, "agentsplayground"), 0o755);
  for (const scenario of [
    { name: "ready agent", curlStatus: 0, pgStatus: 0 },
    { name: "starting agent", curlStatus: 22, pgStatus: 0 },
    { name: "CLI failure", curlStatus: 0, pgStatus: 37 },
  ]) {
    const args = ["--host", "127.0.0.1", "--port", String(port),
      "--service-url", `http://127.0.0.1:${port}/_connector`,
      "--conversation-id", "a value with spaces", "--disable-telemetry"];
    const result = spawnSync("/bin/bash", [join(root, "blenderagent"), "playground", ...args], {
      encoding: "utf8", timeout: 5000,
      env: {
        ...process.env, PATH: `${dir}:${process.env.PATH}`,
        CURL_ARGS: join(dir, "curl-args"), PG_ARGS: join(dir, "pg-args"),
        CURL_STATUS: String(scenario.curlStatus), PG_STATUS: String(scenario.pgStatus),
      },
    });
    assert.ifError(result.error);
    assert.equal(result.status, scenario.pgStatus, result.stderr);
    assert.deepEqual(readFileSync(join(dir, "pg-args"), "utf8").trimEnd().split("\n"),
      ["-e", "http://localhost:8088/api/messages", ...args]);
    const probe = readFileSync(join(dir, "curl-args"), "utf8");
    assert.match(probe, /http:\/\/localhost:8088\/readiness/);
    assert.doesNotMatch(probe, /POST|\{\}|api\/messages/);
    assert.equal(result.stderr.includes("the agent is not ready"), scenario.curlStatus !== 0);
    console.log(`PASS bash launcher: ${scenario.name}, non-mutating probe and exact argument forwarding`);
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}
