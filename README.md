# ntrip-accuracy-monitor

Мониторинг NTRIP-потока дифференциальных поправок и NMEA-телеметрии трёх ГНСС-приёмников **EFT RS3**, сохранение эпох в PostgreSQL и расчёт метрик точности позиционирования (HRMS, VRMS, CEP50, R95, 3D-RMS) с анализом зависимости ошибки от возраста поправок (`age_of_corrections`) и типа решения (`solution_mode`: SPP / DGNSS / RTK float / RTK fixed).

Цель проекта — инструментальное сравнение точности режимов SPP, DGNSS и RTK на реальных данных EFT RS3 в рамках двухпрогонного 24-часового эксперимента: одна общая антенна, три канала приёма, локальный NTRIP-кастер, пост-обработка эталона в RTKLIB. Методика эксперимента зафиксирована отдельным документом (см. `docs/`).

---

## Открытый стек и его авторы

Проект опирается на готовые open-source библиотеки — собственных парсеров NMEA/RTCM и собственного NTRIP-клиента мы не пишем.

- **[pynmeagps](https://github.com/semuconsulting/pynmeagps), [pyrtcm](https://github.com/semuconsulting/pyrtcm), [pygnssutils](https://github.com/semuconsulting/pygnssutils)** — парсеры NMEA-0183, RTCM 3.x и NTRIP-клиент v1/v2. Автор — **semuconsulting** (semuadmin). Лицензия BSD-3-Clause.
- **[pyproj](https://pyproj4.github.io/pyproj/)** — Python-обёртка над PROJ; трансформации LLH ↔ ECEF ↔ ENU. Сопровождается сообществом **pyproj4**.
- **[asyncpg](https://github.com/MagicStack/asyncpg)** — высокопроизводительный async-драйвер PostgreSQL. Автор — **MagicStack**.
- **[pydantic](https://docs.pydantic.dev/)** v2 — валидация конфигурации. Автор — **Samuel Colvin** и команда pydantic.
- **[NumPy](https://numpy.org/), [SciPy](https://scipy.org/), [pandas](https://pandas.pydata.org/), [Matplotlib](https://matplotlib.org/)** — научный стек Python.
- **[uv](https://github.com/astral-sh/uv)** — менеджер пакетов и окружений. Автор — **Astral** (создатели Ruff).
- **[Ruff](https://github.com/astral-sh/ruff), [mypy](https://mypy-lang.org/), [pytest](https://pytest.org/)** — линтер/форматтер, статическая типизация, тестовый фреймворк.

Почему именно такой стек: готовые, покрытые тестами парсеры от одного автора (semuconsulting) избавляют от ручного бит-уровневого разбора RTCM и checksum-валидации NMEA. Собственной остаётся только та логика, которая несёт смысл для работы: сшивка эпох, расчёт метрик, зависимость ошибки от возраста поправок.

---

## Требования

- **Python 3.13+** — установится автоматически через `uv`, системный Python не обязателен.
- **PostgreSQL 16+** — нативно, без контейнеров.
- **Git**.
- *(опционально)* **RTKLIB `rtkpost`** — для пост-обработки эталонных координат.

---

## Установка с нуля

Инструкция оформлена отдельно по ОС. Команды можно копировать целиком.

### Windows 10 / 11

**(a) Установка uv**

Открыть PowerShell. Если политика выполнения скриптов запрещает установку, разово разрешить подписанные скрипты для текущего пользователя:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Установить uv:

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

**Перезапустить терминал**, чтобы обновилась переменная `PATH`.

**(b) Проверка установки uv**

```powershell
uv --version
```

**(c) Установка Python 3.13 через uv**

```powershell
uv python install 3.13
```

`uv` сам скачает нужный интерпретатор в свой кэш — системный Python требуемой версии иметь не обязательно.

**(d) Установка PostgreSQL 16+**

Скачать официальный установщик EDB: <https://www.postgresql.org/download/windows/>. При установке **обязательно запомнить пароль суперпользователя `postgres`**. `pgAdmin 4` обычно ставится тем же установщиком.

Проверка:

```powershell
psql --version
```

**(e) Создание роли и базы данных**

1. Открыть `scripts\init_db.sql` в редакторе и заменить `CHANGE_ME_BEFORE_RUN` на реальный пароль. Этот же пароль позже пропишете в `.env`.
2. Выполнить скрипт одним из способов:

   Через `SQL Shell (psql)` из меню «Пуск», подключиться под `postgres`:

```
   \i C:/путь/к/проекту/scripts/init_db.sql
```

   Либо через pgAdmin: `Servers → PostgreSQL 16 → Databases → postgres → Query Tool` → File → Open → выбрать `scripts\init_db.sql` → `Execute`.

   Либо из PowerShell:

```powershell
   psql -U postgres -f scripts\init_db.sql
```

**(f) Клонирование и установка зависимостей**

```powershell
git clone <repo-url>
cd ntrip-accuracy-monitor
Copy-Item .env.example .env
# отредактировать .env и прописать тот же пароль, что был в init_db.sql
uv sync --extra dev
```

**(g) Проверка**

```powershell
uv run pytest
```

Smoke-тест должен пройти.

---

### Linux (Debian / Ubuntu и производные)

**(a) Установка uv**

```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
```

`uv` ставится в `~/.local/bin`. Если после установки команда `uv` не находится — убедиться, что `~/.local/bin` в `PATH`:

```bash
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  source ~/.bashrc
# для zsh — ~/.zshrc
```

**(b) Проверка**

```bash
  uv --version
```

**(c) Установка Python 3.13 через uv**

```bash
  uv python install 3.13
```

**(d) Установка PostgreSQL 16+**

```bash
  sudo apt update
  s udo apt install -y postgresql-16 postgresql-client-16
```

Служба запускается автоматически. Проверка:

```bash
  psql --version
  sudo systemctl status postgresql
```

**(e) Создание роли и базы**

1. Отредактировать `scripts/init_db.sql`, заменить `CHANGE_ME_BEFORE_RUN` на реальный пароль.
2. Выполнить:

```bash
   sudo -u postgres psql -f scripts/init_db.sql
```

**(f) Клонирование и установка зависимостей**

```bash
git clone <repo-url>
cd ntrip-accuracy-monitor
cp .env.example .env
# отредактировать .env, прописать тот же пароль
uv sync --extra dev
```

**(g) Проверка**

```bash
uv run pytest
```

---

### macOS 13+

**(a) Установка uv**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Альтернатива через Homebrew:

```bash
brew install uv
```

Перезапустить терминал.

**(b) Проверка**

```bash
uv --version
```

**(c) Установка Python 3.13**

```bash
uv python install 3.13
```

**(d) Установка PostgreSQL 16+**

```bash
brew install postgresql@16
brew services start postgresql@16
```

Проверка:

```bash
psql --version
```

**(e) Создание роли и базы**

1. Отредактировать `scripts/init_db.sql`, заменить `CHANGE_ME_BEFORE_RUN` на реальный пароль.
2. Выполнить:

```bash
   psql -U postgres -f scripts/init_db.sql
```

   Если подключение идёт под текущего пользователя (стандартная конфигурация Homebrew без отдельной роли `postgres`) — использовать `psql -U "$(whoami)" -d postgres -f scripts/init_db.sql`.

**(f) Клонирование и установка зависимостей**

```bash
git clone <repo-url>
cd ntrip-accuracy-monitor
cp .env.example .env
uv sync --extra dev
```

**(g) Проверка**

```bash
uv run pytest
```

---

## Команды для повседневной разработки

```bash
uv sync --extra dev                     # переустановить runtime + dev
uv sync --extra dev --extra analysis    # + пакеты для офлайн-анализа
uv run pytest                           # тесты
uv run ruff check .                     # линтер
uv run ruff format .                    # автоформат
uv run mypy                             # строгая проверка типов
```

---

## Типовые проблемы и решения

- **`uv: command not found`** после установки — перезапустить терминал. На Linux/macOS проверить, что `~/.local/bin` в `PATH`.
- **`psql: could not connect to server` / `connection refused`** — PostgreSQL не запущен:
  - Linux: `sudo systemctl start postgresql`
  - macOS: `brew services start postgresql@16`
  - Windows: Службы → `postgresql-x64-16` → Запустить.
- **`ModuleNotFoundError`** в тестах — забыли `uv sync` или пропустили `--extra dev`.
- **`role "ntrip" does not exist`** — не выполнен `scripts/init_db.sql` либо в нём не заменён плейсхолдер пароля.
- **Windows Firewall блокирует PostgreSQL при первом запуске** — разрешить локальный доступ.
- **`password authentication failed for user "ntrip"`** — пароль в `.env` не совпадает с тем, что был в `init_db.sql` в момент выполнения.
- **Тест падает на импорте `pygnssutils`/`pynmeagps`/`pyrtcm`** — проверить, что используется `uv run pytest`, а не `pytest` из системного Python.

---

## Структура проекта

```
ntrip-accuracy-monitor/
├── ntrip_accuracy_monitor/     # исходный код приложения
│   ├── transport/              # asyncio-адаптеры сети (NTRIP, NMEA, кастер)
│   ├── protocols/              # обёртки над pynmeagps / pyrtcm
│   ├── domain/                 # Epoch, SolutionMode, шкалы времени
│   ├── persistence/            # asyncpg-репозиторий, DDL
│   ├── metrics/                # HRMS/VRMS/CEP50/R95, проекции ENU
│   ├── application/            # конфиг, логирование, оркестратор, сшивка
│   └── cli/                    # точка входа CLI
├── tests/
│   ├── fixtures/               # дампы NMEA/RTCM от EFT RS3 (пока пусто)
│   └── test_smoke.py           # smoke-тест окружения
├── scripts/
│   └── init_db.sql             # создание роли и БД
├── docs/                       # методика эксперимента, документация
├── pyproject.toml              # метаданные, зависимости, tool-конфиги
├── .env.example                # шаблон переменных окружения
├── .python-version             # 3.13
└── README.md
```

---

## Документация и ссылки

- Методика эксперимента: `docs/Методика_сравнения_точности_EFT_RS3.docx`
- Анализ open-source стека: `docs/Open-source_стек_для_ntrip-accuracy-monitor__Python_3_13_.md`
- Руководство по эксплуатации EFT RS3: `docs/Руководство_по_эксплуатации_EFT_RS3.docx`
- RTCM STANDARD 10403.x (DGNSS v3) — см. PDF в `docs/`
