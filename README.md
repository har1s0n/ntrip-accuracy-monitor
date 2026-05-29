# ntrip-accuracy-monitor

Мониторинг NTRIP-потока дифференциальных поправок и NMEA-телеметрии трёх ГНСС-приёмников **EFT RS3**, сохранение эпох в
PostgreSQL и расчёт метрик точности позиционирования (HRMS, VRMS, CEP50, R95, 3D-RMS) с анализом зависимости ошибки от
возраста поправок (`age_of_corrections`) и типа решения (`solution_mode`: SPP / DGNSS / RTK float / RTK fixed).

Цель проекта — инструментальное сравнение точности режимов SPP, DGNSS и RTK на реальных данных EFT RS3 в рамках
двухпрогонного 24-часового эксперимента: одна общая антенна, три канала приёма, локальный NTRIP-кастер, пост-обработка
эталона в RTKLIB. Методика эксперимента зафиксирована отдельным документом (см. `docs/`).

---

## Архитектура и поток данных

В лаборатории три приёмника EFT RS3 с разделённым доступом к одной антенне:

- **#1 база** — генерирует RTCM 3.x и отдаёт через собственный NTRIP-каст;
- **#2 rover_rtk** — принимает RTCM от нашего локального кастера, выдаёт RTK-решение;
- **#3 rover_spp** — без поправок, чистое SPP.

Со всеми тремя приёмниками сервис общается как NMEA-клиент (TCP), собирая GGA/GST/GSV/RMC/ZDA. Внутри одной команды
`run` поднимаются:

```
[EFT RS3 #1 каст] ──RTCM──► NtripClient ──► RtcmHub ──┬─► RtcmAuditWriter ─► rtcm_messages
                                                       ├─► FileRtcmSink (опц.)
                                                       └─► NtripCasterServer ──RTCM──► [EFT RS3 #2]
[EFT RS3 #2 NMEA] ─► NmeaTcpClient ─► EpochAggregator ─► EpochBatchWriter ─► epochs
[EFT RS3 #3 NMEA] ─► NmeaTcpClient ─► EpochAggregator ─► EpochBatchWriter ─► epochs

каждые refresh_interval_s + при штатной остановке:
    MetricsService.compute_*(persist=True) ─► session_metrics / metrics_by_age
```

GUI — отдельный процесс на Streamlit, ходит только в ту же PostgreSQL (`SELECT`), backend не импортирует.

---

## Стек

Только то, что по факту используется в коде:

- **[asyncpg](https://github.com/MagicStack/asyncpg)** — async-драйвер PostgreSQL. Автор: MagicStack.
- **[pydantic](https://docs.pydantic.dev/) v2** — валидация конфигурации. Автор: Samuel Colvin и команда pydantic.
- **[NumPy](https://numpy.org/)** — численная часть метрик (HRMS/VRMS/CEP/R95, выбраковка выбросов, age-бины).
- **stdlib**: `asyncio`, `tomllib`, `logging`, `argparse`, `pathlib`.

GUI (optional extra `gui`):

- **[Streamlit](https://streamlit.io/)** + **[streamlit-autorefresh](https://github.com/kmcgrady/streamlit-autorefresh)
  ** — view-only дашборд.
- **[Plotly](https://plotly.com/python/)** — интерактивные графики.
- **[pandas](https://pandas.pydata.org/)** — табличный слой между SQL и виджетами.

Разработка (optional extra `dev`): **[pytest](https://pytest.org/)** + **pytest-asyncio**, *
*[Ruff](https://github.com/astral-sh/ruff)**, **[mypy](https://mypy-lang.org/)**.

Окружение и установка интерпретатора: **[uv](https://github.com/astral-sh/uv)** (Astral).

NMEA-парсер, RTCM-адаптер и NTRIP-клиент/сервер написаны в проекте (см. `ntrip_accuracy_monitor/protocols/`): сторонние
`pynmeagps`/`pyrtcm`/`pygnssutils` не используются. Геодезические преобразования (WGS-84 → ECEF → ENU) тоже свои, без
`pyproj`.

---

## Требования

- **Python 3.13+** — установится автоматически через `uv`, системный Python не обязателен.
- **PostgreSQL 16+** — нативно, без контейнеров.
- **Git**.
- *(опционально)* **RTKLIB `rtkpost`** — для пост-обработки эталонных координат.

---

## Установка с нуля

### Windows 10 / 11

**(a) Установка uv.** Открыть PowerShell. Если политика выполнения скриптов запрещает установку, разово разрешить:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Установить uv:

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

**Перезапустить терминал**, чтобы обновилась переменная `PATH`.

**(b) Проверка:**

```powershell
uv --version
```

**(c) Python 3.13 через uv:**

```powershell
uv python install 3.13
```

**(d) PostgreSQL 16+.** Установщик EDB: <https://www.postgresql.org/download/windows/>. При установке **обязательно
запомнить пароль суперпользователя `postgres`**.

Проверка:

```powershell
psql --version
```

**(e) Роль и БД.**

1. Открыть `scripts\init_db.sql`, заменить `CHANGE_ME_BEFORE_RUN` на реальный пароль (этот же пароль пойдёт в `.env`).
2. Выполнить, любым способом:

```powershell
psql -U postgres -f scripts\init_db.sql
```

либо в `SQL Shell (psql)`:

```
\i C:/путь/к/проекту/scripts/init_db.sql
```

либо через pgAdmin: `Query Tool` → File → Open → `scripts\init_db.sql` → `Execute`.

**(f) Клонирование и зависимости:**

```powershell
git clone <repo-url>
cd ntrip-accuracy-monitor
Copy-Item .env.example .env
# отредактировать .env: PG_PASSWORD = тот же, что и в init_db.sql
uv sync --extra dev
```

**(g) Smoke-тест:**

```powershell
uv run pytest
```

---

### Linux (Debian / Ubuntu)

**(a) Установка uv:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Если после установки команда `uv` не находится — добавить `~/.local/bin` в `PATH`:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
# для zsh — ~/.zshrc
```

**(b) Проверка:**

```bash
uv --version
```

**(c) Python 3.13:**

```bash
uv python install 3.13
```

**(d) PostgreSQL 16+:**

```bash
sudo apt update
sudo apt install -y postgresql-16 postgresql-client-16
psql --version
sudo systemctl status postgresql
```

**(e) Роль и БД.** Отредактировать `scripts/init_db.sql` (заменить плейсхолдер пароля), затем:

```bash
sudo -u postgres psql -f scripts/init_db.sql
```

**(f) Клонирование и зависимости:**

```bash
git clone <repo-url>
cd ntrip-accuracy-monitor
cp .env.example .env
# отредактировать .env, прописать тот же пароль
uv sync --extra dev
```

**(g) Smoke-тест:**

```bash
uv run pytest
```

---

### macOS 13+

**(a) Установка uv:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Или через Homebrew:

```bash
brew install uv
```

**(b–c) Проверка и Python:**

```bash
uv --version
uv python install 3.13
```

**(d) PostgreSQL 16+:**

```bash
brew install postgresql@16
brew services start postgresql@16
psql --version
```

**(e) Роль и БД.** Отредактировать `scripts/init_db.sql`, затем:

```bash
psql -U postgres -f scripts/init_db.sql
```

Если в Homebrew-конфиге без отдельной роли `postgres`:

```bash
psql -U "$(whoami)" -d postgres -f scripts/init_db.sql
```

**(f) Клонирование и зависимости:**

```bash
git clone <repo-url>
cd ntrip-accuracy-monitor
cp .env.example .env
uv sync --extra dev
```

**(g) Smoke-тест:**

```bash
uv run pytest
```

---

## Конфигурация

**Переменные окружения** (через `.env`, подгружается флагом `--env-file`):

- `PG_PASSWORD` — **обязательная**, пароль роли `ntrip` из `init_db.sql`.
- `UPSTREAM_NTRIP_PASSWORD` — опц., пароль к NTRIP-кастеру базы.
- `LOCAL_CASTER_PASSWORD` — опц., basic-auth для нашего локального кастера.

Чувствительные поля **никогда** не читаются из `config.toml`; если случайно туда попали — молча отбрасываются и
замещаются значением из env.

**`config.toml`** (см. `config.example.toml`) — секции:

- `[postgres]` — host/port/database/user, размер пула.
- `[local_caster]` — host/port/mountpoint нашего NtripCasterServer (раздаёт RTCM роверу #2).
- `[upstream_ntrip]` — url/mountpoint/user базы #1, `ntrip_version` (для RS3-каста — `"1.0"`).
- `[[nmea_receivers]]` — по одной секции на приёмник: `receiver_id`, `host`, `port`, `role` ∈ {`base`, `rover_rtk`,
  `rover_spp`}.
- `[reference_antenna]` — `latitude_deg`/`longitude_deg`/`ellipsoidal_height_m` эталонной точки (из
  RTKLIB-постпроцессинга).
- `[metrics]` — `refresh_interval_s` (период пересчёта метрик активного сеанса, по умолчанию 60 с).
- `[captures]` — опц. запись сырого RTCM в файл.
- `[gui]` — `auto_refresh_ms`, `live_window_seconds` и т.д.

> ⚠️ **Важный нюанс.** `receiver_id` для роверов в `[[nmea_receivers]]` **должен быть строго `rover_rtk` и `rover_spp`
** — это значение становится `stream_id` в таблице `epochs`, и GUI хардкодит фильтр по этим именам. Назовёте иначе —
> Live monitor останется пустым при живых данных в БД.

---

## Запуск

### 1. Применение миграций (одноразово при первой установке и после обновлений схемы)

```bash
uv run --env-file .env python -m ntrip_accuracy_monitor.persistence.migrator --config config.toml
```

Раннер идемпотентен: пройденные миграции пропускаются, состояние ведётся в `schema_migrations`.

### 2. Backend — полный цикл мониторинга

```bash
uv run --env-file .env ntrip-accuracy-monitor --config config.toml run
```

Перед стартом — preflight: проверка доступности БД и применённости миграций. Если что-то не так — выход с понятным
сообщением, ingest не стартует.

Остановка — `Ctrl-C` (SIGINT). При штатном завершении делается финальный пересчёт метрик и закрывается строка сеанса.

**Коды возврата:**

- `0` — штатно;
- `2` — ошибка конфигурации;
- `3` — preflight (БД недоступна или есть непримененные миграции);
- `130` — `Ctrl-C` до установки обработчиков сигналов.

### 3. GUI — отдельный процесс

```bash
uv sync --extra gui
uv run --env-file .env streamlit run ntrip_accuracy_monitor/gui/Home.py
```

Дашборд view-only поверх той же PostgreSQL. Страницы:

- **Главная** — список сеансов, KPI всего/активных/эпох/последний старт.
- **Наблюдение в реальном времени** — живое состояние активного сеанса по сырым `epochs`/`rtcm_messages`, autorefresh из
  `config.gui.auto_refresh_ms`.
- **Отчёт по сеансу** — метрики и зависимости. Для активной сессии цифры растут по мере пересчёта в backend.
- *(в работе)* **Сравнение A vs B**.

Backend о существовании GUI не знает.

---

## Команды для повседневной разработки

```bash
uv sync --extra dev                     # переустановить runtime + dev
uv sync --extra dev --extra gui         # + GUI
uv run pytest                           # тесты
uv run ruff check .                     # линтер
uv run ruff format .                    # автоформат
uv run mypy                             # строгая проверка типов
```

---

## Типовые проблемы и решения

**Установка и окружение:**

- `uv: command not found` после установки — перезапустить терминал. На Linux/macOS убедиться, что `~/.local/bin` в
  `PATH`.
- `psql: could not connect to server` / `connection refused` — PostgreSQL не запущен:
    - Linux: `sudo systemctl start postgresql`
    - macOS: `brew services start postgresql@16`
    - Windows: Службы → `postgresql-x64-16` → Запустить.
- `ModuleNotFoundError` в тестах — забыли `uv sync` или нужный `--extra`.
- `role "ntrip" does not exist` — не выполнен `scripts/init_db.sql` или в нём не заменён плейсхолдер пароля.
- `password authentication failed for user "ntrip"` — пароль в `.env` не совпадает с тем, что был в `init_db.sql` в
  момент выполнения.
- Windows Firewall блокирует PostgreSQL — разрешить локальный доступ.

**Запуск `run`:**

- `exit 2`, `Configuration error: …` — не задана `PG_PASSWORD`, либо невалидный `config.toml`.
- `exit 3`, «не применены миграции» — запустить migrator (см. п.1 «Запуск»).
- `exit 3`, «PostgreSQL недоступен» — БД не поднята / неверные креды / неверный host:port.
- В логе «Локальный кастер не поднят: upstream_ntrip.enabled=false» — без апстрима в Hub нечего раздавать; включите
  `[upstream_ntrip].enabled = true`.
- Ровер #2 застрял в SPP (GGA quality = 1) — проверить, что (а) ровер нацелен на наш кастер (его web-UI: host = IP хоста
  мониторинга, port/mountpoint из `[local_caster]`, NTRIP 1.0); (б) NtripClient к базе подключился (лог); (в) база
  реально отдаёт RTCM 3.x (наш кастер фильтрует не-RTCM-3.x — RTCM 2.3 не пройдёт).
- `epochs` не растут — проверить host/port приёмника, XOR-checksum NMEA в логах.
- Live monitor пуст при живых данных — `receiver_id` ровера в конфиге не равен `rover_rtk`/`rover_spp` (см.
  предупреждение в разделе «Конфигурация»).
- Session report без метрик дольше `metrics.refresh_interval_s` после старта — грепнуть лог по «Пересчёт метрик … упал»;
  типовая причина — не задан `[reference_antenna]`.

**Известный долг (не баг кода):**

- **VRMS аномально большой только у RTK** при чистом HRMS — высота базы введена как эллипсоидальная при фактической
  нормальной (БСВ-77). Расхождение порядка высоты квазигеоида (≈ +14…15 м в средних широтах). Метрики честны; правка на
  стороне геодезической конфигурации базы.

---

## Структура проекта

```
ntrip-accuracy-monitor/
├── ntrip_accuracy_monitor/
│   ├── __main__.py                       # python -m ntrip_accuracy_monitor
│   ├── application/
│   │   ├── config.py                     # AppConfig, MetricsConfig, load_config
│   │   └── service/
│   │       ├── lifecycle.py              # SessionLifecycle (оркестратор)
│   │       └── metrics_service.py        # MetricsService (расчёт + persist)
│   ├── cli/
│   │   └── __main__.py                   # argparse, подкоманда `run`, preflight
│   ├── domain/                           # Epoch, SolutionMode, метрики, ENU
│   │   ├── metrics.py / _metrics_numerics.py
│   │   ├── age_bins.py / _age_bins_numerics.py
│   │   ├── geodetic.py                   # WGS-84 ↔ ECEF ↔ ENU без pyproj
│   │   └── position.py
│   ├── gui/                              # отдельный процесс, view-only
│   │   ├── Home.py                       # точка входа Streamlit
│   │   ├── _data.py / _db.py / _format.py / _sidebar.py / _overview.py
│   │   └── pages/
│   │       ├── 1_Live_monitor.py
│   │       ├── 2_Session_report.py
│   │       └── 3_Compare.py
│   ├── persistence/
│   │   ├── pool.py                       # create_pool / close_pool
│   │   ├── migrator.py                   # apply_migrations / pending_migrations
│   │   ├── migrations/                   # V001…V004 (idempotent SQL)
│   │   ├── session_repository.py
│   │   ├── epoch_repository.py
│   │   ├── rtcm_repository.py
│   │   ├── metrics_repository.py
│   │   └── age_bin_metrics_repository.py
│   └── protocols/
│       ├── backoff.py                    # BackoffPolicy
│       ├── nmea/                         # XOR-checksum, частичная сборка
│       ├── ntrip/                        # NtripClient, NtripCasterServer, RtcmHub
│       └── rtcm/                         # RtcmAdapter, _bits, _framer
├── tests/                                # pytest + pytest-asyncio
├── scripts/
│   └── init_db.sql                       # одноразовый bootstrap: роль + БД
├── docs/                                 # методика, спецификации
├── pyproject.toml                        # метаданные, [project.scripts], tool-конфиги
├── config.example.toml
├── .env.example
├── .python-version                       # 3.13
└── README.md
```

---

## Документация и ссылки

- Методика эксперимента: `docs/Методика_сравнения_точности_EFT_RS3.docx`
- Анализ open-source стека: `docs/Open-source_стек_для_ntrip-accuracy-monitor__Python_3_13_.md`
- Руководство по эксплуатации EFT RS3: `docs/Руководство_по_эксплуатации_EFT_RS3.docx`
- RTCM STANDARD 10403.x (DGNSS v3) и 10410.x (NTRIP) — PDF в `docs/`
