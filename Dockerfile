# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools for asyncpg (needs pg_config)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# Stage 2: Runtime
FROM python:3.12-slim AS runtime

# libpq for asyncpg runtime
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd -r agent && useradd -r -g agent -d /app -s /sbin/nologin agent

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY . /app/k8s_runbook_agent/

# Ensure __init__.py exists at the top level for the package
RUN test -f /app/k8s_runbook_agent/__init__.py || touch /app/k8s_runbook_agent/__init__.py

# Set ownership
RUN chown -R agent:agent /app

USER agent

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

EXPOSE 8080

# Run with uvicorn
# - 4 workers for production (adjust based on CPU)
# - Graceful shutdown timeout of 30s
# - Access log disabled (we have our own middleware)
CMD ["uvicorn", "k8s_runbook_agent.server:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--timeout-graceful-shutdown", "30", \
     "--no-access-log"]
