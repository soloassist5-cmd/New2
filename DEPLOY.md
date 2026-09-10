# Деплой на бесплатный хостинг

Боту нужен **долгоживущий процесс**: он держит long polling к Bot API. Отсюда
главное требование к площадке — процесс не должен засыпать. Постоянный диск
желателен, но не обязателен: база регулярно уезжает копией в Telegram, и её
можно вернуть, переслав файл боту.

## Шаг 0. Подготовка — две минуты

Для основного режима нужен **один** секрет.

1. [@BotFather](https://t.me/BotFather) → `/newbot` → имя и юзернейм → он
   пришлёт токен вида `8123456789:AAH...`. Это `BOT_TOKEN`.
2. Форкнуть/запушить репозиторий на GitHub.

Нужен **Telegram Premium** — Business-раздел в настройках есть только у
подписки. Если Premium нет, разверните режим юзербота: возьмите `API_ID` и
`API_HASH` на `my.telegram.org`, выполните локально
`python scripts/gen_session.py` и добавьте `SESSION` в секреты.

## Сравнение площадок

| Платформа | Деплой из GitHub | Засыпает | Диск | Карта |
|---|---|---|---|---|
| **Koyeb** | да, автодеплой на push | нет | эфемерный | нет |
| Render (free web) | да, автодеплой на push | через 15 мин | эфемерный | нет |
| Fly.io | через GitHub Actions | нет | **постоянный том** | да |
| Oracle Cloud Always Free | через Actions по SSH | нет | свой | да |
| Railway / Heroku | да | — | — | бесплатных тарифов нет |
| GitHub Actions как хост | — | лимит 6 ч на job | — | нарушает правила |
| Vercel / Netlify | да | serverless | — | долгоживущего процесса нет |

---

## Вариант A. Koyeb — рекомендуемый

Бесплатный инстанс работает круглосуточно и не засыпает, сборка идёт по
Dockerfile прямо из репозитория.

**Через веб-интерфейс:**

1. koyeb.com → *Create Service* → *GitHub* → репозиторий, ветка `main`.
2. Builder: **Dockerfile**. Instance: **Free**. Region: `fra`.
3. *Ports*: `8080`, protocol `HTTP`, route `/`. Health check: путь `/health`.
4. *Environment variables* → как **secret**: `BOT_TOKEN`.
   Как обычные: `BACKUP_EVERY_MIN=30`, `PORT=8080`.
5. Deploy. Каждый push в `main` пересобирает сервис автоматически.

**Через GitHub Actions** (workflow уже в репозитории):

1. В Koyeb: *Secrets* → создать `bot-token`.
2. В Koyeb: *Account settings* → *API* → создать токен.
3. В GitHub: *Settings → Secrets and variables → Actions* → `KOYEB_TOKEN`.
4. Push в `main` — [`deploy-koyeb.yml`](.github/workflows/deploy-koyeb.yml)
   создаст или обновит сервис. `@bot-token` в файле — ссылка на секрет Koyeb,
   самого значения в репозитории нет.

Проверка: `https://<app>-<org>.koyeb.app/health` отдаёт
`{"status":"ok","bot":true,...}`. Дальше — Start в чате с ботом и подключение
через Настройки → Telegram для бизнеса → Чат-боты.

---

## Вариант B. Render

Бесплатный тариф даёт только web-сервис, засыпающий после 15 минут без
входящих HTTP-запросов. Лечится внешним пингом.

1. *Blueprints* → *New Blueprint Instance* → выбрать репозиторий, Render
   подхватит [`render.yaml`](render.yaml).
2. Заполнить секрет `BOT_TOKEN` (в файле он помечен `sync: false`, то есть
   задаётся только в UI).
3. Завести на [UptimeRobot](https://uptimerobot.com) HTTP-монитор на
   `https://<service>.onrender.com/health` с интервалом 5 минут — он и не даёт
   контейнеру уснуть.

Минус: холодный старт после простоя обрывает polling, и события, пришедшие в
этот момент, теряются. Для антиудаления это означает пропуски.

---

## Вариант C. Fly.io — если нужен постоянный диск

Единственный из бесплатных, где база живёт на томе и переживает передеплой без
ручных действий.

```bash
fly launch --no-deploy            # подхватит fly.toml
fly volumes create guard_data --size 1 --region fra
fly secrets set BOT_TOKEN=...
fly deploy
```

Для деплоя из GitHub: `fly tokens create deploy`, положить в секрет
`FLY_API_TOKEN`, запускать [`deploy-fly.yml`](.github/workflows/deploy-fly.yml).
Ключевая настройка — `auto_stop_machines = false`, иначе машина остановится.

---

## Вариант D. Своя машина

Oracle Cloud Always Free, старый Android с Termux, Raspberry Pi.

```bash
git clone <репозиторий> && cd new2
cp .env.example .env && nano .env
docker compose up -d --build
```

`restart: unless-stopped` поднимет контейнер после перезагрузки.

---

## Эксплуатация

**Проверка живости.** `/health` отдаёт `bot`, `business_connections`,
`userbot`, аптайм и число мутов. В Telegram — `/status` в чате с ботом.

**Данные на эфемерном диске.** Раз в `BACKUP_EVERY_MIN` минут бот присылает
себе копию базы файлом. После передеплоя перешлите этот файл боту — он
проверит его целостность и подставит вместо текущей базы; битый или чужой файл
он отклонит, ничего не тронув. Вручную копию можно запросить командой
`/backup`.

**Отчёты не приходят.**
1. `/health` → `bot: true`? Если нет — неверный `BOT_TOKEN`.
2. `business_connections: 0` → бот не подключён к чатам, см. `/connect`.
3. `/status` покажет выданные права. Нет «удалять сообщения собеседника» —
   не будет работать `.mute`; нет «читать сообщения» — не будет вообще ничего.
4. `bot_reachable: false` → бот не может писать вам первым, нажмите Start.

**Два экземпляра одновременно.** Bot API не разрешает двум процессам читать
апдейты одного бота: в логах появится `409` и предупреждение. Убедитесь, что
старый деплой остановлен.

**Смена токена.** Достаточно обновить `BOT_TOKEN` в секретах и передеплоить;
подключение Business при этом слетит — бот придётся подключить заново.
