"""Иерархия исключений NMEA-уровня.

Не наследуется от pynmeagps.* — чужие исключения оборачиваем,
не пробрасываем за пределы этого пакета.
"""


class NmeaError(Exception):
    """Базовое исключение NMEA-уровня."""


class NmeaChecksumError(NmeaError):
    """Невалидная XOR-сумма; строка должна быть отброшена."""


class NmeaParseError(NmeaError):
    """Сообщение распознано как NMEA, но поля не извлекаются."""


class NmeaUnsupportedTalkerError(NmeaError):
    """Неподдерживаемый talker-id (программная ошибка вызывающего).

    Поднимается прямыми конвертерами `nmea_to_*`, не функцией
    `parse_line` (та возвращает None для нерелевантных сообщений).
    """
