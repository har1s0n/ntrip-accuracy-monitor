"""Расчёт метрик точности ГНСС.

* HRMS, VRMS, 2DRMS, 3D-RMS — (векторные операции над ENU);
* CEP50, R95 — ``scipy.stats.scoreatpercentile`` с методом Хэзена;
* трансформации LLH ↔ ECEF ↔ ENU — ``pyproj.Transformer``;
* зависимости ошибки от ``age_of_corrections`` и ``solution_mode`` —
  агрегирование через ``pandas`` (опциональный extras-группa ``analysis``).
"""
