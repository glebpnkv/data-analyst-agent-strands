FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv for fast, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /uvx /usr/local/bin/

WORKDIR /app

# Minimal system deps. matplotlib needs libgomp at runtime; everything else
# we ship is pure-Python or has manylinux wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN uv pip install --system --no-cache -r /app/requirements.txt

COPY server.py kernel.py auth.py models.py /app/

# Non-root runtime user; /workspace is the kernel's cwd and the only
# writable area the LLM is supposed to touch. In production /workspace
# is overlaid with tmpfs via the ECS task definition's linuxParameters
# (so anything the LLM writes stays in RAM and dies with the task);
# the chown here is for the local-dev `docker run` case where there's
# no tmpfs overlay.
RUN groupadd --system --gid 1000 runner \
 && useradd --system --uid 1000 --gid runner --create-home runner \
 && mkdir -p /workspace \
 && chown -R runner:runner /workspace /app
USER runner:runner

EXPOSE 8081

# Single worker: kernel state is per-process. uvicorn's reload/multi-worker
# mode would spawn multiple kernels and lose all the persistence we want.
CMD ["uvicorn", "server:app", \
     "--host", "0.0.0.0", \
     "--port", "8081", \
     "--workers", "1", \
     "--app-dir", "/app"]
