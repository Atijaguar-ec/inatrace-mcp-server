# Dockerfile para inatrace-mcp-server (Python 3.11 + uv)
# Build reproducible: versiones resueltas desde uv.lock y verificadas por hash.
FROM python:3.11-slim AS builder

# Versión de uv fijada: un tag móvil (:latest) hace el build irreproducible y
# convierte la imagen del gestor en una vía de compromiso de la cadena de suministro.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv

WORKDIR /app

# uv.lock es la fuente de verdad de las versiones; pyproject.toml solo declara rangos.
COPY pyproject.toml uv.lock ./

# 'uv lock --check' aborta el build si uv.lock no corresponde a pyproject.toml. Es
# imprescindible: --frozen solo impide re-resolver, de modo que por sí mismo exportaría
# el lock obsoleto ignorando en silencio una dependencia añadida sin re-lock.
# --require-hashes rechaza cualquier artefacto cuyo hash no coincida con el registrado
# en el lock (ISO/IEC 27001, trazabilidad de cadena de suministro).
RUN uv lock --check \
 && uv export --frozen --no-dev --no-emit-project -o requirements.txt \
 && uv pip install --system --require-hashes -r requirements.txt

# Etapa final de producción (mínimo peso)
FROM python:3.11-slim

WORKDIR /app

# Copiar paquetes instalados desde la etapa builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY src/ ./src/
COPY tests/ ./tests/

# Usuario no root para cumplimiento de seguridad ISO/IEC 27001
RUN useradd -u 1000 -m inatrace && chown -R inatrace:inatrace /app
USER 1000

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Apunta a /health/ready: sin pool de solo lectura el proceso vive pero no puede
# responder ninguna consulta regulatoria, y el orquestador debe saberlo.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.app"]
