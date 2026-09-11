# =====================================================
# Blender Scene Agent - Docker Image
# Runs headless Blender + AI Agent in a single container
# =====================================================

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1

# ── 1. System packages ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python 3 + pip
    python3 python3-pip python3-venv \
    # Virtual framebuffer for headless Blender GUI
    xvfb \
    # Blender dependencies
    libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 \
    libgl1-mesa-dri libegl1 libsm6 libxkbcommon0 \
    libxrandr2 libxinerama1 libxcursor1 \
    # Azure Speech SDK runtime dependency (ALSA) for the voice path
    libasound2t64 \
    # Utilities
    wget netcat-openbsd curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# ── 1b. Azure CLI ──
# Required for DefaultAzureCredential → AzureCliCredential when running
# locally in Docker with: -v ~/.azure:/root/.azure:ro
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# ── 2. Install Blender 4.4 ──
# Note: Blender only provides x64 Linux builds, so this image must be
# built with --platform linux/amd64 on Apple Silicon Macs.
ARG BLENDER_VERSION=4.4.3
RUN echo "Downloading Blender ${BLENDER_VERSION}..." \
    && wget -q "https://download.blender.org/release/Blender4.4/blender-${BLENDER_VERSION}-linux-x64.tar.xz" -O /tmp/blender.tar.xz \
    && mkdir -p /opt/blender \
    && tar -xf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
    && rm /tmp/blender.tar.xz \
    && ln -s /opt/blender/blender /usr/local/bin/blender \
    && echo "Blender installed successfully"

# ── 3. Python application dependencies ──
WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
# Microsoft-managed development environments require packages to flow through
# the approved feed proxy, which enforces package-age policy. The host's global
# pip.conf is not copied into this Ubuntu image, so configure the same index
# explicitly instead of falling through to blocked files.pythonhosted.org URLs.
ARG PIP_INDEX_URL=https://packagefeedproxy.microsoft.io/pypi/simple/
ENV PIP_INDEX_URL=$PIP_INDEX_URL \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt requirements.lock ./
# Required prereleases are pinned explicitly in requirements.txt. Do not use
# global `--pre`: it also selects newly-published beta transitive dependencies.
RUN pip install --no-cache-dir --retries "$PIP_RETRIES" --timeout "$PIP_DEFAULT_TIMEOUT" -r requirements.lock

# ── 4. Copy application code ──
COPY main.py .
COPY voice_pipeline.py .
COPY activity_bridge.py .
COPY conversation_telemetry.py .
COPY blender_startup.py .
COPY blender_connection.py .
COPY scene_manager.py .
COPY entrypoint.sh .
COPY agent.yaml .

RUN chmod +x /app/entrypoint.sh

# ── 5. Expose ports ──
# 8088 = Agent HTTP server
# 8089 = Voice WebSocket (invocations_ws), used when ENABLE_VOICE is on
# 9876 = Blender MCP socket (internal)
EXPOSE 8088
EXPOSE 8089

# ── 6. Start everything ──
ENTRYPOINT ["/app/entrypoint.sh"]
