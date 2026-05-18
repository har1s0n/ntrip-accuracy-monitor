# captures/

Локальное хранилище записей RTCM и NMEA, снятых с реального оборудования.
В git попадает только этот файл — содержимое подкаталогов игнорируется.

## Что класть

| Расширение | Содержимое                                                  |
| --- |-------------------------------------------------------------|
| `.bin`   | Сырые байты RTCM-потока (3.x и/или 2.x), как пришли по сети |
| `.nmea`  | Текстовый NMEA-захват, по одному сообщению на строку        |
| `.txt`   | Sourcetable, конфиги, meta-файлы захвата                    |

## Именование каталогов

captures/
└── lab_YYYYMMDD_HHMMSS/
├── meta.txt                       что и когда записано
├── session_a_base.bin             RTCM от базы, сессия A (RTK)
├── session_a_rover2_rtk.nmea      NMEA RTK-ровера, сессия A
├── session_a_rover3_spp.nmea      NMEA SPP-ровера, сессия A
├── session_b_base.bin             RTCM от базы, сессия B (DGNSS)
├── session_b_rover2_dgnss.nmea    NMEA DGNSS-ровера, сессия B
└── session_b_rover3_spp.nmea      NMEA SPP-ровера, сессия B

## meta.txt — обязательный

Минимум: дата, состав оборудования, конфигурация каждого приемника,
фактический результат (что в захвате реально оказалось — режимы,
переходы, аномалии).

## Воспроизведение локально

```python
from pathlib import Path
from ntrip_accuracy_monitor.tools.replay import FileRtcmSource, NmeaReplayServer

src = FileRtcmSource(Path("captures/lab_20260514_135834/session_a_base.bin"))
server = NmeaReplayServer(
    Path("captures/lab_20260514_135834/session_a_rover2_rtk.nmea"),
    host="127.0.0.1",
    port=20002,
)
```
