-- PostgreSQL
-- Первичная инициализация роли и базы данных для ntrip-accuracy-monitor.
--
-- ПЕРЕД ЗАПУСКОМ: замените 'CHANGE_ME_BEFORE_RUN' на реальный пароль и
-- пропишите этот же пароль в .env (переменная PG_PASSWORD).
--
-- Запуск как суперпользователь:
--   Linux/macOS:  sudo -u postgres psql -f scripts/init_db.sql
--   Windows:      psql -U postgres -f scripts\init_db.sql
-- Повторный запуск не падает, если роль/БД уже есть.

-- 1. Роль приложения.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ntrip') THEN
        CREATE ROLE ntrip WITH LOGIN ENCRYPTED PASSWORD 'CHANGE_ME_BEFORE_RUN';
    END IF;
END
$$;

-- 2. База данных. CREATE DATABASE нельзя обернуть в DO/транзакцию,
--    поэтому используем psql-мета-команду \gexec.
SELECT 'CREATE DATABASE ntrip_monitor OWNER ntrip ENCODING ''UTF8'' TEMPLATE template0'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'ntrip_monitor')
\gexec

-- 3. Привилегии.
GRANT ALL PRIVILEGES ON DATABASE ntrip_monitor TO ntrip;
