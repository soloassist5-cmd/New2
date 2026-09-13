"""Отчёт по истории имён — по одному человеку и по всем сразу.

Записывает её бот сам: при каждом сообщении сверяет имя и @username с тем, что
видел в прошлый раз. Наружу за историей он не ходит, поэтому здесь только
собственные наблюдения и только с момента, как бот подключён.
"""
from __future__ import annotations

import db
from core import fmt

LIMIT = 20               # длиннее — уже не отчёт, а лента
PER_PERSON = 6           # столько имён на человека в общем списке

EMPTY_ALL = ("🏷 **Никто пока не переименовывался.**\n\n"
             "Я запоминаю имя и @username при каждом сообщении и отмечаю, когда "
             "они меняются. История копится с того момента, как я подключён, — "
             "задним числом её взять неоткуда.")
EMPTY_ONE = ("🏷 **{who}** — имя при мне не менялось.\n\n"
             "_Записано только то, что я видел сам._")


def _name(row) -> str:
    handle = f" (@{row['username']})" if row["username"] else ""
    return f"{row['name'] or '—'}{handle}"


def _chain(rows) -> list[str]:
    """Цепочка имён от старого к новому, свежее — последним."""
    lines = []
    for i, row in enumerate(rows):
        if i == 0:
            lines.append(f"  `{fmt.hm(row['first_seen'])}` {_name(row)} "
                         f"_— так звали, когда я его увидел_")
        else:
            lines.append(f"  `{fmt.hm(row['first_seen'])}` → {_name(row)}")
    return lines


async def one(owner_id: int, user_id: int) -> str:
    """История имён конкретного человека."""
    rows = list(reversed(await db.aliases(owner_id, user_id, LIMIT)))
    # name_of смотрит в журналы; если человек только писал, имя знает лишь
    # история — и в ответе «имя не менялось» оно как раз и нужно.
    who = (await db.name_of(owner_id, user_id)
           or (rows[-1]["name"] if rows else None) or f"id {user_id}")
    if len(rows) < 2:
        return EMPTY_ONE.format(who=who)

    changes = len(rows) - 1
    head = (f"🏷 **{_name(rows[-1])}**\n🆔 `{user_id}`\n\n"
            f"Менял(а) имя при мне **{changes}** "
            f"{fmt.plural(changes, ('раз', 'раза', 'раз'))}:")
    return fmt.truncate("\n".join([head, *_chain(rows)]), 3500)


async def everyone(owner_id: int) -> str:
    """Все, кто переименовывался. Кто сменил имя недавно — выше."""
    rows = await db.renames(owner_id)
    if not rows:
        return EMPTY_ALL

    people: dict[int, list] = {}
    for row in rows:
        people.setdefault(row["user_id"], []).append(row)

    # Порядок — по свежести последнего переименования, а не по номеру.
    order = sorted(people.items(), key=lambda item: item[1][-1]["first_seen"],
                   reverse=True)

    total = len(order)
    lines = [f"🏷 **Переименовывались: {total}**", ""]
    for user_id, chain in order[:LIMIT]:
        shown = chain[-PER_PERSON:]
        skipped = len(chain) - len(shown)
        lines.append(f"👤 **{_name(chain[-1])}** · `{user_id}`")
        if skipped:
            lines.append(f"  _…и раньше ещё {skipped}_")
        lines += _chain(shown) if not skipped else [
            f"  `{fmt.hm(row['first_seen'])}` → {_name(row)}" for row in shown]
        lines.append("")

    if total > LIMIT:
        lines.append(f"_Показаны {LIMIT} из {total}. По одному человеку — "
                     f"`.names <id>`._")
    return fmt.truncate("\n".join(lines).rstrip(), 3500)
