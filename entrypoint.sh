#!/bin/bash
set -e

echo "=== Blender Scene Agent - Starting up ==="

# ── 0. Fix Azure CLI read-only mount ──
# When ~/.azure is mounted as :ro, Azure CLI can't write logs/tokens.
# Copy to a writable location and point AZURE_CONFIG_DIR there.
if [ -d /root/.azure ] && ! touch /root/.azure/.writetest 2>/dev/null; then
    echo "Detected read-only Azure config mount, copying to writable location..."
    cp -r /root/.azure /tmp/.azure-writable
    export AZURE_CONFIG_DIR=/tmp/.azure-writable
    echo "AZURE_CONFIG_DIR set to /tmp/.azure-writable"
else
    rm -f /root/.azure/.writetest 2>/dev/null
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

# ── 1. Start Xvfb and wait until the display is actually accepting clients ──
start_xvfb
if ! wait_for_xvfb; then
    # Xvfb failed to come up — surface the error but try once more before giving up.
    echo "Restarting Xvfb after failed readiness check..."
    if [ -n "$XVFB_PID" ] && kill -0 "$XVFB_PID" 2>/dev/null; then
        kill -9 "$XVFB_PID" 2>/dev/null || true
    fi
    start_xvfb
    if ! wait_for_xvfb; then
        echo "FATAL: Xvfb could not be started"
        exit 1
    fi
fi

# ── 2. Start Blender (GUI renders to Xvfb virtual display) ──
# NOTE: Do NOT use --background. Blender needs its event loop running
# so that bpy.app.timers work and the socket server stays alive.
echo "Starting Blender with MCP socket server..."

# Retry the *initial* launch a few times instead of exiting the container,
# so a transient cold-start race (e.g. display still warming up) doesn't
# turn into a Foundry session_not_ready / HTTP 424.
set +e
attempt=1
while [ $attempt -le $BLENDER_MAX_INITIAL_ATTEMPTS ]; do
    echo "Blender start attempt ${attempt}/${BLENDER_MAX_INITIAL_ATTEMPTS}..."
    if start_blender_and_wait "$BLENDER_SOCKET_TIMEOUT_FIRST"; then
        break
    fi
    echo "Blender did not become ready on attempt ${attempt}"
    attempt=$((attempt + 1))
    # If Xvfb itself died, bring it back before the next try.
    if [ -n "$XVFB_PID" ] && ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "Xvfb is no longer running — restarting it before retrying Blender"
        start_xvfb
        wait_for_xvfb || true
    fi
done
set -e

if [ -z "$BLENDER_PID" ] || ! kill -0 "$BLENDER_PID" 2>/dev/null; then
    echo "WARNING: Blender failed to start after ${BLENDER_MAX_INITIAL_ATTEMPTS} attempts."
    echo "         Starting agent server anyway; the supervisor will keep retrying."
fi

# ── 3. Ensure Azure credentials are available ──
# In Foundry Hosted, managed identity is injected automatically via
# IDENTITY_ENDPOINT / MSI_ENDPOINT — no Azure CLI login needed.
# Only fall back to device-code login for local Docker development.
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

# ── 4. Start the Python Agent server ──
# Authentication is handled by DefaultAzureCredential in Python.
# In Foundry Hosted, managed identity is injected automatically.
echo "Starting Blender Scene Agent on port 8088..."

# ── 5. Xvfb + Blender process supervision ──
# Monitor both Xvfb and Blender in the background and restart whichever
# dies. The agent server keeps running; the retry logic in
# blender_connection.py will reconnect once Blender's socket is back up.
(
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
echo "Blender supervisor started (PID: $SUPERVISOR_PID)"

exec /app/venv/bin/python /app/main.py --port 8088
