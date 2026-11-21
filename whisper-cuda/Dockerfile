# Whisper CUDA add-on for HAOS: speech-to-text only. The NVIDIA Driver add-on loads the kernel
# module; this image carries only the matching userspace.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS driver

# Must match the kernel module the NVIDIA Driver add-on loads (checked at start). Pinned on purpose.
ARG NV_VERSION=580.196.07
ARG NV_SHA256=2189c47fa87ae45b7a166b58c3742dd1629035069cb0fb4103ed375229c55867

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

# Userspace only. No nvidia-modprobe: a consumer never loads the driver or creates nodes.
RUN set -eux; \
    cd /tmp; \
    curl -fsSLo nv.run "https://download.nvidia.com/XFree86/Linux-x86_64/${NV_VERSION}/NVIDIA-Linux-x86_64-${NV_VERSION}-no-compat32.run"; \
    echo "${NV_SHA256}  nv.run" | sha256sum -c -; \
    sh nv.run --extract-only --target /tmp/nv; \
    mkdir -p /opt/nvidia/lib /opt/nvidia/bin; \
    for l in libcuda libnvidia-ml libnvidia-ptxjitcompiler libnvidia-nvvm libnvidia-gpucomp; do \
        cp "/tmp/nv/${l}.so.${NV_VERSION}" /opt/nvidia/lib/; \
    done; \
    install -m 0755 /tmp/nv/nvidia-smi /opt/nvidia/bin/nvidia-smi; \
    echo "${NV_VERSION}" > /opt/nvidia/VERSION; \
    rm -rf /tmp/nv /tmp/nv.run

FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG BUILD_VERSION=dev

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl jq netcat-openbsd python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=driver /opt/nvidia /opt/nvidia
RUN ln -s /opt/nvidia/bin/nvidia-smi /usr/local/bin/nvidia-smi \
    && echo /opt/nvidia/lib > /etc/ld.so.conf.d/nvidia.conf \
    && ldconfig \
    && ln -sf libcuda.so.1 /opt/nvidia/lib/libcuda.so

# Whisper server + benchmark. CTranslate2's x86 wheel is built without cuDNN, so cuBLAS 12 is the
# only CUDA library needed (cuDNN >= 9.11 dropped Pascal anyway). cuBLAS 12.9 still ships sm_61.
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.lock \
    && echo /opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib > /etc/ld.so.conf.d/cublas.conf \
    && ldconfig \
    && rm /tmp/requirements.lock
ENV HF_HOME=/data/hf \
    HF_HUB_DISABLE_TELEMETRY=1

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/*.sh /usr/local/bin/*.py

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="Whisper CUDA" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

# Healthy once the Wyoming server answers. The long start period covers the first model download
# and an optional benchmark, so they are never interrupted.
HEALTHCHECK --start-period=90m --interval=60s --timeout=5s --retries=5 \
    CMD nc -z -w 3 127.0.0.1 10300 || exit 1

CMD ["/usr/local/bin/run.sh"]
