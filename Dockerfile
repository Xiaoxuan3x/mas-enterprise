# ─────────────────────────────────────────────────────────────────────────────
# MAS Enterprise Gateway — Dockerfile
# Target: On-prem NVIDIA B200/B300 or AWS ECS/EKS
# Base: Python 3.11 slim (CUDA base used when deploying with GPU agents)
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

WORKDIR /app

# System dependencies for cryptography, httpx, and uvicorn
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Dependency installation ───────────────────────────────────────────────────
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Application ───────────────────────────────────────────────────────────────
FROM deps AS app

COPY . .

# Non-root user for zero-trust container security
RUN useradd -m -u 1001 masapp && chown -R masapp:masapp /app
USER masapp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "gateway.ingress:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--log-config", "/dev/null"]
