"""Транспортный слой — сетевой ввод-вывод на asyncio.

* NTRIP-клиент — обёртка над ``pygnssutils.GNSSNTRIPClient``;
* NMEA-ридер — ``asyncio.StreamReader`` с line-framing и watchdog'ом;
* NTRIP-кастер — собственная компактная реализация на ``asyncio`` для
  раздачи RTCM-потока роверу №2 и монитору (аудит).
"""
