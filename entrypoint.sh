#!/bin/bash
set -e

echo "=== Blender Scene Agent - Starting up ==="

# ── 0. Fix Azure CLI read-only mount ──
# When ~/.azure is mounted as :ro, Azure CLI can't write logs/tokens.
# Copy to a writable location and point AZURE_CONFIG_DIR there.
if [ -d /root/.azure ] && ! touch /root/.azure/.writetest 2>/dev/null; then
    echo "Detected read-only Azure config mount, copying auth state to writable location..."
    mkdir -p /tmp/.azure-writable
    # Selective copy: only the files Azure CLI / azure-identity actually need
    # for token reuse. Avoids the multi-minute `cp -r` over Docker Desktop's
    # Windows↔Linux bind-mount bridge, which is very slow for the thousands of
    # small files in ~/.azure/logs, ~/.azure/telemetry, ~/.azure/commands, etc.
    for f in azureProfile.json clouds.config config \
             msal_token_cache.bin msal_token_cache.json \
             service_principal_entries.bin service_principal_entries.json \
             AzureRmContext.json TokenCache.dat versionCheck.json; do
        [ -e "/root/.azure/$f" ] && cp -p "/root/.azure/$f" "/tmp/.azure-writable/" 2>/dev/null || true
    done
    export AZURE_CONFIG_DIR=/tmp/.azure-writable
    echo "AZURE_CONFIG_DIR set to /tmp/.azure-writable (selective copy done)"
else
    rm -f /root/.azure/.writetest 2>/dev/null
fi

# ── 0b. Persistent logging to $HOME ──
# The platform persists $HOME across idle periods. Writing logs there lets
# us troubleshoot session-restore failures after the fact.
PERSISTENT_LOG_DIR="${HOME}/logs"
mkdir -p "$PERSISTENT_LOG_DIR"

# Detect fresh session vs restored session
if [ -f "$PERSISTENT_LOG_DIR/entrypoint.log" ]; then
    echo "=== SESSION RESTORED (previous logs found in $PERSISTENT_LOG_DIR) ==="
    echo "--- $HOME contents at restore time ---"
    ls -laR "$HOME" 2>/dev/null | head -60
    echo "--- end of $HOME listing ---"
else
    echo "=== FRESH SESSION (no previous logs in $PERSISTENT_LOG_DIR) ==="
fi

# Tee all subsequent output to persistent log (append, so prior runs are kept)
exec > >(tee -a "$PERSISTENT_LOG_DIR/entrypoint.log") 2>&1
echo ""
echo "=== entrypoint.sh starting at $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="

# ── 0c. Mark session state as "Blender not ready yet" ──
# Mark blender_ready=false in the persisted session-state file so the agent
# middleware can detect at request time that we are recovering from idle and
# need to wait for the supervisor to bring Blender back up. The Python agent
# will flip this to true once it successfully connects to Blender.
SESSION_STATE_FILE="${HOME}/.blender_session_state"
NOW_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
# Cold start = no prior state file → this is a fresh container (not idle
# recovery). The agent middleware uses this to avoid showing misleading
# "🔄 restarting after being paused" messages on the first request of a
# brand-new container.
if [ -f "$SESSION_STATE_FILE" ]; then
    export BLENDER_COLD_START=0
else
    export BLENDER_COLD_START=1
fi
echo "BLENDER_COLD_START=${BLENDER_COLD_START}"
if [ -f "$SESSION_STATE_FILE" ]; then
    # Use python to do a safe in-place JSON update; fall back to overwrite.
    python3 - <<PYEOF || echo '{"needs_scene_reload":true,"blender_ready":false,"session_started_at":"'"$NOW_UTC"'"}' > "$SESSION_STATE_FILE"
import json, os, sys
p = "$SESSION_STATE_FILE"
try:
    with open(p) as f:
        d = json.load(f)
except Exception:
    d = {}
d["blender_ready"] = False
d["session_started_at"] = "$NOW_UTC"
# Existence of a prior state file means a previous session ran successfully
# (and presumably saved a scene), so subsequent requests should attempt
# scene reload rather than starting fresh.
d.setdefault("needs_scene_reload", True)
with open(p, "w") as f:
    json.dump(d, f)
PYEOF
    echo "Session state file updated (existing): $SESSION_STATE_FILE"
else
    # Fresh container — no previous scene exists, so needs_scene_reload=false.
    echo '{"needs_scene_reload":false,"blender_ready":false,"session_started_at":"'"$NOW_UTC"'"}' > "$SESSION_STATE_FILE"
    echo "Session state file created (fresh): $SESSION_STATE_FILE"
fi

export DISPLAY=:99
XVFB_READY_TIMEOUT=15
BLENDER_SOCKET_TIMEOUT_FIRST=90
BLENDER_SOCKET_TIMEOUT_RESTART=60
BLENDER_MAX_INITIAL_ATTEMPTS=3

XVFB_PID=""
BLENDER_PID=""

# ── helpers ───────────────────────────────────────────

start_xvfb() {
    # Clean up stale Xvfb state. On idle→active restore the container's PID
    # namespace is reset but /tmp is preserved, so the old /tmp/.X99-lock and
    # /tmp/.X11-unix/X99 socket files persist and make Xvfb fail with
    # "Server is already active for display 99". Kill any zombie Xvfb procs
    # too (defensive — should not exist after a fresh container restore).
    pkill -9 -f "Xvfb :99" 2>/dev/null || true
    rm -f /tmp/.X99-lock 2>/dev/null || true
    rm -f /tmp/.X11-unix/X99 2>/dev/null || true

    # Launch Xvfb and stream its stderr/stdout into our log with a prefix
    # so we can see failures (previously redirected to /dev/null).
    ( Xvfb :99 -screen 0 1920x1080x24 2>&1 | sed 's/^/[xvfb] /' ) &
    XVFB_PID=$!
    echo "Xvfb started on display :99 (PID: $XVFB_PID)"
}

wait_for_xvfb() {
    # Actively wait for the Xvfb X11 socket to be accepting connections,
    # rather than relying on a fixed sleep. Probe via AF_UNIX connect to
    # /tmp/.X11-unix/X99 — no extra package needed.
    local waited=0
    while [ $waited -lt $XVFB_READY_TIMEOUT ]; do
        if [ -S /tmp/.X11-unix/X99 ] && \
           python3 -c "import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(1); s.connect('/tmp/.X11-unix/X99'); s.close()" 2>/dev/null; then
            echo "Xvfb display :99 ready (waited ${waited}s)"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "ERROR: Xvfb did not become ready within ${XVFB_READY_TIMEOUT}s"
    return 1
}

start_blender() {
    /opt/blender/blender --python /app/blender_startup.py &
    BLENDER_PID=$!
    echo "Blender started (PID: $BLENDER_PID)"
}

wait_for_blender_socket() {
    local timeout=$1
    local waited=0
    while ! nc -z localhost 9876 2>/dev/null; do
        if [ $waited -ge $timeout ]; then
            return 1
        fi
        # If the Blender process died early, stop waiting.
        if [ -n "$BLENDER_PID" ] && ! kill -0 "$BLENDER_PID" 2>/dev/null; then
            echo "Blender process exited before socket became ready"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "Blender MCP socket server is ready (waited ${waited}s)"
    return 0
}

start_blender_and_wait() {
    # Start Blender once and wait for its socket. Returns 0 on success.
    local timeout=$1
    start_blender
    if wait_for_blender_socket "$timeout"; then
        return 0
    fi
    # Cleanup the failed Blender process so it doesn't leak.
    if [ -n "$BLENDER_PID" ] && kill -0 "$BLENDER_PID" 2>/dev/null; then
        echo "Killing unresponsive Blender (PID: $BLENDER_PID)"
        kill -9 "$BLENDER_PID" 2>/dev/null || true
    fi
    return 1
}

# ── 1. Ensure Azure credentials are available ──
# Do this BEFORE starting the agent server so MSI / az login is ready
# for the first request. This is fast (instant for MSI, interactive for local).
if [ -n "$IDENTITY_ENDPOINT" ] || [ -n "$MSI_ENDPOINT" ]; then
    echo "Hosted environment detected (managed identity available) — skipping Azure CLI login."
elif ! az account show > /dev/null 2>&1; then
    echo ""
    echo "=============================================="
    echo " No valid Azure credentials detected."
    echo " Logging in via device code flow..."
    echo "=============================================="
    echo ""
    az login --use-device-code
fi

# ── 2. Xvfb + Blender startup AND supervision (background) ──
# Start Xvfb and Blender in the background so the agent server can start
# immediately. The protocol library's /readiness endpoint needs the agent
# server on port 8088 to respond — if we block here waiting for Blender
# (up to 90s × 3 = 270s), the platform times out with session_not_ready.
# The blender_connection.py retry logic handles "Blender not ready yet"
# gracefully on the first tool call.
(
    echo "Supervisor: starting initial Xvfb + Blender setup..."

    # ── Initial Xvfb start ──
    start_xvfb
    if ! wait_for_xvfb; then
        echo "Supervisor: restarting Xvfb after failed readiness check..."
        if [ -n "$XVFB_PID" ] && kill -0 "$XVFB_PID" 2>/dev/null; then
            kill -9 "$XVFB_PID" 2>/dev/null || true
        fi
        start_xvfb
        if ! wait_for_xvfb; then
            echo "Supervisor: FATAL — Xvfb could not be started"
            # Don't exit the container; the agent server is still useful for
            # returning error messages. The supervisor loop will keep retrying.
        fi
    fi

    # ── Initial Blender start (with retries) ──
    echo "Supervisor: starting Blender with MCP socket server..."
    set +e
    attempt=1
    while [ $attempt -le $BLENDER_MAX_INITIAL_ATTEMPTS ]; do
        echo "Supervisor: Blender start attempt ${attempt}/${BLENDER_MAX_INITIAL_ATTEMPTS}..."
        if start_blender_and_wait "$BLENDER_SOCKET_TIMEOUT_FIRST"; then
            echo "Supervisor: Blender initial start succeeded (PID: $BLENDER_PID)"
            break
        fi
        echo "Supervisor: Blender did not become ready on attempt ${attempt}"
        attempt=$((attempt + 1))
        if [ -n "$XVFB_PID" ] && ! kill -0 "$XVFB_PID" 2>/dev/null; then
            echo "Supervisor: Xvfb died — restarting before retrying Blender"
            start_xvfb
            wait_for_xvfb || true
        fi
    done
    set -e

    if [ -z "$BLENDER_PID" ] || ! kill -0 "$BLENDER_PID" 2>/dev/null; then
        echo "Supervisor: WARNING — Blender failed initial start after ${BLENDER_MAX_INITIAL_ATTEMPTS} attempts. Will keep retrying."
    fi

    # ── Ongoing supervision loop ──
    while true; do
        # Supervise Xvfb. If it dies, Blender's display is gone too, so
        # we'll restart both.
        if [ -z "$XVFB_PID" ] || ! kill -0 "$XVFB_PID" 2>/dev/null; then
            echo "WARNING: Xvfb (PID: $XVFB_PID) is not running, restarting..."
            start_xvfb
            wait_for_xvfb || echo "WARNING: Xvfb still not ready after restart"
            # Force a Blender restart since the display it was attached to is gone.
            if [ -n "$BLENDER_PID" ] && kill -0 "$BLENDER_PID" 2>/dev/null; then
                echo "Killing Blender (PID: $BLENDER_PID) so it reattaches to the new Xvfb"
                kill -9 "$BLENDER_PID" 2>/dev/null || true
            fi
            BLENDER_PID=""
        fi

        # Supervise Blender.
        if [ -z "$BLENDER_PID" ] || ! kill -0 "$BLENDER_PID" 2>/dev/null; then
            echo "WARNING: Blender (PID: $BLENDER_PID) is not running, restarting..."
            if start_blender_and_wait "$BLENDER_SOCKET_TIMEOUT_RESTART"; then
                echo "Blender supervisor: restart succeeded (PID: $BLENDER_PID)"
            else
                echo "WARNING: Blender did not come back this cycle; will retry shortly"
            fi
        fi

        sleep 5
    done
) &
SUPERVISOR_PID=$!
echo "Xvfb + Blender supervisor started in background (PID: $SUPERVISOR_PID)"

# ── 3. Start the Python Agent server immediately ──
# The agent server binds port 8088 and the protocol library exposes
# /readiness for the platform health check. Starting it NOW (without
# waiting for Blender) ensures /readiness returns 200 quickly and avoids
# session_not_ready timeouts on session restore after idle.
echo "Starting Blender Scene Agent on port 8088..."
exec /app/venv/bin/python /app/main.py --port 8088
