# tgbackup — личный бэкап переписок Telegram с доказательной целостностью

Юзербот на Telethon (MTProto). Работает от вашего аккаунта 24/7 и:

- сохраняет все входящие и исходящие сообщения, включая сервисные (звонки, закрепления), пересылки, реакции, опросы, геолокации и контакты;
- сразу скачивает медиа (фото, видео, голосовые, кружки, документы, стикеры) и хранит их зашифрованными (AES-256-GCM);
- перехватывает view-once медиа (голосовые, фото, видео, кружки), если бот получил событие раньше, чем сообщение было открыто в клиенте;
- ведёт историю правок и присылает уведомление «было → стало»;
- помечает удалённые сообщения (из базы ничего не стирается) и присылает их вам вместе с медиа;
- при массовом удалении присылает полный экспорт чата;
- следит за профилями собеседников (имя, username, «о себе», фото) и сохраняет старые аватарки;
- ведёт **append-only хеш-цепочку** всех событий и раз в сутки заякоривает её через **RFC 3161 (FreeTSA)** и **OpenTimestamps (Bitcoin)**;
- делает экспорт в HTML + PDF с подписанным манифестом и автономным скриптом проверки.

Бот **ничего не делает от вашего имени**: не отмечает прочитанным, не отвечает, не отправляет статус «в сети».

> ⚠️ **Файл сессии `data/<SESSION_NAME>.session` = полный доступ к вашему аккаунту.**
> Любой, кто его получит, сможет читать и писать от вашего имени без кода и 2FA.
> Никогда не коммитьте его, не пересылайте и не храните в бэкапах без шифрования.
> Если файл утёк — Telegram → Настройки → Устройства → завершите сеанс сервера.

> ⚠️ **`ENCRYPTION_KEY` — единственный ключ к базе, медиа и ключу подписи.**
> Потеряете его — данные не восстановить. Сохраните копию офлайн (менеджер паролей, бумага).

## Структура

```
tgbackup/
  config.py        конфигурация из .env, логирование с ротацией
  migrations.py    версионированные миграции схемы (schema_migrations)
  db.py            SQLCipher/SQLite, запросы, конвертация plaintext → SQLCipher
  crypto.py        HKDF-ключи, AES-GCM хранилище медиа (Vault), подпись Ed25519
  evidence.py      append-only журнал с хеш-цепочкой и его проверка
  anchoring.py     RFC 3161 (FreeTSA) + OpenTimestamps, планировщик якорей
  profiles.py      история профилей, ежедневный обход
  extract.py       сервисные действия, реакции, пересылки
  media.py         определение типа медиа и скачивание с лимитом размера
  handlers.py      Tracker: новые / изменённые / удалённые / сервисные / реакции
  deletions.py     фиксация удалений и группировка уведомлений
  exporter.py      экспорт: HTML, PDF, manifest.json + подпись, срез цепочки, якоря
  maintenance.py   перенос данных версии 1 (шифрование медиа, backfill цепочки)
  standalone/verify_export.py   автономная проверка экспорта (кладётся в каждый ZIP)
  templates/       chat.html, chat_pdf.html, export_readme.txt
  notifier.py      уведомления (бот или «Избранное»)
  commands.py      команды бота
  sync.py          загрузка истории с обработкой FloodWait
  main.py          сборка и запуск, автопереподключение, graceful shutdown
login.py           интерактивный первый вход
initial_sync.py    первичная загрузка истории
verify.py          полная проверка архива
tests/             pytest
```

Данные лежат в `data/`: `backup.db` (SQLCipher), `media/` (зашифрованные `*.enc`), `anchors/` (якоря, `.tsr`, `.ots`), `keys/` (Ed25519), `exports/`, `logs/bot.log`, файлы сессий.

## 1. API_ID и API_HASH

1. Зайдите на <https://my.telegram.org> под своим номером.
2. **API development tools** → создайте приложение (название и short name любые, платформа — Other).
3. Скопируйте `api_id` и `api_hash` в `.env`.

## 2. Бот-уведомитель

Отдельный бот нужен, чтобы уведомления приходили обычными push-уведомлениями: сообщения в «Избранное» от самого себя не звенят.

1. Напишите [@BotFather](https://t.me/BotFather) → `/newbot` → получите токен.
2. Впишите его в `NOTIFY_BOT_TOKEN`.
3. **Обязательно** откройте своего бота и нажмите **Start**, иначе он не сможет вам писать.
4. `OWNER_ID` — ваш числовой id (его покажет `login.py`, или [@userinfobot](https://t.me/userinfobot)). Бот отвечает только этому id.

Если `NOTIFY_BOT_TOKEN` пустой, уведомления идут в «Избранное», а команды можно писать туда же.

## 3. Настройка

```bash
cp .env.example .env
echo "ENCRYPTION_KEY=$(openssl rand -hex 32)" >> .env   # и сохраните ключ офлайн!
nano .env
```

| Переменная | По умолчанию | Описание |
|---|---|---|
| `API_ID`, `API_HASH` | — | с my.telegram.org |
| `SESSION_NAME` | `userbot` | имя файла сессии в `data/` |
| `NOTIFY_BOT_TOKEN` | пусто | токен бота-уведомителя |
| `OWNER_ID` | — | ваш user id, обязателен при наличии токена |
| `ENCRYPTION_KEY` | — | ≥ 32 байт (hex/base64). Из него через HKDF выводятся ключ SQLCipher, ключ AES-GCM для медиа и пароль ключа подписи |
| `ALLOW_UNENCRYPTED` | `false` | разрешить запуск без шифрования (не рекомендуется) |
| `TRACK_PRIVATE` / `TRACK_GROUPS` / `TRACK_CHANNELS` | `true` / `false` / `false` | какие чаты сохранять |
| `EXCLUDED_CHAT_IDS` | пусто | id через запятую, например `-1001234567890,777000` |
| `MEDIA_MAX_SIZE_MB` | `50` | файлы больше — только метаданные |
| `BULK_DELETE_THRESHOLD` | `10` | сколько удалений из одного чата считать «чат удалён» |
| `BULK_DELETE_WINDOW_SEC` | `30` | окно группировки уведомлений об удалениях |
| `NOTIFY_OWN_ACTIONS` | `false` | уведомлять ли о правках/удалениях своих сообщений |
| `EXPORT_PART_SIZE_MB` | `49` | максимальный размер одной части ZIP |
| `EXPORT_PDF` | `true` | добавлять PDF в экспорт |
| `TIMEZONE` | `UTC` | местное время в уведомлениях и экспорте (UTC выводится всегда) |
| `TSA_ENABLED`, `TSA_URL` | `true`, FreeTSA | RFC 3161 метки времени |
| `OTS_ENABLED` | `true` | OpenTimestamps |
| `ANCHOR_HOUR` | `3` | час (местный), после которого делается ежедневный якорь |
| `OTS_UPGRADE_HOURS` | `6` | как часто проверять подтверждение OTS в Bitcoin |
| `PROFILE_TRACKING` | `true` | слежение за профилями |
| `PROFILE_ACTIVE_DAYS` | `30` | ежедневно проверять собеседников, писавших за N дней |
| `PROFILE_SWEEP_HOUR` | `4` | час ежедневного обхода профилей |
| `PROFILE_SWEEP_PAUSE` | `3` | пауза между запросами профилей, сек |

`chat_id` в формате Telethon: пользователи — положительные, группы — `-id`, каналы и супергруппы — `-100…`.

## 4. Запуск в Docker (рекомендуется)

```bash
git clone <repo> tgbackup && cd tgbackup
cp .env.example .env && nano .env
echo "ENCRYPTION_KEY=$(openssl rand -hex 32)" >> .env
echo "UID=$(id -u)" >> .env && echo "GID=$(id -g)" >> .env
mkdir -p data && chmod 700 data
docker compose build
```

### Первый вход (интерактивно)

```bash
docker compose run --rm tgbackup python login.py
```

Введите номер телефона в формате `+992…`, код из Telegram и пароль 2FA, если он включён. Скрипт выведет ваш id — впишите его в `OWNER_ID`. Файл сессии создаётся с правами `600`.

### Первичная загрузка истории (по желанию)

Скрипт и бот используют один файл сессии, поэтому запускайте его **при остановленном** боте:

```bash
docker compose stop
docker compose run --rm tgbackup python initial_sync.py                    # все чаты по фильтрам TRACK_*
docker compose run --rm tgbackup python initial_sync.py @username 123456789
docker compose run --rm tgbackup python initial_sync.py --limit 5000 --no-media
docker compose up -d
```

Либо без остановки — командой `/sync all` в боте. Загруженные так сообщения попадают в цепочку с пометкой `source: sync`: момент фиксации — время загрузки, а не время отправки.

### Запуск

```bash
docker compose up -d
docker compose logs -f
```

Бот пришлёт «✅ Бэкап переписок запущен». При старте он догружает пропущенные за время простоя события (`catch_up`).

## 5. Альтернатива: systemd

Нужны системные пакеты `openssl`, `libpango-1.0-0`, `libpangoft2-1.0-0`, `fonts-dejavu-core` (для PDF).

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
| `/export <@username \| chat_id \| название>` | сначала `PDF` (читать сразу на телефоне), следом ZIP с доказательствами: HTML, медиа, манифест с подписью, цепочка, метки времени |
| `/deleted <чат> [N]` | последние N (по умолчанию 30) удалённых сообщений |
| `/search <текст>` | поиск по тексту всех сохранённых сообщений (без учёта регистра) |
| `/stats` | сообщения, чаты, медиа, размер БД, записи цепочки, якоря |
| `/profile <чат>` | история изменений профиля собеседника |
| `/verify` | быстрая проверка хеш-цепочки |
| `/anchor` | заякорить цепочку прямо сейчас (`/anchor force` — повторно) |
| `/sync <чат \| all> [лимит]` | догрузить историю в фоне (вместе с отметками прочтения) |
| `/settings` | настройки кнопками: личные/группы/каналы, уведомления о своих действиях, списки заглушённых и исключённых чатов |
| `/mute`, `/unmute <чат>` | выключить / вернуть уведомления по чату (сохранение продолжается) |
| `/exclude`, `/include <чат>` | перестать / снова сохранять чат |

Под уведомлениями об удалении, правке и смене профиля есть кнопки: **📦 Экспорт чата**, **📜 Все удалённые**, **🔕 Не уведомлять об этом чате** (после нажатия меняется на «🔔 Вернуть уведомления»).

Настройки из `/settings` хранятся в базе и применяются сразу, без перезапуска. Значения из `.env` служат начальными. Кнопки работают только с ботом-уведомителем; в режиме «Избранное» доступны команды.

## 7. Сторож (мониторинг)

Рядом с ботом работает контейнер `khalid_tgbackup_watchdog`. Бот каждые 30 секунд пишет пульс в `data/heartbeat.json`, сторож раз в минуту его проверяет и при проблеме пишет вам **напрямую через Bot API** (в обход самого бота):

| Ситуация | Сообщение |
|---|---|
| бот упал, завис или не запускается (пульс старше 3 минут) | 🚨 Бэкап не отвечает |
| сеанс удалён из «Устройств» / разлогинен (бот проверяет каждые 10 минут) | 🚨 Сеанс Telegram завершён |
| нет связи с Telegram дольше 5 минут | ⚠️ Нет связи с Telegram |
| бот перезапускался 3+ раз за час | ⚠️ Бот перезапускался |
| метка времени не ставилась больше 36 часов, или FreeTSA не ответил | ⚠️ Метка времени… |
| диск заполнен больше чем на 90% | ⚠️ Диск заполнен |

Каждая проблема присылается один раз, затем напоминание раз в 6 часов, а после исправления приходит «✅ …». `docker ps` показывает у бота `(healthy)` или `(unhealthy)` по тому же пульсу.

Если отключится весь сервер, сторож не сработает: он живёт на том же сервере. На этот случай задайте `HEALTHCHECK_URL`, например бесплатный <https://healthchecks.io>. Сторож пингует его каждую минуту, а если пинги прекратятся, healthchecks.io пришлёт уведомление сам (в Telegram, на почту).

## 8. Доказательная целостность

### Журнал событий (`evidence_log`)

Источник истины — append-only таблица `evidence_log`. Каждое событие (`new`, `edit`, `delete`, `reaction`, `read`, `profile_change`, `anchor`) — новая запись:

```
payload      = канонический JSON {event, chat_id, message_id, observed_at,
                                  raw: message.to_dict(), media_sha256, data}
record_hash  = SHA-256(payload)
entry_hash   = SHA-256("<seq>:<prev_hash>:<record_hash>")     # prev_hash первой записи = 64 нуля
```

- `raw` — полный объект Telethon в момент получения (datetime → ISO-8601 UTC, bytes → `base64:…`).
- `media_sha256` — хеш **исходного** файла, посчитанный при скачивании, до шифрования.
- Удаление фиксируется сразу при получении события: id сообщений, сырое событие Telegram и `record_hash` удалённых записей.
- Правка — новая запись со старым текстом, хешем предыдущей версии и новым raw-объектом.
- Прочтение — когда собеседник читает ваши сообщения, Telegram присылает `UpdateReadHistoryOutbox`. В цепочку пишется запись `read` со списком прочитанных исходящих сообщений. В экспорте у них ✓✓ и «прочитано не позднее <время>». Время означает момент получения события: собеседник прочитал в этот момент или раньше. При `/sync` состояние берётся из диалога (`read_outbox_max_id`) с пометкой `source: sync`.

Целостность обеспечивает сама база. Триггер `evidence_log_chain_guard` принимает новую запись, только если её `seq` = последний + 1 и `prev_hash` = хеш последней записи. Поэтому даже несколько процессов одновременно (бот, `initial_sync.py`, ручной скрипт) не могут разветвить цепочку: проигравший перечитывает голову и повторяет. UPDATE и DELETE запрещены триггерами SQLite на `evidence_log`, `edits` и `profile_history`. Из `messages` ничего не удаляется, а идентификаторы исходного сообщения (`chat_id`, `message_id`, `date`, `first_seq`, `record_hash`) неизменяемы. Сама `messages` — быстрый индекс для поиска и экспорта: текущий текст и флаги «изменено/удалено» в ней обновляются, но каждое такое изменение сначала записывается в журнал. Любая строка `messages` ссылается на свою исходную запись (`first_seq`, `record_hash`), и `verify.py` это проверяет.

### Ежедневные якоря

После `ANCHOR_HOUR` бот создаёт `data/anchors/anchor_<seq>_<время>.txt` с номером и хешем головы цепочки и:

1. отправляет SHA-256 файла в FreeTSA (RFC 3161), сохраняет `.tsr` (и `.tsq`), проверяет imprint и nonce;
2. ставит метку OpenTimestamps (`.ots`); каждые `OTS_UPGRADE_HOURS` часов бот обновляет её, пока не появится подтверждение в блоке Bitcoin (обычно несколько часов);
3. записывает в цепочку событие `anchor`. Так каждый якорь сам оказывается под следующим.

Якорь доказывает, что все записи до него существовали не позже времени метки, а любое изменение прошлых записей ломает совпадение с якорем.

### Проверка архива

```bash
docker compose run --rm tgbackup python verify.py              # всё, включая сверку OTS с блокчейном
docker compose run --rm tgbackup python verify.py --skip-media --offline
```

Проверяются: непрерывность цепочки и все хеши, наличие триггеров append-only, расшифровка и SHA-256 каждого медиафайла, связь `messages` → цепочка, файлы якорей, подписи FreeTSA (`openssl ts -verify`) и подтверждение OpenTimestamps (merkle root блока через blockstream.info). Итог — понятный отчёт `[OK]/[WARN]/[FAIL]`, код выхода 1 при нарушениях.

### Экспорт

Каждый ZIP (`/export` или автоматический при массовом удалении) содержит:

| Файл | Содержимое |
|---|---|
| `chat.html` | переписка: пузыри, дни, ответы, пересылки, реакции, звонки; у каждого сообщения id, время UTC и местное, отметки изменено/удалено, история правок, хеш записи |
| `chat.pdf` | то же для печати: шапка с данными собеседника (user_id, username, телефон, все известные имена), нумерация «стр. X из Y», SHA-256 записей на каждой странице |
| `media/` | вложения в исходном (расшифрованном) виде |
| `manifest.json` | SHA-256 всех файлов, сообщения с хешами записей, диапазон цепочки, якоря, версия программы, дата экспорта |
| `manifest.sig`, `public_key.pem` | подпись Ed25519 манифеста. Ключ создаётся при первом запуске в `data/keys/` и зашифрован паролем, выведенным из `ENCRYPTION_KEY` |
| `evidence/records.jsonl` | записи журнала по этому чату (raw JSON + хеши) |
| `evidence/chain.csv` | только заголовки цепочки от первой записи чата до головы: можно проверить связность, не раскрывая чужие переписки |
| `evidence/anchors/` | якоря, `.tsr`, `.ots`, сертификаты FreeTSA |
| `README.txt` | как проверить целостность вручную |
| `verify_export.py` | автоматическая проверка (только stdlib; для подписи — `cryptography` или `openssl`) |

```bash
unzip export.zip -d export && python3 export/verify_export.py export
```

### Ограничения, о которых надо знать

- Фиксируется то, что ваш аккаунт получил от Telegram. Сообщение, которое пришло и было удалено, пока бот не работал, не восстановить.
- Время удаления — момент получения события ботом. Если бот был выключен, это время после `catch_up`.
- Сообщения, сохранённые версией 1.x, перенесены в цепочку при обновлении с пометкой `backfilled` и `raw: null`: исходный объект тогда не сохранялся.
- Ключ шифрования лежит в `.env` на том же сервере. Шифрование защищает от утечки диска, снапшотов и бэкапов, но не от root-доступа к живому серверу. Поэтому сервер нужно защитить (раздел 10).

## 9. Как работают удаления

Для личных чатов и обычных групп Telegram присылает событие удаления **без chat_id**, только с id сообщений. Эти id уникальны в пределах аккаунта, поэтому сообщение ищется по `message_id` среди не-каналов. Для каналов и супергрупп chat_id приходит, и поиск идёт по паре `(chat_id, message_id)`.

Факт удаления пишется в цепочку сразу. Уведомления копятся `BULK_DELETE_WINDOW_SEC` секунд: если из чата удалено меньше `BULK_DELETE_THRESHOLD` — приходит уведомление на каждое сообщение (с медиа), иначе — полный экспорт с подписью «Похоже, чат с X был удалён/очищен».

## 10. Обновление

```bash
cd ~/khalid_tgbot_bekap
git pull                      # или скопируйте новые файлы
docker compose build
docker compose up -d
```

Схема обновляется миграциями (`schema_migrations`) при старте. При переходе с 1.x на 2.x автоматически:

1. применяется миграция 2: новые колонки и таблицы, триггеры append-only;
2. если задан `ENCRYPTION_KEY`, plaintext-БД конвертируется в SQLCipher (`sqlcipher_export`), а старые медиа шифруются;
3. все сохранённые ранее сообщения записываются в цепочку с пометкой `backfilled`.

Бэкап данных (БД копируется в зашифрованном виде):

```bash
docker compose stop
tar czf tgbackup-$(date +%F).tgz data/backup.db data/media data/anchors data/keys
docker compose up -d
```

Храните `ENCRYPTION_KEY` отдельно от архива.

## 11. Защита VPS

Сервер хранит сессию с полным доступом к аккаунту и ключ шифрования. Минимум:

**SSH только по ключу, без root:**

```bash
# на своём компьютере
ssh-keygen -t ed25519 && ssh-copy-id user@server

# на сервере: /etc/ssh/sshd_config.d/10-hardening.conf
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
AllowUsers user

sudo sshd -t && sudo systemctl reload ssh
```

Проверьте вход по ключу в новом окне, прежде чем закрывать текущую сессию.

**Файрвол (ufw):** боту входящие порты не нужны.

```bash
sudo apt install ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH          # или свой порт: sudo ufw allow 2222/tcp
sudo ufw enable && sudo ufw status verbose
```

Docker публикует порты в обход ufw. В этом проекте портов нет, но для других контейнеров на сервере публикуйте только `127.0.0.1:порт:порт`.

**fail2ban:**

```bash
sudo apt install fail2ban
sudo tee /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
backend = systemd
maxretry = 4
findtime = 10m
bantime = 1h
EOF
sudo systemctl enable --now fail2ban && sudo fail2ban-client status sshd
```

**Автоматические обновления безопасности:**

```bash
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
systemctl status unattended-upgrades
```

**Прочее:**

- `chmod 700 data && chmod 600 .env` — данные и ключи доступны только владельцу;
- 2FA (облачный пароль) в Telegram обязательно: без неё утёкший код входа даёт доступ к аккаунту;
- периодически смотрите Telegram → Устройства: там должен быть ровно один сеанс сервера;
- обновляйте образ (`docker compose build --pull`) и не держите на этом сервере чужих пользователей с sudo.

## 12. Тесты

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Полный набор (с SQLCipher и PDF) — в Docker:

```bash
docker compose build
docker build -f Dockerfile.test -t tgbackup-test . && docker run --rm tgbackup-test
```
