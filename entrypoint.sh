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

# ── 1. Start Xvfb (virtual frame buffer) ──
export DISPLAY=:99
Xvfb :99 -screen 0 1920x1080x24 2>/dev/null &
XVFB_PID=$!
echo "Xvfb started on display :99 (PID: $XVFB_PID)"

# Wait a moment for Xvfb to initialize
sleep 2

# ── 2. Start Blender (GUI renders to Xvfb virtual display) ──
# NOTE: Do NOT use --background. Blender needs its event loop running
# so that bpy.app.timers work and the socket server stays alive.
echo "Starting Blender with MCP socket server..."
/opt/blender/blender \
    --python /app/blender_startup.py \
    &
BLENDER_PID=$!
echo "Blender started (PID: $BLENDER_PID)"

# ── 3. Wait for the Blender socket server to be ready ──
echo "Waiting for Blender MCP socket server on port 9876..."
MAX_WAIT=60
WAITED=0
while ! nc -z localhost 9876 2>/dev/null; do
    if [ $WAITED -ge $MAX_WAIT ]; then
        echo "ERROR: Blender socket server did not start within ${MAX_WAIT}s"
        exit 1
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done
echo "Blender MCP socket server is ready (waited ${WAITED}s)"

# ── 4. Ensure Azure credentials are available ──
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

# ── 5. Start the Python Agent server ──
# Authentication is handled by DefaultAzureCredential in Python.
# In Foundry Hosted, managed identity is injected automatically.
echo "Starting Blender Scene Agent on port 8088..."

# ── 6. Blender process supervision ──
# Monitor Blender in the background and restart it if it crashes.
# The agent server keeps running; the retry logic in blender_connection.py
# will reconnect automatically once Blender's socket server is back up.
(
    while true; do
        if ! kill -0 "$BLENDER_PID" 2>/dev/null; then
            echo "WARNING: Blender process (PID: $BLENDER_PID) exited unexpectedly, restarting..."
            /opt/blender/blender --python /app/blender_startup.py &
            BLENDER_PID=$!
            echo "Blender restarted (PID: $BLENDER_PID)"
            # Wait for socket server to come back
            WAITED=0
            while ! nc -z localhost 9876 2>/dev/null; do
                if [ $WAITED -ge $MAX_WAIT ]; then
                    echo "ERROR: Blender socket server did not recover within ${MAX_WAIT}s"
                    break
                fi
                sleep 1
                WAITED=$((WAITED + 1))
            done
            if nc -z localhost 9876 2>/dev/null; then
                echo "Blender socket server recovered (waited ${WAITED}s)"
            fi
        fi
        sleep 5
    done
) &
SUPERVISOR_PID=$!
echo "Blender supervisor started (PID: $SUPERVISOR_PID)"

exec /app/venv/bin/python /app/main.py --port 8088
