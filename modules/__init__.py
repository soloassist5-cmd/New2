"""Импорт модуля регистрирует его команды; setup(client) вешает хендлеры событий.

Порядок хендлеров задаёт кортеж MODULES (а не порядок импортов, который
переставляет линтер): мут-фильтр должен отработать раньше кэширования, иначе
перехваченное сообщение попадёт в журнал удалённых как «удалено собеседником».
Порядок разделов в `.help` задан отдельно — в dispatcher.CAT_ORDER.
"""
from __future__ import annotations

from modules import (
    admin,  # noqa: F401
    afk,  # noqa: F401
    antidelete,  # noqa: F401
    info,  # noqa: F401
    mute,  # noqa: F401,I001
    notes,  # noqa: F401
    purge,  # noqa: F401
    system,  # noqa: F401
)

MODULES = (mute, antidelete, purge, admin, afk, notes, info, system)


def setup_all(client) -> None:
    for module in MODULES:
        setup = getattr(module, "setup", None)
        if setup is not None:
            setup(client)
