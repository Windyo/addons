# Ollama CUDA add-on for HAOS (amd64): a local LLM on the GPU the NVIDIA Driver add-on loaded.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS fetch

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl zstd xz-utils binutils \
    && rm -rf /var/lib/apt/lists/*

# Pinned: v0.30 broke Pascal (#16415), fixed in #16994. Upgrades are deliberate and re-run the
# benchmark. The checksum is pinned here and must also match the release's sha256sum.txt.
ARG OLLAMA_VERSION=0.40.11
ARG OLLAMA_SHA256=7bedab90dd122661be78798e9f0edf37fccf0cccf2699d2c58ea1a3afdc04b6d
RUN set -eux; \
    cd /tmp; \
    base="https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}"; \
    curl -fsSLo ollama.tar.zst "${base}/ollama-linux-amd64.tar.zst"; \
    curl -fsSLo sha256sum.txt "${base}/sha256sum.txt"; \
    grep -E "^${OLLAMA_SHA256}  (\./)?ollama-linux-amd64\.tar\.zst$" sha256sum.txt; \
    echo "${OLLAMA_SHA256}  ollama.tar.zst" | sha256sum -c -; \
    mkdir -p /opt/ollama; \
    tar --zstd -xf ollama.tar.zst -C /opt/ollama; \
    rm -f ollama.tar.zst; \
    ls /opt/ollama/lib/ollama; \
    # Only the cuda_v12 runner has real sm_61 kernels; cuda_v13 starts at sm_75.
    cd /opt/ollama/lib/ollama; \
    rm -rf cuda_v13 vulkan* mlx* rocm*; \
    test -d cuda_v12; \
    if find /opt/ollama -name 'libcudnn*' | grep .; then echo "cuDNN must never ship"; exit 1; fi; \
    for f in cuda_v12/*.so*; do ! objdump -p "$f" 2>/dev/null | grep -i 'NEEDED.*cudnn'; done; \
    # The release's ggml CUDA library moves aside; main.py links the one chosen by `cuda_library`.
    mkdir -p /opt/ggml/release; \
    mv cuda_v12/libggml-cuda.so /opt/ggml/release/; \
    echo "Ollama ${OLLAMA_VERSION} release, 13 GPU architectures" > /opt/ggml/release/VERSION

# The same library rebuilt in CI from Ollama's source and release preset, for sm_61 only
# (.github/workflows/ollama-runner-sm61.yaml). The release's one faulted the GTX 1070 (Xid 13).
ARG SM61_RELEASE=ollama-cuda-v12-sm61-2
ARG SM61_SHA256=8ed85e0ed194a8dafb4b0da5ee0b93b772dc385e91a6f2095d4491665e96908b
RUN set -eux; \
    curl -fsSLo /tmp/sm61.tar.zst "https://github.com/windyo/addons/releases/download/${SM61_RELEASE}/ollama-cuda-v12-sm61.tar.zst"; \
    echo "${SM61_SHA256}  /tmp/sm61.tar.zst" | sha256sum -c -; \
    mkdir -p /opt/ggml/sm61; \
    tar --zstd -xf /tmp/sm61.tar.zst -C /opt/ggml/sm61; \
    rm /tmp/sm61.tar.zst; \
    test -f /opt/ggml/sm61/libggml-cuda.so; \
    ! objdump -p /opt/ggml/sm61/libggml-cuda.so | grep -i 'NEEDED.*cudnn'

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
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=driver /opt/nvidia /opt/nvidia
COPY --from=fetch /opt/ollama /usr
COPY --from=fetch /opt/ggml /opt/ggml
RUN ln -s /opt/nvidia/bin/nvidia-smi /usr/local/bin/nvidia-smi \
    && echo /opt/nvidia/lib > /etc/ld.so.conf.d/nvidia.conf \
    && ldconfig \
    && ln -sf libcuda.so.1 /opt/nvidia/lib/libcuda.so

COPY rootfs/ /
RUN chmod 0755 /usr/local/bin/*.py

LABEL io.hass.type="addon" \
      io.hass.arch="amd64" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="Ollama CUDA" \
      org.opencontainers.image.source="https://github.com/windyo/addons"

CMD ["python3", "/usr/local/bin/main.py"]
