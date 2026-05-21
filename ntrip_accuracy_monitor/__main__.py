"""Точка входа для `python -m ntrip_accuracy_monitor`.

Делегирует в ntrip_accuracy_monitor.cli.__main__:main. Та же функция
вызывается через entry-point скрипт `ntrip-accuracy-monitor`, прописанный
в [project.scripts] pyproject.toml.
"""

from ntrip_accuracy_monitor.cli.__main__ import main

if __name__ == "__main__":
    main()
