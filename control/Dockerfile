# MDD Sim Gateway control-plane image: FastAPI manager + React WebUI, in one container.
#
# Build context = repo ROOT (build with: docker build -f control/Dockerfile -t mdd-sim-gateway/control .)
# Multi-arch (amd64/arm64) and fully from source: the WebUI is compiled in stage 1, pcsc-lite
# and the Python deps in stage 2 — no Node or Python toolchain needed on the host. Stage 3 is
# the shipped image: it inherits NONE of the build toolchain, only the compiled artifacts
# (pcsc-lite runtime lib + site-packages) are copied in.
#
# Runtime model: runs PRIVILEGED with the host Docker socket (/var/run/docker.sock) and the
# host pcscd socket (/run/pcscd) bind-mounted; it launches/《manages》sibling engine containers
# via the Docker SDK and reads the physical reader via pyscard. The repo layout is mirrored
# under /app so run.py (`from app import ...`) resolves.

# ---------- stage 1: build the WebUI ----------
# The output is architecture-independent static content. Keep this stage on the native build
# platform so an amd64 GitHub runner does not run npm under ARM64 QEMU during release builds.
FROM --platform=$BUILDPLATFORM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS webui
WORKDIR /webui
# Install deps against the lockfile first (better layer caching), then build.
COPY webui/package.json webui/package-lock.json ./
RUN npm ci
COPY webui/ ./
RUN npm run build

# Compose's GitHub binary endpoint intermittently times out even when its metadata
# and Docker Hub are healthy. The official image contains the exact same release
# binaries; pin its index and still verify our existing per-architecture hashes.
FROM docker/compose-bin:v2.32.4@sha256:2f9a25e681af058b800a39f7f6bc847d7933f06bbc1acd93479d2a2a40d0fa7a AS compose-bin

# ---------- stage 2: native build (pcsc-lite + python deps) ----------
# Everything that needs a C toolchain is compiled here and only the *artifacts* are copied into
# the runtime stage, so swig/gcc/meson/ninja/flex/perl/wget never reach the shipped image.
# NOTE: no --platform here on purpose — this stage compiles native binaries (pcsc-lite, pyscard)
# and must run on the TARGET platform. release.yml builds each arch on a native runner.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS deps
# pcsc-lite client library, built from source and pinned to the SAME version as the host pcscd
# and the engine image (PCSC_VERSION). pyscard loads libpcsclite.so.1 at runtime to detect the
# reader via the mounted host /run/pcscd socket — matching versions avoids protocol mismatch.
ARG PCSC_VERSION=2.3.3
ARG PCSC_SHA256=00b667aa71504ed1d39a48ad377de048c70dbe47229e8c48a3239ab62979c70f
ARG LPAC_COMMIT=c2fcf5e4b21c712d54e35a11da2ad9ad134fb821
ARG LPAC_SHA256=f3e92f08d10121cf3633cce66853ba1260eb5daa35c594f903cc2720fc11f9ee
ARG DOCKER_CLI_VERSION=27.5.1
ARG DOCKER_CLI_SHA256_AMD64=4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640
ARG DOCKER_CLI_SHA256_ARM64=e6b53725a73763ab3f988c73f8772eaed429754c1a579db5ff11f21990fd1817
ARG COMPOSE_SHA256_AMD64=ed1917fb54db184192ea9d0717bcd59e3662ea79db48bff36d3475516c480a6b
ARG COMPOSE_SHA256_ARM64=0c4591cf3b1ed039adcd803dbbeddf757375fc08c11245b0154135f838495a2f
COPY patches/lpac/01_pcsc_reader_selection.patch /tmp/mdd-lpac.patch
RUN apt-get update && apt-get install -y --no-install-recommends \
        swig gcc python3-dev pkg-config build-essential cmake \
        libcurl4-openssl-dev libpcsclite-dev patch \
        wget ca-certificates meson ninja-build flex perl \
    && cd /tmp \
    && wget -qO pcsc.tar.gz "https://codeload.github.com/LudovicRousseau/PCSC/tar.gz/refs/tags/${PCSC_VERSION}" \
    && echo "${PCSC_SHA256}  pcsc.tar.gz" | sha256sum -c - \
    && tar xf pcsc.tar.gz && cd "PCSC-${PCSC_VERSION}" \
    && meson setup builddir --prefix=/usr -Dpolkit=false -Dlibsystemd=false -Dlibudev=false \
    && ninja -C builddir \
    && ninja -C builddir install && ldconfig \
    && DESTDIR=/pcsc-runtime ninja -C builddir install \
    && rm -rf /pcsc-runtime/usr/include /pcsc-runtime/usr/share /pcsc-runtime/usr/bin \
              /pcsc-runtime/usr/sbin /pcsc-runtime/etc /pcsc-runtime/var \
    && find /pcsc-runtime -name pkgconfig -type d -prune -exec rm -rf {} + \
    && find /pcsc-runtime -name '*.la' -delete \
    && find /pcsc-runtime ! -type d \
    && rm -rf /tmp/pcsc.tar.gz "/tmp/PCSC-${PCSC_VERSION}" \
    && cd /tmp \
    && wget -qO lpac.tar.gz \
       "https://codeload.github.com/estkme-group/lpac/tar.gz/${LPAC_COMMIT}" \
    && echo "${LPAC_SHA256}  lpac.tar.gz" | sha256sum -c - \
    && tar xf lpac.tar.gz && cd "lpac-${LPAC_COMMIT}" \
    && patch -p1 < /tmp/mdd-lpac.patch \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
         -DLPAC_WITH_APDU_PCSC=ON -DLPAC_WITH_HTTP_CURL=ON \
         -DLPAC_WITH_APDU_AT=OFF -DLPAC_WITH_APDU_QMI=OFF \
         -DLPAC_WITH_APDU_QMI_QRTR=OFF -DLPAC_WITH_APDU_MBIM=OFF \
         -DLPAC_WITH_APDU_GBINDER=OFF \
    && cmake --build build --parallel \
    && DESTDIR=/lpac-runtime cmake --install build \
    && rm -rf /var/lib/apt/lists/*

# The detached updater needs only the Docker client and Compose plugin. Both are checksummed
# native binaries; no daemon or host package is installed or modified.
COPY --from=compose-bin /docker-compose /docker-tools/cli-plugins/docker-compose
RUN architecture="$(dpkg --print-architecture)" \
    && case "$architecture" in \
         amd64) docker_arch=x86_64; docker_sum="$DOCKER_CLI_SHA256_AMD64"; compose_sum="$COMPOSE_SHA256_AMD64" ;; \
         arm64) docker_arch=aarch64; docker_sum="$DOCKER_CLI_SHA256_ARM64"; compose_sum="$COMPOSE_SHA256_ARM64" ;; \
         *) echo "unsupported Docker CLI architecture: $architecture" >&2; exit 1 ;; \
       esac \
    && mkdir -p /docker-tools/cli-plugins \
    && wget -qO /tmp/docker.tgz "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
    && echo "$docker_sum  /tmp/docker.tgz" | sha256sum -c - \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && cp /tmp/docker/docker /docker-tools/docker \
    && echo "$compose_sum  /docker-tools/cli-plugins/docker-compose" | sha256sum -c - \
    && chmod 0755 /docker-tools/docker /docker-tools/cli-plugins/docker-compose

# pyscard has no prebuilt wheel and is compiled here against the pcsc-lite installed above.
# --root keeps the console-script shebangs pointing at the FINAL /usr/local/bin/python.
COPY control/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --prefix=/usr/local --root=/python-deps -r /tmp/requirements.txt

# ---------- stage 3: python runtime ----------
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
ARG MDD_VERSION=dev
# Self-built pcsc-lite runtime library (see stage 2) + the prebuilt python site-packages.
COPY --from=deps /pcsc-runtime/ /
COPY --from=deps /python-deps/usr/local/ /usr/local/
COPY --from=deps /lpac-runtime/usr/local/bin/lpac /usr/local/bin/lpac
COPY --from=deps /docker-tools/docker /usr/local/bin/docker
COPY --from=deps /docker-tools/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
# modemmanager (mmcli) is the only runtime OS dependency; ldconfig registers libpcsclite.so.1.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libcurl4t64 modemmanager \
    && printf '/usr/lib64\n' > /etc/ld.so.conf.d/mdd-pcsc.conf \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*
# The control plane starts without Pillow or pi-heif on purpose -- MMS picture conversion then
# simply stops -- so an image whose wheels install but do not load must fail here instead.
RUN python -c "import PIL.Image, pi_heif"

WORKDIR /app/control

# App code + built WebUI, mirroring the repo layout under /app.
COPY control/ /app/control/
COPY host/mdd_update.py /app/host/mdd_update.py
COPY host/mdd_container_update.py /app/host/mdd_container_update.py
COPY VERSION /app/VERSION
COPY --from=webui /webui/dist /app/webui/dist

# Defaults; the installer overrides MDD_DATA/MDD_HOST_DATA/MDD_ADVERTISE_ADDR/port.
ENV MDD_WEBUI=/app/webui/dist \
    MDD_DATA=/data \
    MDD_HTTP_PORT=8443 \
    PYTHONUNBUFFERED=1

EXPOSE 8443
LABEL io.mdd-sim-gateway.managed="true" \
      io.mdd-sim-gateway.component="control" \
      org.opencontainers.image.version="${MDD_VERSION}" \
      org.opencontainers.image.title="MDD Sim Gateway control plane"
# Each check is a docker exec that starts a fresh Python: on a Raspberry Pi the three
# containers' checks cost about a fifth of a core at 10 s. The first check now comes
# 30 s after start. --start-interval would keep starts fast, but Docker before 25
# rejects it at build time, and install.sh builds this image with the host's Docker.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c 'import socket; s=socket.create_connection(("127.0.0.1",8443),2); s.close()'
ENTRYPOINT ["python", "run.py"]
