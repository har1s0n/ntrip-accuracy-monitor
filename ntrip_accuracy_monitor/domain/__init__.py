"""Доменная модель — не зависит от транспорта и БД.

* ``Epoch`` — измерение на момент времени: позиция, решение, возраст поправок,
  число спутников, DOP'ы, σ по ENU;
* ``SolutionMode`` — SPP / DGNSS / RTK float / RTK fixed / invalid
  (соответствует GGA quality indicator 0/1/2/4/5);
* шкалы времени — UTC, GPS time, GLONASS time; все ``datetime`` TZ-aware.
"""
