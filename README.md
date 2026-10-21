# INATrace MCP Server (Python 3.11+ / FastMCP)
## Oráculo de Trazabilidad, Cumplimiento Regulatorio Ecuador-UE e Interoperabilidad Institucional

Microservicio satélite de alta especialización diseñado bajo los estándares internacionales **ISO/IEC 25010** (Calidad de Software), **ISO/IEC 27001** (Seguridad de la Información) e **ISO 22005** (Trazabilidad en la Cadena Alimentaria).

Actúa como un oráculo de interoperabilidad bidireccional entre las cooperativas ecuatorianas (**UNOCACE**, **Fortaleza del Valle**), los sistemas de acopio y báscula local (**ERP Contable / SRI**), y las autoridades aduaneras europeas (**TRACES-NT / Reglamento EUDR 2023/1115**).

---

## 🏛️ Arquitectura Modular por Capas

```
inatrace-mcp-server/
├── Dockerfile                      # Imagen ligera python:3.11-slim (< 140 MB)
├── pyproject.toml                  # Dependencias y metadatos del proyecto
├── README.md                       # Documentación técnica
├── src/
│   ├── app.py                      # FastMCP bootstrap, registro modular y lifecycle hooks
│   ├── config.py                   # Pydantic Settings / Environment loader
│   ├── domain/                     # Lógica pura de negocio y contratos regulatorios
│   │   ├── dag_engine.py           # Reconstrucción retrospectiva recursiva del Grafo DAG
│   │   ├── eudr_geometry.py        # Topología WGS84, cierre de anillos (>4 ha) y centroides (<=4 ha)
│   │   ├── privacy.py              # Data Minimization Gate (LOPDP Art. 10 / GDPR Art. 5)
│   │   ├── sri_validator.py        # Algoritmo de Módulo 11 (49 dígitos) y claves canónicas LC|CA|ERP
│   │   └── traces_ld.py            # Serializador canónico JSON-LD para TRACES-NT / DG SANTE
│   ├── infrastructure/             # Adaptadores de datos y seguridad
│   │   ├── db_pool.py              # Pool resiliente asyncpg (Modo Solo Lectura inatrace_mcp_ro)
│   │   ├── keycloak_guard.py       # Control de acceso OIDC y Gobernanza 4 Tiers (RS256)
│   │   └── spring_client.py        # Cliente HTTPX asíncrono hacia Core Spring Boot (CQRS)
│   └── tools/                      # Herramientas FastMCP
│       ├── audit_tools.py          # audit_lot_traceability & verify_eudr_compliance
│       ├── diagnostic_tools.py     # diagnose_tenant_health & diagnose_and_repair_qr
│       ├── erp_tools.py            # ingest_erp_purchase_order, sync_farmer_profile & verify_sri_compliance
│       └── export_tools.py         # generate_traces_jsonld & export_agrocalidad_guia
└── tests/                          # Suite completa de pruebas automatizadas
    ├── run_tests.py                # Test runner autónomo (unittest + pytest compatible)
    ├── test_dag_engine.py          # Navegación retrospectiva multinivel
    ├── test_db_queries_integration.py # Pruebas de integración de queries SQL con mock pool
    ├── test_edge_cases.py          # Casos límite y condiciones de frontera
    ├── test_eudr_geometry.py       # Cierre topológico WGS84 y centroides
    ├── test_keycloak_guard.py      # Autenticación OIDC y Gobernanza 4 Tiers
    ├── test_mcp_tools.py           # Integración asíncrona de las 9 tools
    ├── test_privacy.py             # Sanitización de datos personales PII
    ├── test_sri_validator.py       # Algoritmo Módulo 11 y extracción secuencial
    └── test_traces_ld.py           # Serialización DG SANTE y padding DPA INEC

```

---

## 🛠️ Catálogo Canónico de Herramientas FastMCP

| Herramienta | Entrada Principal | Función y Marco Regulatorio |
|---|---|---|
| `audit_lot_traceability` | `lot_identifier`, `company_id`, `public_view` | Reconstruye el árbol DAG hacia atrás desde el saco de 69kg hasta productores y parcelas. Si `public_view=True`, sanitiza toda PII (LOPDP/GDPR). |
| `verify_eudr_compliance` | `stock_order_id`, `strict_polygon_closure` | Audita anillos cerrados WGS84 (>4 ha), centroides (<=4 ha, 6+ decimales) y fecha de corte de deforestación al 31/12/2020. |
| `generate_traces_jsonld` | `stock_order_id`, `operator_ruc` | Emite el payload JSON-LD oficial según el esquema W3C/DG SANTE con códigos DPA INEC de 6 dígitos para TRACES-NT. |
| `export_agrocalidad_guia` | `company_id`, `date_from`, `date_to` | Emite el reporte oficial CSV delimitado por `;` con UTF-8 BOM (`\uFEFF`) para el sistema GUIA de Agrocalidad (ADR-BI-001). |
| `verify_sri_compliance` | `stock_order_id` | Audita el campo `comments` del lote validando el algoritmo de Módulo 11 de la clave de 49 dígitos del SRI. |
| `ingest_erp_purchase_order` | `company_id`, `farmer_id`, `parcel_name`, `gross_weight_kg`, `tare_kg`, `sri_access_key`, `erp_internal_id` | Valida clave SRI, convierte kg a Libras canónicas (`PESOLIBRA`) y delega la escritura al Core API de Spring Boot (`PUT /api/chain/stock-order`). |
| `sync_farmer_profile` | `company_id`, `farmer_cedula`, `first_name`, `last_name`, `phone`, `canton_dpa_code` | Sincroniza datos maestros de agricultores con consentimientos LOPDP delegando a Spring Boot (`POST /api/company/userCustomers`). |
| `diagnose_tenant_health` | `company_id` | Scanner automático para anomalías conocidas de producción: PM-033 (rol OWNER), PM-036 (semiproducto de salida QR), PM-037 (Torpedo ORDER BY), PM-040 (DPA padding). |
| `diagnose_and_repair_qr` | `qr_payload`, `company_id` | Oráculo interactivo para escaneos de código QR: diagnostica 403 Forbidden vs 404 Not Found y valida banderas en `company.configuration`. |

---

## 🧪 Ejecución de Pruebas Automatizadas

La suite **exige las dependencias declaradas en `pyproject.toml`**. Todas tienen un
fallback por `ImportError` en `src/`, de modo que si faltan no se produce ningún
fallo: se activan rutas degradadas (sin SDK de MCP, sin driver de PostgreSQL, sin
topología de Shapely, sin verificación de firmas JWT) y la suite pasaría en verde sin
ejercitar nada de eso. `run_tests.py` lo comprueba antes de empezar y aborta con
código 2 enumerando lo que falte.

```bash
# Crear el entorno del proyecto (una sola vez)
uv venv && uv pip install -r pyproject.toml --extra dev
```

```bash
# Suite completa con el runner autónomo
KEYCLOAK_AUDIENCE=inatrace-mcp uv run python tests/run_tests.py
```

```bash
# Con informe de cobertura (falla por debajo del umbral de pyproject.toml)
KEYCLOAK_AUDIENCE=inatrace-mcp uv run coverage run tests/run_tests.py && uv run coverage report
```

`KEYCLOAK_AUDIENCE` es necesario porque sin audiencia configurada el guard rechaza
todo token: es el comportamiento correcto en producción, pero haría fallar las
pruebas de resolución de tier.

Las pruebas usan dobles para el pool de PostgreSQL y no abren ninguna conexión real.
Para verificar latencia y aislamiento transaccional contra la base de datos de
staging existe [`tests/verify_staging_live.py`](tests/verify_staging_live.py), que
aborta si no obtiene el pool de solo lectura.

---

## 📦 Despliegue en Docker

> [!IMPORTANT]
> El servicio **no termina TLS** y debe publicarse únicamente en el loopback del
> host (`127.0.0.1`), detrás de un proxy inverso que aporte TLS. Publicarlo como
> `-p 8000:8000` lo expone en todas las interfaces y deja los tokens y los
> expedientes de trazabilidad en claro.
>
> Ten en cuenta además que `inatrace-test-network` es una red externa compartida:
> cualquier contenedor en ella alcanza el puerto 8000 del contenedor sin pasar por
> el binding de loopback del host.

```bash
# Construir la imagen
docker build -t inatrace-mcp-server .

# Ejecutar el contenedor en la red de INATrace (solo loopback del host)
docker run -d --name inatrace-mcp \
  --network inatrace-network \
  -p 127.0.0.1:8085:8000 \
  -e DB_HOST=inatrace-postgres \
  -e DB_PORT=5432 \
  -e DB_NAME=inatrace_unocace_stage \
  -e DB_USER=inatrace_mcp_ro \
  -e DB_PASSWORD=ro_password_secure \
  -e CORE_API_URL=http://inatrace-backend:8080/api \
  -e KEYCLOAK_URL=https://auth.unocace.com \
  -e KEYCLOAK_REALM=unocace \
  -e SERVICE_ACCOUNT_CLIENT_ID=service-account-erp-contable \
  -e SERVICE_ACCOUNT_CLIENT_SECRET=secret \
  inatrace-mcp-server
```
