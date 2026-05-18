"""Корневой conftest. Подгружает плагины с фикстурами из подпакетов.

pytest подхватывает pytest_plugins только из корневого conftest (этого
файла или conftest.py в корне репозитория), но не из вложенных.
"""

pytest_plugins = ["tests.persistence.conftest"]
