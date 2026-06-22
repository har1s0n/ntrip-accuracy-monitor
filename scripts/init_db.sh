#!/usr/bin/env bash
# ntrip_accuracy_monitor/scripts/init_db.sh
# Обёртка над init_db.sql: читает PG_PASSWORD из .env (формат KEY=VALUE)
# и передаёт его в psql. Запускать из любой директории.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Исправлено: поднимаемся на один уровень вверх к корню проекта
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"
SQL_FILE="${SCRIPT_DIR}/init_db.sql"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: .env не найден по пути ${ENV_FILE}" >&2
    echo "Создайте .env с переменной PG_PASSWORD перед запуском." >&2
    exit 1
fi

# Извлекаем PG_PASSWORD из .env, снимаем возможные кавычки.
PG_PASSWORD="$(
    grep -E '^[[:space:]]*PG_PASSWORD[[:space:]]*=' "$ENV_FILE" \
        | head -n 1 \
        | sed -E 's/^[[:space:]]*PG_PASSWORD[[:space:]]*=[[:space:]]*//' \
        | sed -E 's/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
)"

if [[ -z "${PG_PASSWORD:-}" ]]; then
    echo "ERROR: PG_PASSWORD пуст или не задан в ${ENV_FILE}" >&2
    exit 1
fi

# Определяем команду для запуска psql
if id -u postgres >/dev/null 2>&1; then
    # Linux-окружения, где есть системный пользователь postgres
    PSQL_CMD="sudo -u postgres psql"
else
    # macOS или окружения, где psql работает от текущего пользователя
    PSQL_CMD="psql postgres"
fi

echo "Запуск init_db.sql через: ${PSQL_CMD}"
$PSQL_CMD -v ON_ERROR_STOP=1 -v pg_password="$PG_PASSWORD" < "$SQL_FILE"
echo "Готово. Теперь можно запускать миграции."
