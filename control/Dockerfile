# MDD Sim Gateway control-plane image: FastAPI manager + React WebUI, in one container.
#
# Build context = repo ROOT (build with: docker build -f control/Dockerfile -t mdd-sim-gateway/control .)
# Multi-arch (amd64/arm64) and fully from source: the WebUI is compiled in stage 1, Python
# deps are installed in stage 2 — no Node or Python toolchain needed on the host.
#
# Runtime model: runs PRIVILEGED with the host Docker socket (/var/run/docker.sock) and the
# host pcscd socket (/run/pcscd) bind-mounted; it launches/《manages》sibling engine containers
# via the Docker SDK and reads the physical reader via pyscard. The repo layout is mirrored
# under /app so run.py (`from app import ...`) resolves.

# ---------- stage 1: build the WebUI ----------
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS webui
WORKDIR /webui
# Install deps against the lockfile first (better layer caching), then build.
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ ./
RUN npm run build

# ---------- stage 2: python runtime ----------
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
ARG MDD_VERSION=dev
# pcsc-lite client library, built from source and pinned to the SAME version as the host pcscd
# and the engine image (PCSC_VERSION). pyscard loads libpcsclite.so.1 at runtime to detect the
# reader via the mounted host /run/pcscd socket — matching versions avoids protocol mismatch.
ARG PCSC_VERSION=2.3.3
ARG PCSC_SHA256=00b667aa71504ed1d39a48ad377de048c70dbe47229e8c48a3239ab62979c70f
RUN apt-get update && apt-get install -y --no-install-recommends \
        swig gcc python3-dev pkg-config \
        wget ca-certificates meson ninja-build flex perl \
        modemmanager \
    && cd /tmp \
    && wget -qO pcsc.tar.gz "https://github.com/LudovicRousseau/PCSC/archive/refs/tags/${PCSC_VERSION}.tar.gz" \
    && echo "${PCSC_SHA256}  pcsc.tar.gz" | sha256sum -c - \
    && tar xf pcsc.tar.gz && cd "PCSC-${PCSC_VERSION}" \
    && meson setup builddir --prefix=/usr -Dpolkit=false -Dlibsystemd=false -Dlibudev=false \
    && ninja -C builddir && ninja -C builddir install && ldconfig \
    && rm -rf /tmp/pcsc.tar.gz "/tmp/PCSC-${PCSC_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/control
COPY control/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y swig gcc python3-dev pkg-config meson ninja-build flex \
    && apt-get autoremove -y

# App code + built WebUI, mirroring the repo layout under /app.
COPY control/ /app/control/
COPY VERSION /app/VERSION
COPY --from=webui /webui/dist /app/webui/dist

# Defaults; the installer overrides MDD_DATA/MDD_HOST_DATA/MDD_ADVERTISE_ADDR/port.
ENV MDD_WEBUI=/app/webui/dist \
    MDD_DATA=/data \
    MDD_HTTP_PORT=8443 \
    PYTHONUNBUFFERED=1

EXPOSE 8443
LABEL io.mdd-sim-gateway.managed="true" \
      org.opencontainers.image.version="${MDD_VERSION}" \
      org.opencontainers.image.title="MDD Sim Gateway control plane"
ENTRYPOINT ["python", "run.py"]
