"""Управление самим ботом."""
from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import time

import config
import db
from core import backup, dispatcher, fmt, state
from core.dispatcher import Ctx, command

CAT = "Система"
LOG_FILE = config.ROOT / "data" / "guard.log"


@command("help", args="[команда]", cat=CAT, aliases=["h", "хелп"],
         desc="список команд или справка по одной")
async def cmd_help(ctx: Ctx) -> None:
    await ctx.done(dispatcher.help_text(ctx.args[0] if ctx.args else None))


@command("alive", args="", cat=CAT, desc="короткая карточка состояния")
async def cmd_alive(ctx: Ctx) -> None:
    s = await db.stats()
    await ctx.done(
        "🛡 **Guard на связи**\n"
        f"⏱ {fmt.uptime(time.time() - state.start_time)}\n"
        f"🗂 кэш: {s['cached']} · 🗑 сохранено: {s['deleted']} · 🔇 мутов: {s['mutes']}\n"
        f"🐍 Python {sys.version.split()[0]}"
    )


@command("backup", args="", cat=CAT, desc="выгрузить базу в лог-чат прямо сейчас")
async def cmd_backup(ctx: Ctx) -> None:
    await ctx.edit("🗄 Делаю бэкап…")
    ok = await backup.upload()
    await ctx.done("🗄 Бэкап отправлен в лог-чат." if ok
                   else "⚠️ Бэкап не удался, смотрите логи.", delete_after=8)


@command("cleanup", args="", cat=CAT, desc="принудительная чистка кэша по TTL")
async def cmd_cleanup(ctx: Ctx) -> None:
    removed, purged = await db.cleanup()
    await ctx.done(f"🧹 Кэш: -{removed}, журнал: -{purged}.", delete_after=8)


@command("logs", args="[N]", cat=CAT, desc="последние N строк лога файлом")
async def cmd_logs(ctx: Ctx) -> None:
    lines = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 200
    if not LOG_FILE.exists():
        await ctx.fail("Файл лога пуст или не создан.")
        return
    tail = LOG_FILE.read_text(errors="replace").splitlines()[-lines:]
    data = io.BytesIO("\n".join(tail).encode())
    data.name = "guard.log"
    await ctx.msg.delete()
    await ctx.client.send_file(ctx.chat_id, data, caption=f"📝 Последние {len(tail)} строк")


@command("restart", args="", cat=CAT, desc="перезапустить процесс")
async def cmd_restart(ctx: Ctx) -> None:
    await ctx.edit("♻️ Перезапуск…")
    await db.kv_set("restart_chat", str(ctx.chat_id))
    await db.kv_set("restart_msg", str(ctx.msg.id))
    await backup.upload()
    await db.close()
    os.execv(sys.executable, [sys.executable, str(config.ROOT / "main.py")])


@command("update", args="", cat=CAT, desc="git pull и перезапуск")
async def cmd_update(ctx: Ctx) -> None:
    if not (config.ROOT / ".git").exists():
        await ctx.fail("Это не git-репозиторий — обновляйтесь через передеплой хоста.")
        return
    await ctx.edit("⬇️ Забираю обновления…")
    try:
        out = subprocess.run(["git", "pull", "--ff-only"], cwd=config.ROOT,
                             capture_output=True, text=True, timeout=90)
    except Exception as e:                                   # noqa: BLE001
        await ctx.fail(f"git pull не запустился: `{e}`")
        return
    result = (out.stdout + out.stderr).strip()
    if "Already up to date" in result or "Уже актуально" in result:
        await ctx.done(f"✅ Обновлений нет.\n```\n{fmt.truncate(result, 800)}\n```",
                       delete_after=12)
        return
    await ctx.edit(f"✅ Обновлено, перезапускаюсь…\n```\n{fmt.truncate(result, 800)}\n```")
    await asyncio.sleep(1)
    await db.close()
    os.execv(sys.executable, [sys.executable, str(config.ROOT / "main.py")])
