-- Provisión del rol de solo lectura `inatrace_mcp_ro`.
--
-- Implementa el control exigido por docs/DISENO_ARQUITECTURA_INATRACE_MCP_SERVER_ISO.md
-- (§154-160): el servidor MCP lee con un usuario dedicado sin privilegios de
-- escritura, de modo que ningún agente de IA pueda ejecutar INSERT, UPDATE o
-- DELETE directos sobre la base de datos.
--
-- NO reutilizar las credenciales del backend: son de lectura y escritura, y con
-- ellas el aislamiento CQRS que sostiene la certificación de QA desaparece.
--
-- Uso:
--   psql -h <host> -U <admin> -d <base> -v ON_ERROR_STOP=1 -f provision-ro-role.sql
--   psql -h <host> -U <admin> -d <base> -c "\password inatrace_mcp_ro"
--
-- La contraseña se asigna aparte, con `\password`, que la pide por terminal sin
-- escribirla en el historial del shell ni en la lista de procesos. No pasarla por
-- `-v` ni dejarla en este fichero.

\set ON_ERROR_STOP on

-- 1. Rol sin privilegios heredados y sin capacidad de crear nada.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'inatrace_mcp_ro') THEN
    CREATE ROLE inatrace_mcp_ro WITH LOGIN NOINHERIT NOCREATEDB NOCREATEROLE NOSUPERUSER;
    RAISE NOTICE 'Rol inatrace_mcp_ro creado. Asigne la contraseña con \\password.';
  ELSE
    RAISE NOTICE 'El rol inatrace_mcp_ro ya existe; se reaplican los privilegios.';
  END IF;
END
$$;

-- 2. Conexión a la base actual y visibilidad de los esquemas que usan las
--    herramientas. `bi` es imprescindible: export_agrocalidad_guia consulta
--    bi.rpt_cacao_agrocalidad_guia y sin USAGE esa herramienta falla.
GRANT CONNECT ON DATABASE :"DBNAME" TO inatrace_mcp_ro;
GRANT USAGE ON SCHEMA public TO inatrace_mcp_ro;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'bi') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA bi TO inatrace_mcp_ro';
  ELSE
    RAISE WARNING 'El esquema bi no existe: export_agrocalidad_guia fallará hasta que se cree.';
  END IF;
END
$$;

-- 3. Solo lectura, revocando primero cualquier privilegio previo.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM inatrace_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO inatrace_mcp_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO inatrace_mcp_ro;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'bi') THEN
    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA bi FROM inatrace_mcp_ro';
    EXECUTE 'GRANT SELECT ON ALL TABLES IN SCHEMA bi TO inatrace_mcp_ro';
  END IF;
END
$$;

-- 4. Las tablas y vistas que se creen después también nacen en solo lectura.
--    Sin esto, una tabla nueva quedaría inaccesible y el servidor fallaría en
--    seco en lugar de leerla.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO inatrace_mcp_ro;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'bi') THEN
    EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA bi GRANT SELECT ON TABLES TO inatrace_mcp_ro';
  END IF;
END
$$;

-- 5. Cinturón adicional a nivel de sesión. El servidor ya lo aplica por conexión
--    en src/infrastructure/db_pool.py; aquí queda también en el rol.
ALTER ROLE inatrace_mcp_ro SET default_transaction_read_only = on;

-- 6. Verificación: debe devolver 0 privilegios de escritura.
SELECT
  count(*) AS privilegios_de_escritura_detectados
FROM information_schema.table_privileges
WHERE grantee = 'inatrace_mcp_ro'
  AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER');
