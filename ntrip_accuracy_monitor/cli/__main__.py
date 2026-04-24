"""Точка входа CLI ntrip-accuracy-monitor (каркас)."""

from __future__ import annotations


def main() -> int:
    """Entry point CLI. Пока заглушка.

    Returns:
        Код возврата процесса (0 — успех).
    """
    # TODO(): парсинг аргументов (argparse),
    # загрузка config.toml через application.config,
    # запуск application.service.Service внутри asyncio.run().
    print("ntrip-accuracy-monitor: bootstrap OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
