# NVIDIA driver add-on for HAOS (amd64): builds and loads the kernel modules, monitors the GPU.
# Ubuntu 24.04 ships GCC 13.3; the HAOS 18 kernel is built with GCC 13.4 (same major.minor family).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG BUILD_VERSION=dev
# NVIDIA 580 is the last driver branch for Pascal (GTX 10xx). Pinned on purpose: every GPU
# consumer image ships the same userspace version.
ARG NV_VERSION=580.196.07
ARG NV_SHA256=2189c47fa87ae45b7a166b58c3742dd1629035069cb0fb4103ed375229c55867
# "-no-compat32" or "" (some older releases only ship the plain .run). CI builds other branches.
ARG NV_RUN_SUFFIX=-no-compat32

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

# Kernel + out-of-tree module build toolchain, module tools, status helpers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        acpica-tools bc bison build-essential ca-certificates cpio curl flex gcc-13 jq kmod \
        libelf-dev libssl-dev patch perl python3 util-linux xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

# Proprietary kernel module sources (Pascal is not supported by the open modules) and the
# userspace nvidia-smi needs. No libcuda: this add-on never runs CUDA.
RUN set -eux; \
    cd /tmp; \
    curl -fsSLo nv.run "https://download.nvidia.com/XFree86/Linux-x86_64/${NV_VERSION}/NVIDIA-Linux-x86_64-${NV_VERSION}${NV_RUN_SUFFIX}.run"; \
    echo "${NV_SHA256}  nv.run" | sha256sum -c -; \
    sh nv.run --extract-only --target /tmp/nv; \
    mkdir -p /opt/nvidia/lib; \
    cp -a /tmp/nv/kernel /opt/nvidia/kernel-src; \
    cp "/tmp/nv/libnvidia-ml.so.${NV_VERSION}" /opt/nvidia/lib/; \
    install -m 0755 /tmp/nv/nvidia-smi /usr/local/bin/nvidia-smi; \
    echo /opt/nvidia/lib > /etc/ld.so.conf.d/nvidia.conf; \
    ldconfig; \
    echo "${NV_VERSION}" > /opt/nvidia/VERSION; \
    rm -rf /tmp/nv /tmp/nv.run

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/*.sh

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="NVIDIA Driver" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

# Healthy while the monitor loop runs, whatever the GPU state: a failed build must not make the
# watchdog restart the add-on into another build. The start period covers a build on a new kernel.
HEALTHCHECK --start-period=90m --interval=30s --timeout=5s --retries=3 \
    CMD test -n "$(find /tmp/heartbeat -mmin -1 2>/dev/null)" || exit 1

CMD ["/usr/local/bin/run.sh"]
