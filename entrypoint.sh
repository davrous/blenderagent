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
# On Windows, the MSAL token cache is encrypted (DPAPI) and unreadable
# inside a Linux container. If no valid credential is found, prompt
# the user to log in interactively via device code.
if ! az account show > /dev/null 2>&1; then
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
exec /app/venv/bin/python /app/main.py --port 8088
