# syntax=docker/dockerfile:1.7
#
# Production image for the Laya service.
#
# Multi-stage: the builder resolves and installs wheels into a self-contained
# virtualenv, and the runtime stage copies only that venv plus the source. No
# compilers, no pip cache, and no build tooling end up in the shipped image.
#
# Build from the repository root:
#   docker build -f deploy/docker/Dockerfile -t laya-service:latest .
#
# IMPORTANT: this image installs CPU-only PyTorch. It is ~200 MB instead of
# ~2.5 GB, and it is the right choice on any host without a GPU. For a CUDA
# image, change the TORCH_INDEX_URL build argument below.

# ---------------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

# Pin the CPU wheel index. Override for a CUDA build.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Deliberately NOT installing build-essential. Every dependency in the resolved
# set ships a manylinux wheel for this platform (torch, tokenizers, safetensors,
# numpy, pydantic-core all do), so a compiler toolchain would add ~320 MB and
# several minutes of apt downloads to the build for nothing. If you add a
# dependency that needs to compile from source, add build-essential here -- it
# is discarded with this stage either way.
#
# `curl` is only needed by the builder's healthcheck-free path; the runtime
# stage installs its own copy for HEALTHCHECK.

# Dedicated venv so the runtime stage can copy one directory.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

# CPU-only torch first, so the resolver below never considers the CUDA build.
#
# --extra-index-url is required, not optional. The torch index carries wheels
# but no source distributions, so with --index-url alone pip cannot fetch the
# build backend (flit_core) that some wheels declare, and the install dies with
# "Could not find a version that satisfies the requirement flit_core". Keeping
# PyPI as a fallback index fixes that without letting it supply torch itself --
# the explicit ==2.9.1+cpu pin and the CPU index resolve first.
RUN pip install \
        --index-url "${TORCH_INDEX_URL}" \
        --extra-index-url "${PIP_INDEX_URL}" \
        "torch==2.9.1+cpu"

# Copy only the packaging metadata first: this layer is cached until the
# dependency set actually changes, so editing source does not reinstall torch.
COPY pyproject.toml README.md ./
COPY requirements/ ./requirements/
COPY src/ ./src/

# Upgrade pip inside the venv, then install the project and its dependencies.
# `--no-deps` is deliberately NOT used: we want the full closure resolved.
RUN pip install --index-url "${PIP_INDEX_URL}" --upgrade pip setuptools wheel \
    && pip install --index-url "${PIP_INDEX_URL}" .

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="laya-service" \
      org.opencontainers.image.description="Decision-model inference over HTTP" \
      org.opencontainers.image.source="https://github.com/example/laya-service"

# Unbuffered stdout so logs reach the container runtime immediately, and no
# .pyc files because the source tree is read-only at runtime.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    # Keep the Hugging Face cache on a mounted volume in production so that a
    # container restart does not re-download gigabytes of weights.
    HF_HOME=/data/hf \
    MODEL_CACHE_DIR=/data/hf \
    HOST=0.0.0.0 \
    PORT=8000

# Only two packages are genuinely needed here:
#
#   libgomp1 -- torch's libtorch_cpu.so links against libgomp.so.1 for OpenMP.
#               Without it the container starts and then dies on `import torch`
#               with "ImportError: libgomp.so.1: cannot open shared object file".
#               It is ~150 kB.
#
# `curl` is deliberately NOT installed. The HEALTHCHECK below uses the
# interpreter's own urllib instead, which removes a ~30 MB dependency and, more
# importantly, avoids pulling the Debian apt index -- which on a slow mirror is
# the single slowest step in this build by a wide margin.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 laya \
    && useradd --uid 10001 --gid laya --create-home --shell /usr/sbin/nologin laya \
    && mkdir -p /app /data/hf \
    && chown -R laya:laya /app /data

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=laya:laya /build/src /app/src

WORKDIR /app

# Run as a non-root user. If an attacker achieves code execution through the
# inference path, they land in an unprivileged account with no write access to
# the application code.
USER laya

EXPOSE 8000

# Liveness, not readiness: a container whose model is still loading is healthy
# and must not be restarted -- restarting would discard the in-progress load and
# loop forever.
#
# Uses the interpreter's urllib rather than curl, so the image needs no HTTP
# client on PATH. `--start-period` is generous because importing torch alone
# takes several seconds before the server can bind.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# Bind to 0.0.0.0 so the container is reachable; this REQUIRES LAYA_API_KEY to
# be set (the settings validator enforces it) or the process exits at startup.
CMD ["python", "-m", "laya_service.main"]
