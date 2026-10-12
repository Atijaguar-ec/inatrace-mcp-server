# Dockerfile para inatrace-mcp-server (Python 3.11 + uv)
# Optimizado para arranque en frío ultrarrápido y footprint < 140MB
FROM python:3.11-slim AS builder

# Instalar gestor ultra-rápido uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app
COPY pyproject.toml .

# Instalar dependencias en el entorno del sistema
RUN uv pip install --system -r pyproject.toml

# Etapa final de producción (mínimo peso)
FROM python:3.11-slim

WORKDIR /app

# Copiar paquetes instalados desde la etapa builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY src/ ./src/

# Usuario no root para cumplimiento de seguridad ISO/IEC 27001
RUN useradd -u 1000 -m inatrace && chown -R inatrace:inatrace /app
USER 1000

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

CMD ["python", "-m", "src.app"]
