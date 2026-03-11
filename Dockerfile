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
    # Utilities
    wget netcat-openbsd curl ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# ── 1b. Azure CLI ──
# Required for DefaultAzureCredential → AzureCliCredential when running
# locally in Docker with: -v ~/.azure:/root/.azure:ro
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# ── 2. Install Blender 4.2 LTS ──
# Note: Blender only provides x64 Linux builds, so this image must be
# built with --platform linux/amd64 on Apple Silicon Macs.
ARG BLENDER_VERSION=4.2.10
RUN echo "Downloading Blender ${BLENDER_VERSION}..." \
    && wget -q "https://mirror.clarkson.edu/blender/release/Blender4.2/blender-${BLENDER_VERSION}-linux-x64.tar.xz" -O /tmp/blender.tar.xz \
    && mkdir -p /opt/blender \
    && tar -xf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 \
    && rm /tmp/blender.tar.xz \
    && ln -s /opt/blender/blender /usr/local/bin/blender \
    && echo "Blender installed successfully"

# ── 3. Python application dependencies ──
WORKDIR /app

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 4. Copy application code ──
COPY main.py .
COPY blender_startup.py .
COPY blender_connection.py .
COPY entrypoint.sh .
COPY agent.yaml .

RUN chmod +x /app/entrypoint.sh

# ── 5. Expose ports ──
# 8088 = Agent HTTP server
# 9876 = Blender MCP socket (internal)
EXPOSE 8088

# ── 6. Start everything ──
ENTRYPOINT ["/app/entrypoint.sh"]
