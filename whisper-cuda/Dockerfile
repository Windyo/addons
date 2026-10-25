# Whisper CUDA add-on for HAOS.
# Ubuntu 24.04 ships GCC 13.3; the HAOS 18 kernel is built with GCC 13.4 (same major.minor family).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG BUILD_VERSION=dev
# NVIDIA 580 is the last driver branch for Pascal (GTX 10xx). Pinned on purpose.
ARG NV_VERSION=580.196.07
ARG NV_SHA256=2189c47fa87ae45b7a166b58c3742dd1629035069cb0fb4103ed375229c55867

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

# Kernel + out-of-tree module build toolchain, module tools, status helpers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bc bison build-essential ca-certificates cpio curl flex gcc-13 jq kmod \
        libelf-dev libssl-dev pciutils perl python3 util-linux xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

# NVIDIA driver: proprietary kernel module sources (Pascal is not supported by the open
# modules) and the matching userspace. Kernel module and userspace must be the same version.
RUN set -eux; \
    cd /tmp; \
    curl -fsSLo nv.run "https://download.nvidia.com/XFree86/Linux-x86_64/${NV_VERSION}/NVIDIA-Linux-x86_64-${NV_VERSION}-no-compat32.run"; \
    echo "${NV_SHA256}  nv.run" | sha256sum -c -; \
    sh nv.run --extract-only --target /tmp/nv; \
    mkdir -p /opt/nvidia/lib; \
    cp -a /tmp/nv/kernel /opt/nvidia/kernel-src; \
    for l in libcuda libnvidia-ml libnvidia-ptxjitcompiler libnvidia-nvvm libnvidia-gpucomp; do \
        cp "/tmp/nv/${l}.so.${NV_VERSION}" /opt/nvidia/lib/; \
    done; \
    install -m 0755 /tmp/nv/nvidia-smi /usr/local/bin/nvidia-smi; \
    echo /opt/nvidia/lib > /etc/ld.so.conf.d/nvidia.conf; \
    ldconfig; \
    ln -sf libcuda.so.1 /opt/nvidia/lib/libcuda.so; \
    echo "${NV_VERSION}" > /opt/nvidia/VERSION; \
    rm -rf /tmp/nv /tmp/nv.run

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/*.sh

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="Whisper CUDA" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

CMD ["/usr/local/bin/run.sh"]
