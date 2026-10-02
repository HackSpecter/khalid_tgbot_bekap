ЭКСПОРТ ПЕРЕПИСКИ TELEGRAM С ДОКАЗАТЕЛЬСТВАМИ ЦЕЛОСТНОСТИ
=========================================================

Чат:        {{ title }} (id {{ chat_id }}, тип {{ chat_type }}{% if username %}, @{{ username }}{% endif %})
{% if interlocutor %}
Собеседник: user_id {{ interlocutor.user_id }}, {{ interlocutor.name or '—' }}{% if interlocutor.username %}, @{{ interlocutor.username }}{% endif %}{% if interlocutor.phone %}, {{ interlocutor.phone }}{% endif %}

{% endif %}
Создан:     {{ generated_utc }} ({{ generated }}, {{ timezone }})
Часть:      {{ part }} из {{ total }}
Программа:  tgbackup {{ version }}

SHA-256 записей (records_digest): {{ records_digest }}
Голова хеш-цепочки:               seq {{ head_seq }}, {{ head_hash }}
Ключ подписи (SHA-256 Ed25519):   {{ fingerprint }}


СОДЕРЖИМОЕ АРХИВА
-----------------
chat.html                 переписка для просмотра в браузере
chat.pdf                  та же переписка для печати (на каждой странице — records_digest)
media/                    вложения в исходном виде (как получены из Telegram)
manifest.json             перечень всех файлов с SHA-256, сообщений и их хешей, якорей
manifest.sig              подпись Ed25519 над точными байтами manifest.json
public_key.pem            публичный ключ для проверки подписи
evidence/records.jsonl    исходные записи журнала по этому чату: raw JSON объекта
                          Telegram (MTProto) в момент получения + хеш медиафайла
evidence/chain.csv        заголовки хеш-цепочки (seq, prev_hash, record_hash, entry_hash)
                          от первой записи этого чата до головы цепочки
evidence/anchors/         якоря цепочки и внешние метки времени:
                          *.txt — якорь (seq и entry_hash головы на момент якоря)
                          *.tsr — ответ RFC 3161 от FreeTSA (квалифицированная метка времени)
                          *.ots — доказательство OpenTimestamps (Bitcoin)
                          freetsa_cacert.pem, freetsa_tsa.crt — сертификаты FreeTSA
verify_export.py          автоматическая проверка всего перечисленного


КАК УСТРОЕНА ДОКАЗАТЕЛЬНАЯ БАЗА
-------------------------------
1. Каждое событие (новое сообщение, правка, удаление, реакция, изменение профиля)
   записывается в журнал только добавлением. Запись содержит raw JSON объекта
   Telegram и SHA-256 медиафайла, вычисленный при скачивании.
   record_hash = SHA-256(payload)
2. Записи связаны в цепочку:
   entry_hash = SHA-256("<seq>:<prev_hash>:<record_hash>")
   Изменить или удалить любую запись невозможно без изменения всех последующих хешей.
3. Ежедневно голова цепочки заякоривается внешними метками времени:
   RFC 3161 (FreeTSA) и OpenTimestamps (блокчейн Bitcoin). Это доказывает, что
   все записи до якоря существовали не позже указанного времени.
4. Манифест экспорта подписан ключом Ed25519 программы.


КАК ПРОВЕРИТЬ
-------------
Автоматически (Python 3.8+; для подписи — пакет cryptography или openssl 3):

    python3 verify_export.py

Вручную:

1) Хеши файлов:
    sha256sum chat.html chat.pdf media/*
   и сравнить со значениями в manifest.json → "files".

2) Подпись манифеста (OpenSSL 3.x):
    openssl pkeyutl -verify -pubin -inkey public_key.pem -rawin \
        -in manifest.json -sigfile manifest.sig

3) Метка времени RFC 3161 для каждого якоря:
    openssl ts -verify -data evidence/anchors/<якорь>.txt \
        -in evidence/anchors/<якорь>.txt.tsr \
        -CAfile evidence/anchors/freetsa_cacert.pem \
        -untrusted evidence/anchors/freetsa_tsa.crt
   Ожидается "Verification: OK". Время — в выводе:
    openssl ts -reply -in evidence/anchors/<якорь>.txt.tsr -text

4) OpenTimestamps (pip install opentimestamps-client):
    ots upgrade evidence/anchors/<якорь>.txt.ots
    ots verify  evidence/anchors/<якорь>.txt.ots
   или загрузить .txt и .ots на https://opentimestamps.org

5) Цепочка: для каждой строки chain.csv entry_hash = SHA-256("seq:prev_hash:record_hash"),
   prev_hash строки равен entry_hash предыдущей; запись якоря (seq, entry_hash)
   совпадает со строкой chain.csv; каждая запись records.jsonl присутствует в chain.csv
   с теми же хешами, а SHA-256 её поля payload равен record_hash.


ОГРАНИЧЕНИЯ
-----------
- Записи фиксируют то, что аккаунт владельца получил от серверов Telegram.
  Сообщения, удалённые до того, как программа их получила, не сохраняются.
- Время удаления — момент получения события об удалении программой.
- Раздел chain.csv содержит только заголовки (хеши) чужих записей; их содержимое
  в экспорт не включается.
{% if anchors %}

ЯКОРЯ В ЭТОМ ЭКСПОРТЕ
---------------------
{% for a in anchors %}
seq {{ a.seq }}  {{ a.entry_hash }}
    RFC 3161:       {{ a.tsa_time or 'нет' }}
    OpenTimestamps: {{ a.ots_status or 'нет' }}{% if a.ots_block %}, блок Bitcoin {{ a.ots_block }}{% endif %}

{% endfor %}
{% else %}

Якорей нет: записи этого экспорта ещё не попали под ежедневное заякоривание.
Повторите экспорт после ближайшего якоря (или выполните /anchor).
{% endif %}
