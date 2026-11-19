# GPU probe add-on for HAOS (amd64): a GPU consumer with no kernel rights.
# Same base and the same NVIDIA userspace as whisper-cuda, without the kernel sources.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS driver

# Must match the kernel module that whisper-cuda loads. Pinned on purpose (last Pascal branch).
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
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROBE_VERSION=${BUILD_VERSION}

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=driver /opt/nvidia /opt/nvidia
RUN ln -s /opt/nvidia/bin/nvidia-smi /usr/local/bin/nvidia-smi \
    && echo /opt/nvidia/lib > /etc/ld.so.conf.d/nvidia.conf \
    && ldconfig \
    && ln -sf libcuda.so.1 /opt/nvidia/lib/libcuda.so

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/*.py

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="GPU Probe" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

# No HEALTHCHECK on purpose: the Supervisor would wait for "healthy" (up to 120 s) before moving
# on from the initialize stage.
CMD ["python3", "/usr/local/bin/probe.py"]
