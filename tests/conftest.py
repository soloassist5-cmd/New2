"""Общее окружение для тестов: фиктивные креды и временная БД."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "0" * 32)
os.environ.setdefault("SESSION", "test-session")
os.environ.setdefault("ANIM_DELAY", "0")
os.environ["OWNER_ID"] = "0"      # администратора тесты задают сами
os.environ["BACKUP_EVERY_MIN"] = "0"   # фоновые копии включает только их тест
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.sqlite3")

import pytest  # noqa: E402 — только после того, как окружение готово


@pytest.fixture(autouse=True)
def _clean_process_caches():
    """Кэши живут в модулях, а база у каждого теста своя — иначе течёт."""
    import db

    db.forget_aliases()
    yield
    db.forget_aliases()
