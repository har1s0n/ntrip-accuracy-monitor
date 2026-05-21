-- PostgreSQL
-- ntrip_accuracy_monitor/scripts/init_db.sql
-- Первичная инициализация роли и базы для ntrip-accuracy-monitor.
--
-- Запуск (через обёртку init_db.sh для удобства, см. рядом):
--
--   Linux/macOS:
--     sudo -u postgres psql -v pg_password="$PG_PASSWORD" -f scripts/init_db.sql
--
--   Windows (PowerShell):
--     psql -U postgres -v pg_password="$env:PG_PASSWORD" -f scripts\init_db.sql
--
-- Идемпотентен. При повторном запуске пароль роли синхронизируется со
-- значением pg_password — это намеренное поведение для bootstrap.

-- ---------------------------------------------------------------------------
-- 0. Проверка переменной pg_password.
-- ---------------------------------------------------------------------------

\if :{?pg_password}
\else
\warn 'ERROR: pg_password не задан. Запустите: psql -v pg_password=... -f init_db.sql'
\quit 1
\endif

DO $$
BEGIN
    IF :'pg_password' = '' THEN
        RAISE EXCEPTION 'pg_password is empty; provide a non-empty value via psql -v';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Роль приложения. Создаём, если нет; иначе обновляем пароль.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ntrip') THEN
        EXECUTE format(
            'CREATE ROLE ntrip WITH LOGIN ENCRYPTED PASSWORD %L',
            :'pg_password'
        );
    ELSE
        EXECUTE format(
            'ALTER ROLE ntrip WITH LOGIN ENCRYPTED PASSWORD %L',
            :'pg_password'
        );
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. База данных. CREATE DATABASE не идёт внутри транзакции/DO — через \gexec.
-- ---------------------------------------------------------------------------

SELECT 'CREATE DATABASE ntrip_monitor OWNER ntrip ENCODING ''UTF8'' TEMPLATE template0'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'ntrip_monitor')
\gexec

-- ---------------------------------------------------------------------------
-- 3. Привилегии.
-- ---------------------------------------------------------------------------

GRANT ALL PRIVILEGES ON DATABASE ntrip_monitor TO ntrip;
