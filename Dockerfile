FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    PATH="/opt/BitNet/build/bin:/opt/BitNet:${PATH}"

# ── Dependências base ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        curl \
        gnupg \
        lsb-release \
        software-properties-common \
        ca-certificates \
        git \
        build-essential \
        cmake \
        libomp-dev \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# ── Clang 18 via repositório oficial LLVM ───────────────────────────────────
RUN wget -qO /tmp/llvm.sh https://apt.llvm.org/llvm.sh \
    && chmod +x /tmp/llvm.sh \
    && bash /tmp/llvm.sh 18 \
    && apt-get install -y --no-install-recommends \
        clang-18 \
        libc++-18-dev \
        libc++abi-18-dev \
    && update-alternatives --install /usr/bin/clang   clang   /usr/bin/clang-18   100 \
    && update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-18 100 \
    && rm -f /tmp/llvm.sh \
    && rm -rf /var/lib/apt/lists/*

# ── Clonar BitNet com todos os submódulos ───────────────────────────────────
WORKDIR /opt
RUN git clone --recursive https://github.com/microsoft/BitNet.git

WORKDIR /opt/BitNet

# ── Patch: const-correctness bug em ggml-bitnet-mad.cpp:811 ─────────────────
# y é const int8_t* mas y_col não declara const → erro hard em clang.
RUN sed -i \
    's/int8_t \* y_col = y + col \* by;/const int8_t * y_col = y + col * by;/' \
    src/ggml-bitnet-mad.cpp

# ── Passo 1: gguf + dependências Python ─────────────────────────────────────
# Equivalente a setup_gguf() em setup_env.py
RUN pip3 install --no-cache-dir 3rdparty/llama.cpp/gguf-py \
    && pip3 install --no-cache-dir -r requirements.txt

# ── Passo 2: gerar include/bitnet-lut-kernels.h ─────────────────────────────
# Equivalente a gen_code() em setup_env.py para x86_64 + modelo BitNet-b1.58-2B-4T.
# codegen_tl2.py escreve em ../../include/ relativo ao script = /opt/BitNet/include/
# Parâmetros idênticos aos usados pelo setup_env.py para "BitNet-b1.58-2B-4T" em x86_64.
RUN python3 utils/codegen_tl2.py \
        --model  bitnet_b1_58-3B \
        --BM     160,320,320 \
        --BK     96,96,96 \
        --bm     32,32,32

# ── Passo 3: compilar ────────────────────────────────────────────────────────
# Equivalente a compile() em setup_env.py para x86_64.
# -DBITNET_X86_TL2=OFF é o COMPILER_EXTRA_ARGS que setup_env.py usa em x86_64.
# setup_env.py usa "clang"/"clang++" — nossos alternatives apontam para clang-18.
RUN cmake -B build \
        -DBITNET_X86_TL2=OFF \
        -DGGML_NATIVE=OFF \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER=clang \
        -DCMAKE_CXX_COMPILER=clang++ \
    && cmake --build build --config Release -j$(nproc)

# ── Diretório de modelos (monte com -v ./models:/models) ────────────────────
RUN mkdir -p /models
VOLUME ["/models"]

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /opt/BitNet
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
