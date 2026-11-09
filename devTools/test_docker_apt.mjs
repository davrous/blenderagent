import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";

const dockerfile = readFileSync(new URL("../Dockerfile", import.meta.url), "utf8");
const block = dockerfile.match(/^RUN ([\s\S]*?)\n\n/m)?.[1];
assert.ok(block, "Dockerfile must contain the system-package RUN block");
const script = block.replace(/^\s*#.*\n/gm, "").replace(/\\\n/g, "");

// Execute the Dockerfile's shell logic with no package, filesystem or sleep effects.
const mocks = `
set -f
updates=0
alias apt-get=apt_get
apt_get() {
    printf 'apt:%s\\n' "$*"
    case " $* " in
        *" update "*)
            updates=$((updates + 1))
            if [ "$updates" -le "$UPDATE_FAILURES" ]; then
                echo "File has unexpected size. Mirror sync in progress?" >&2
                return 100
            fi
            ;;
        *" install "*) return "$INSTALL_STATUS" ;;
        *) echo "Unexpected apt-get command" >&2; return 99 ;;
    esac
}
rm() {
    printf 'cleanup:%s\\n' "$*"
    return "$CLEANUP_STATUS"
}
sleep() {
    printf 'sleep:%s\\n' "$*"
    return "$SLEEP_STATUS"
}
`;

const cases = [
  { name: "immediate success", failures: 0, status: 0, updates: 1, install: true },
  { name: "mirror recovery", failures: 2, status: 0, updates: 3, install: true },
  { name: "last-attempt recovery", failures: 4, status: 0, updates: 5, install: true },
  { name: "exhausted retries", failures: 5, status: 100, updates: 5, install: false },
  { name: "install failure", failures: 0, installStatus: 100, status: 100, updates: 1, install: true },
  { name: "index cleanup failure", failures: 1, cleanupStatus: 1, status: 1, updates: 1, install: false },
  { name: "sleep failure", failures: 1, sleepStatus: 1, status: 1, updates: 1, install: false },
];

for (const scenario of cases) {
  const result = spawnSync("/bin/sh", ["-c", mocks + script], {
    encoding: "utf8",
    timeout: 5000,
    env: {
      ...process.env,
      UPDATE_FAILURES: String(scenario.failures),
      INSTALL_STATUS: String(scenario.installStatus ?? 0),
      CLEANUP_STATUS: String(scenario.cleanupStatus ?? 0),
      SLEEP_STATUS: String(scenario.sleepStatus ?? 0),
    },
  });
  assert.ifError(result.error);
  assert.equal(result.status, scenario.status, `${scenario.name}: ${result.stderr}`);
  const events = result.stdout.trim().split("\n");
  const expected = [];
  for (let attempt = 1; attempt <= scenario.updates; attempt++) {
    expected.push("apt:-o Acquire::Retries=3 update --error-on=any");
    if (attempt <= scenario.failures && attempt < 5) {
      expected.push("cleanup:-rf /var/lib/apt/lists/*");
      if (!scenario.cleanupStatus) expected.push(`sleep:${attempt * 15}`);
    }
  }
  const installEvents = events.filter((event) => event.startsWith("apt:") && event.includes(" install "));
  assert.equal(installEvents.length, Number(scenario.install), scenario.name);
  if (scenario.install) {
    assert.match(installEvents[0], /^apt:-o Acquire::Retries=3 install -y --no-install-recommends /);
    expected.push(installEvents[0]);
    if (!scenario.installStatus) expected.push("cleanup:-rf /var/lib/apt/lists/*");
  }
  assert.deepEqual(events, expected, scenario.name);
  if (scenario.failures > 0) assert.match(result.stderr, /File has unexpected size/);
  if (scenario.failures === 5) assert.match(result.stderr, /failed after 5 attempts; aborting build/);
  console.log(`PASS ${scenario.name}`);
}
