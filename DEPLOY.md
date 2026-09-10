# Деплой на бесплатный хостинг

Юзербот — это **долгоживущий процесс**, а не веб-приложение по запросу. Отсюда
два требования, по которым отсеивается большинство «бесплатных» платформ:
процесс не должен засыпать, а состояние (сессия + база) должно переживать
пересоздание контейнера.

Вторую проблему проект решает сам: строка сессии лежит в переменной окружения, а
база выгружается в лог-чат Telegram (`BACKUP_EVERY_MIN`) и подтягивается оттуда
при старте (`RESTORE_ON_START`). Поэтому эфемерный диск не страшен — важна
только первая проблема, засыпание.

## Сравнение вариантов

| Платформа | Деплой из GitHub | Засыпает | Карта | Итог |
|---|---|---|---|---|
| **Koyeb** | да, автодеплой на push | нет | нет | **рекомендуется** |
| Render (free web) | да, автодеплой на push | да, через 15 мин | нет | годится с пингом извне |
| Fly.io | через GitHub Actions | нет, если `min_machines_running=1` | да | запасной вариант |
| Oracle Cloud Always Free | через Actions по SSH | нет | да | максимум контроля |
| Railway / Heroku | да | — | — | бесплатных тарифов нет |
| GitHub Actions как хост | — | лимит 6 ч на job | — | не подходит, нарушает правила |
| Vercel / Netlify | да | serverless | нет | долгоживущего процесса нет в принципе |

Ниже — рабочая инструкция для основного варианта и двух запасных.

---

## Шаг 0. Подготовка (одинаково для всех)

1. `my.telegram.org` → **API development tools** → получить `API_ID` и `API_HASH`.
2. Создать **приватный канал** для логов, добавить в него себя, скопировать его
   id (например, через `.id` уже после запуска, или ботом `@username_to_id_bot`).
   Можно обойтись значением `me` — логи пойдут в «Избранное».
3. Локально сгенерировать строку сессии:

   ```bash
   pip install -r requirements.txt
   python scripts/gen_session.py
   ```

   Строка = полный доступ к аккаунту. Её место — только в секретах хостинга,
   никогда в git.
4. Форкнуть/запушить репозиторий на GitHub.

---

## Вариант A. Koyeb — рекомендуемый

Бесплатный инстанс работает круглосуточно и не засыпает, деплой идёт прямо из
GitHub-репозитория по Dockerfile.

**Через веб-интерфейс (проще):**

1. koyeb.com → *Create Service* → *GitHub* → выбрать репозиторий, ветку `main`.
2. Builder: **Dockerfile**. Instance: **Free**. Region: `fra`.
3. *Ports*: `8080`, protocol `HTTP`, route `/`. Health check: путь `/health`.
4. *Environment variables* — добавить как **secret**: `API_ID`, `API_HASH`,
   `SESSION`, `LOG_CHAT`; как обычные: `BACKUP_EVERY_MIN=30`,
   `RESTORE_ON_START=1`, `PORT=8080`.
5. Deploy. Дальше каждый push в `main` пересобирает сервис автоматически.

**Через GitHub Actions** (в репозитории уже лежит
[`.github/workflows/deploy-koyeb.yml`](.github/workflows/deploy-koyeb.yml)):

1. В Koyeb: *Secrets* → создать `api-id`, `api-hash`, `tg-session`, `log-chat`.
2. В Koyeb: *Account settings* → *API* → создать токен.
3. В GitHub: *Settings → Secrets and variables → Actions* → добавить
   `KOYEB_TOKEN`.
4. Push в `main` — workflow сам создаст/обновит сервис. Значения `@api-id` в
   файле workflow — это ссылки на секреты Koyeb, самих значений в репозитории нет.

Проверка: `https://<app>-<org>.koyeb.app/health` должен отдавать
`{"status":"ok","connected":true,...}`, а в лог-чат прилетит «🛡 Guard запущен».

---

## Вариант B. Render — если Koyeb недоступен

Бесплатный тариф даёт только **web service**, и он засыпает после 15 минут без
входящих HTTP-запросов. Лечится внешним пингом.

1. В дашборде: *Blueprints* → *New Blueprint Instance* → выбрать репозиторий.
   Render подхватит [`render.yaml`](render.yaml) и создаст сервис.
2. Заполнить секреты `API_ID`, `API_HASH`, `SESSION`, `LOG_CHAT`
   (в `render.yaml` они помечены `sync: false`, то есть задаются только в UI).
3. Завести на [UptimeRobot](https://uptimerobot.com) (или cron-job.org) HTTP(s)
   монитор на `https://<service>.onrender.com/health` с интервалом 5 минут —
   именно он не даёт контейнеру уснуть.
4. Автодеплой на push в `main` включён (`autoDeploy: true`).

Минус варианта: холодный старт после простоя рвёт MTProto-сессию, и события,
пришедшие в этот момент, теряются. Для антиудаления это означает пропуски.

---

## Вариант C. Fly.io — если нужен постоянный диск

Даёт настоящий том, так что бэкап в Telegram становится подстраховкой, а не
необходимостью.

```bash
fly launch --no-deploy          # подхватит fly.toml
fly volumes create guard_data --size 1 --region fra
fly secrets set API_ID=... API_HASH=... SESSION=... LOG_CHAT=...
fly deploy
```

Для деплоя из GitHub: создать токен `fly tokens create deploy`, положить его в
секрет `FLY_API_TOKEN`, запускать
[`.github/workflows/deploy-fly.yml`](.github/workflows/deploy-fly.yml) вручную
или добавить туда триггер на push. Ключевая настройка в `fly.toml` —
`auto_stop_machines = false`: без неё машина будет останавливаться.

---

## Вариант D. Своя машина — самый надёжный

Oracle Cloud Always Free (ARM-инстанс, бессрочно бесплатно), старый Android с
Termux или Raspberry Pi. Никаких засыпаний и лимитов, диск свой.

```bash
git clone <репозиторий> && cd new2
cp .env.example .env && nano .env
docker compose up -d --build
```

`restart: unless-stopped` в [`docker-compose.yml`](docker-compose.yml) поднимет
контейнер после перезагрузки. Обновление — `.update` прямо из Telegram
(git pull + перезапуск) либо `docker compose pull && docker compose up -d`.

На Android: `pkg install python git`, `termux-wake-lock`, автозапуск через
Termux:Boot.

---

## Эксплуатация

**Проверка живости.** `/health` отдаёт `connected`, аптайм и число мутов. В
Telegram — `.ping` и `.alive`.

**Данные.** `.stats` — размер базы и счётчики. `.backup` — выгрузить базу в
лог-чат прямо сейчас. Старые бэкапы чистятся автоматически, хранятся последние 5.
`.cleanup` — принудительно применить TTL (кэш 48 ч, журнал удалённых 30 дней —
настраивается в `.env`).

**Логи.** `.logs 300` пришлёт хвост файла. На хостинге — обычный вывод в stdout.
Ошибки команд с трейсбеком дублируются в лог-чат.

**Что делать, если сессия отвалилась.** Telegram может разлогинить сессию
(смена пароля, ручное завершение сеанса в настройках). Признак — контейнер
перезапускается по кругу, `/health` отдаёт 503. Лечится перегенерацией
`SESSION` локально и обновлением секрета на хостинге.

**Первый запуск на новом хосте.** Базы ещё нет, `RESTORE_ON_START=1` попробует
скачать её из лог-чата — если бэкапов там нет, просто создастся пустая. Это
нормально.
