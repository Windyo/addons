# GPU diagnostic add-on for HAOS (amd64). Built locally by the Supervisor, but nothing is compiled
# here: the sm_61 binaries come from a CI release (.github/workflows/gpu-diag-bins.yaml).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS fetch

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl zstd \
    && rm -rf /var/lib/apt/lists/*

ARG BINS_RELEASE=gpu-diag-bins-3
ARG BINS_SHA256=d602ee75b24a2a4ca8fe8309ffa8ef8d6e1216a6db6b1cc5e9743e51d34a13e4
RUN set -eux; \
    curl -fsSLo /tmp/bins.tar.zst "https://github.com/windyo/addons/releases/download/${BINS_RELEASE}/gpu-diag-bins.tar.zst"; \
    echo "${BINS_SHA256}  /tmp/bins.tar.zst" | sha256sum -c -; \
    mkdir -p /opt/diag; \
    tar --zstd -xf /tmp/bins.tar.zst -C /opt/diag; \
    rm /tmp/bins.tar.zst

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

# CUDA 12.8 runtime and cuBLAS, the versions the binaries were built against. No cuDNN.
FROM nvidia/cuda:12.8.1-base-ubuntu24.04

ARG BUILD_VERSION=dev

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ggml-cuda also links NCCL (single GPU here, but the library must load): the CUDA 12.8 build of it.
RUN set -eux; \
    apt-get update; \
    nccl=$(apt-cache madison libnccl2 | awk '{print $3}' | grep -m1 'cuda12\.8$'); \
    apt-get install -y --no-install-recommends python3 libgomp1 libcublas-12-8 "libnccl2=${nccl}"; \
    rm -rf /var/lib/apt/lists/*

COPY --from=driver /opt/nvidia /opt/nvidia
COPY --from=fetch /opt/diag /opt/diag
RUN ln -s /opt/nvidia/bin/nvidia-smi /usr/local/bin/nvidia-smi \
    && echo /opt/nvidia/lib > /etc/ld.so.conf.d/00-nvidia.conf \
    && ldconfig \
    && ln -sf libcuda.so.1 /opt/nvidia/lib/libcuda.so

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/*.py

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="GPU Diagnostic" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

CMD ["python3", "/usr/local/bin/diag.py"]
