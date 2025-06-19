ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV NVARCH x86_64
ENV NVDISTRO debian12

# Install CUDA
RUN apt update \
    && apt install software-properties-common wget -y \
    && apt-add-repository contrib non-free-firmware \
    && wget https://developer.download.nvidia.com/compute/cuda/repos/${NVDISTRO}/${NVARCH}/cuda-keyring_1.1-1_all.deb \
    && dpkg -i cuda-keyring_1.1-1_all.deb \
    && apt update \
    && apt install cuda-12-8 -y 

ENV PATH /usr/local/cuda-12.8/bin${PATH:+:${PATH}}
ENV LD_LIBRARY_PATH=${LD_LIBRARY_PATH}:/usr/local/cuda-12.8/lib64

# Required for nvidia-docker v1
RUN echo "/usr/local/nvidia/lib" >> /etc/ld.so.conf.d/nvidia.conf \
    && echo "/usr/local/nvidia/lib64" >> /etc/ld.so.conf.d/nvidia.conf


# Install Whisper
WORKDIR /usr/src
ARG WYOMING_WHISPER_VERSION
ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        netcat-traditional \
        python3 \
        python3-dev \
        python3-pip \
	curl \
    \
    && pip3 install --no-cache-dir -U \
        setuptools \
        wheel \
    && pip3 install --no-cache-dir \
        "wyoming-faster-whisper @ https://github.com/rhasspy/wyoming-faster-whisper/archive/refs/tags/v${WYOMING_WHISPER_VERSION}.tar.gz" \
        'transformers==4.52.4' \
    \
    && pip3 install --no-cache-dir \
        --index-url 'https://download.pytorch.org/whl/cu128' \
        'torch' \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /
COPY rootfs /

ENV NVIDIA_VISIBLE_DEVICES all
ENV NVIDIA_DRIVER_CAPABILITIES compute,utility

HEALTHCHECK --start-period=10m \
    CMD echo '{ "type": "describe" }' \
        | nc -w 1 localhost 10300 \
        | grep -q "faster-whisper" \
        || exit 1

