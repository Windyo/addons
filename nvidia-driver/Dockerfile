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
# Second branch in the image, "version:sha256:suffix" ("" for none): 570, to test whether the GPU
# faults are a 580 regression. The driver_branch option picks one at start (nv-branch.sh).
ARG NV_ALT=570.211.01:8b423f751bb7f73a1533c4fed5e97719892b2937abda2828ae0183af3af6314b:-no-compat32

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

# Kernel + out-of-tree module build toolchain, module tools, status helpers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        acpica-tools bc bison build-essential ca-certificates cpio curl flex gcc-13 jq kmod \
        libelf-dev libssl-dev patch perl python3 util-linux xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

# Proprietary kernel module sources (Pascal is not supported by the open modules) and the
# userspace nvidia-smi needs, per branch in /opt/nvidia/branches/<version>/{kernel-src,lib,bin,VERSION}.
# /opt/nvidia/{kernel-src,lib,bin,VERSION} link to the branch in use. No libcuda: this add-on never
# runs CUDA.
RUN set -eux; \
    fetch() { \
        v=$1; d=/opt/nvidia/branches/$1; \
        curl -fsSLo /tmp/nv.run "https://download.nvidia.com/XFree86/Linux-x86_64/${v}/NVIDIA-Linux-x86_64-${v}${3:-}.run"; \
        echo "$2  /tmp/nv.run" | sha256sum -c -; \
        sh /tmp/nv.run --extract-only --target /tmp/nv; \
        mkdir -p "$d/lib" "$d/bin"; \
        cp -a /tmp/nv/kernel "$d/kernel-src"; \
        cp "/tmp/nv/libnvidia-ml.so.${v}" "$d/lib/"; \
        ln -s "libnvidia-ml.so.${v}" "$d/lib/libnvidia-ml.so.1"; \
        install -m 0755 /tmp/nv/nvidia-smi "$d/bin/nvidia-smi"; \
        echo "$v" > "$d/VERSION"; \
        rm -rf /tmp/nv /tmp/nv.run; \
    }; \
    fetch "$NV_VERSION" "$NV_SHA256" "$NV_RUN_SUFFIX"; \
    if [ -n "$NV_ALT" ] && [ "${NV_ALT%%:*}" != "$NV_VERSION" ]; then fetch $(echo "$NV_ALT" | tr : ' '); fi; \
    for n in VERSION kernel-src lib bin; do ln -s "branches/${NV_VERSION}/$n" "/opt/nvidia/$n"; done; \
    ln -s /opt/nvidia/bin/nvidia-smi /usr/local/bin/nvidia-smi; \
    echo /opt/nvidia/lib > /etc/ld.so.conf.d/nvidia.conf; \
    ldconfig

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
