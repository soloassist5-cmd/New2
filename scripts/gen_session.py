"""Генерация строки сессии (StringSession).

Запускать локально: python scripts/gen_session.py
Полученную строку положить в SESSION в .env / секреты хостинга.
Строка = полный доступ к аккаунту, никому не показывайте и не коммитьте.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon.sessions import StringSession  # noqa: E402
from telethon.sync import TelegramClient  # noqa: E402

try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv()
except ImportError:
    pass


def ask(name: str, env: str) -> str:
    value = os.getenv(env, "").strip()
    return value or input(f"{name}: ").strip()


def main() -> None:
    api_id = int(ask("API_ID", "API_ID"))
    api_hash = ask("API_HASH", "API_HASH")

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = client.get_me()
        print("\n" + "=" * 60)
        print(f"Вошли как: {me.first_name} (@{me.username or '—'}, id {me.id})")
        print("=" * 60)
        print("\nSESSION=" + client.session.save())
        print("\nСкопируйте строку в .env или в переменные окружения хостинга.")
        print("Никому её не передавайте: это доступ к аккаунту.\n")


if __name__ == "__main__":
    main()
