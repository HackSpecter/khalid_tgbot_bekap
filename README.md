# tgbackup — личный бэкап переписок Telegram

Юзербот на Telethon (MTProto), который работает от вашего аккаунта 24/7 и:

- сохраняет все входящие и исходящие сообщения в SQLite (WAL);
- сразу скачивает медиа (фото, видео, голосовые, кружки, документы, стикеры) в `data/media/<chat_id>/`;
- ведёт историю правок и присылает уведомление «было → стало»;
- помечает удалённые сообщения (из базы ничего не стирается) и присылает их вам вместе с медиа;
- при массовом удалении (≥ `BULK_DELETE_THRESHOLD` за `BULK_DELETE_WINDOW_SEC` секунд) присылает полный HTML-экспорт чата;
- отвечает на команды в отдельном боте-уведомителе.

Бот **ничего не делает от вашего имени**: не отмечает прочитанным, не отвечает, не меняет статус «в сети». Самоуничтожающиеся (view-once / таймер) медиа не сохраняются.

> ⚠️ **Файл сессии `data/<SESSION_NAME>.session` = полный доступ к вашему аккаунту.**
> Любой, кто его получит, сможет читать и писать от вашего имени без кода и 2FA.
> Никогда не коммитьте его, не пересылайте и не храните в бэкапах без шифрования.
> Если файл утёк — Telegram → Настройки → Устройства → завершите сеанс «tgbackup»/сервер.

## Структура

```
tgbackup/
  config.py      конфигурация из .env, логирование с ротацией
  db.py          схема SQLite и все запросы (aiosqlite, WAL)
  media.py       определение типа медиа и скачивание с лимитом размера
  handlers.py    Tracker: новые / изменённые / удалённые сообщения
  deletions.py   окно группировки удалений, точечные уведомления и bulk-экспорт
  exporter.py    HTML-экспорт в ZIP (с разбиением на части)
  templates/chat.html
  notifier.py    отправка уведомлений (бот или «Избранное»)
  commands.py    /export /deleted /stats /search /sync
  sync.py        загрузка истории с обработкой FloodWait
  main.py        сборка и запуск, автопереподключение, graceful shutdown
login.py         интерактивный первый вход
initial_sync.py  первичная загрузка истории
tests/           pytest для БД, экспортера и удалений
```

Все данные лежат в `data/`: `backup.db`, `media/`, `exports/`, `logs/bot.log`, файлы сессий.

## 1. API_ID и API_HASH

1. Зайдите на <https://my.telegram.org> под своим номером.
2. **API development tools** → создайте приложение (название и short name любые, платформа — Other).
3. Скопируйте `api_id` и `api_hash` в `.env`.

## 2. Бот-уведомитель

Отдельный бот нужен, чтобы уведомления приходили обычными push-ами (сообщения в «Избранное» от себя не звенят).

1. Напишите [@BotFather](https://t.me/BotFather) → `/newbot` → получите токен.
2. Впишите его в `NOTIFY_BOT_TOKEN`.
3. **Обязательно** откройте своего бота и нажмите **Start** — иначе он не сможет вам писать.
4. `OWNER_ID` — ваш числовой id (его покажет `login.py`, или [@userinfobot](https://t.me/userinfobot)). Бот отвечает только этому id.

Если `NOTIFY_BOT_TOKEN` пустой, уведомления идут в «Избранное», а команды можно писать туда же.

## 3. Настройка

```bash
cp .env.example .env
nano .env
```

| Переменная | По умолчанию | Описание |
|---|---|---|
| `API_ID`, `API_HASH` | — | с my.telegram.org |
| `SESSION_NAME` | `userbot` | имя файла сессии в `data/` |
| `NOTIFY_BOT_TOKEN` | пусто | токен бота-уведомителя |
| `OWNER_ID` | — | ваш user id, обязателен при наличии токена |
| `TRACK_PRIVATE` / `TRACK_GROUPS` / `TRACK_CHANNELS` | `true` / `false` / `false` | какие чаты сохранять |
| `EXCLUDED_CHAT_IDS` | пусто | id через запятую, например `-1001234567890,777000` |
| `MEDIA_MAX_SIZE_MB` | `50` | файлы больше — только метаданные |
| `BULK_DELETE_THRESHOLD` | `10` | сколько удалений из одного чата считать «чат удалён» |
| `BULK_DELETE_WINDOW_SEC` | `30` | окно накопления удалений |
| `NOTIFY_OWN_ACTIONS` | `false` | уведомлять ли о правках/удалениях своих сообщений |
| `EXPORT_PART_SIZE_MB` | `49` | максимальный размер одной части ZIP |
| `TIMEZONE` | `UTC` | часовой пояс в уведомлениях и экспорте |

`chat_id` в формате Telethon: пользователи — положительные, группы — `-id`, каналы и супергруппы — `-100…`.

## 4. Запуск в Docker (рекомендуется)

```bash
git clone <repo> tgbackup && cd tgbackup
cp .env.example .env && nano .env
echo "UID=$(id -u)" >> .env && echo "GID=$(id -g)" >> .env
mkdir -p data
docker compose build
```

### Первый вход (интерактивно)

```bash
docker compose run --rm tgbackup python login.py
```

Введите номер телефона в формате `+992…`, код из Telegram и пароль 2FA, если он включён. Скрипт выведет ваш id — впишите его в `OWNER_ID`. Файл сессии создаётся с правами `600`.

### Первичная загрузка истории (по желанию)

Скрипт и основной бот используют один файл сессии, поэтому запускайте его **при остановленном** боте:

```bash
docker compose stop
docker compose run --rm tgbackup python initial_sync.py                    # все чаты по фильтрам TRACK_*
docker compose run --rm tgbackup python initial_sync.py @durov 123456789   # конкретные чаты
docker compose run --rm tgbackup python initial_sync.py --limit 5000 --no-media
docker compose up -d
```

Либо без остановки — командой `/sync all` в боте.

### Запуск

```bash
docker compose up -d
docker compose logs -f
```

Бот пришлёт «✅ Бэкап переписок запущен». При старте он догружает пропущенные за время простоя события (`catch_up`).

## 5. Альтернатива: systemd

```bash
sudo useradd -r -m -d /opt/tgbackup -s /usr/sbin/nologin tgbackup
sudo -u tgbackup git clone <repo> /opt/tgbackup
cd /opt/tgbackup
sudo -u tgbackup python3.12 -m venv .venv
sudo -u tgbackup .venv/bin/pip install -r requirements.txt
sudo -u tgbackup cp .env.example .env && sudo -u tgbackup nano .env
sudo -u tgbackup .venv/bin/python login.py
sudo cp deploy/tgbackup.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tgbackup
journalctl -u tgbackup -f
```

## 6. Команды бота

| Команда | Что делает |
|---|---|
| `/export <@username \| chat_id \| название>` | HTML-экспорт чата в ZIP (`chat.html` + `media/`), > 49 МБ — несколько частей |
| `/deleted <чат> [N]` | последние N (по умолчанию 30) удалённых сообщений |
| `/search <текст>` | поиск по тексту всех сохранённых сообщений (без учёта регистра) |
| `/stats` | количество сообщений, чатов, медиа, размер БД и папки медиа |
| `/sync <чат \| all> [лимит]` | догрузить историю в фоне |

Экспорт открывается в любом браузере: распакуйте архив и откройте `chat.html`. Удалённые сообщения обведены красным пунктиром с датой удаления, у изменённых — отметка и раскрывающаяся история правок.

## 7. Как работают удаления

Telegram для личных чатов и обычных групп присылает событие удаления **без chat_id**, только с id сообщений. Эти id уникальны в пределах аккаунта, поэтому сообщение ищется по `message_id` среди не-каналов. Для каналов и супергрупп chat_id приходит, и поиск идёт по паре `(chat_id, message_id)`.

События копятся `BULK_DELETE_WINDOW_SEC` секунд. Затем по каждому чату: если удалено меньше порога — приходит отдельное уведомление на каждое сообщение (с медиа файлом), иначе — полный экспорт чата с подписью «Похоже, чат с X был удалён/очищен».

Удалить сообщение, которое бот не успел увидеть (пришло и удалилось, пока бот был выключен), отследить нельзя — Telegram не отдаёт содержимое удалённых сообщений.

## 8. Обновление

```bash
cd ~/khalid_tgbot_bekap
git pull                      # или скопируйте новые файлы
docker compose build
docker compose up -d
```

Схема БД создаётся через `CREATE TABLE IF NOT EXISTS`, данные в `data/` сохраняются между обновлениями.

## 9. Бэкап самих данных

```bash
sqlite3 data/backup.db ".backup 'backup-$(date +%F).db'"
tar czf media-$(date +%F).tgz data/media
```

## 10. Тесты

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```
