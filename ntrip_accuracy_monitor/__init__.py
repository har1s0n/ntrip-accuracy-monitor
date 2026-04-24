"""ntrip-accuracy-monitor — мониторинг NTRIP/NMEA и расчёт метрик точности ГНСС.

Пакет разделён на слои:

* ``transport``    — сетевые адаптеры (NTRIP-клиент/кастер, NMEA TCP-ридер);
* ``protocols``    — адаптеры поверх внешних парсеров (pynmeagps, pyrtcm);
* ``domain``       — доменная модель (Epoch, SolutionMode, шкалы времени);
* ``persistence``  — слой хранения (asyncpg-репозиторий, DDL);
* ``metrics``      — расчёт HRMS/VRMS/CEP50/R95 и зависимостей ошибки;
* ``application``  — оркестрация, конфиг, логирование, сервис;
* ``cli``          — точка входа командной строки.
"""

__all__: list[str] = []
