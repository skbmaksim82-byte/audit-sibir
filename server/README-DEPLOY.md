# Запуск сервера «Аудит магазина» (пилот 11472)

Сервер — один процесс FastAPI на порту 8010. Он же отдаёт приложение `index.html`, поэтому nginx нужен только для проксирования. Данные лежат в SQLite, фото — файлами на диске. Схема та же, что у Дашборда паспортов.

Пример ниже ставит приложение по адресу `https://<ваш-домен>/audit/`. Нужны доступ к серверу по SSH и права sudo. Время — минут 20. Нужен Python 3.10 или новее (Ubuntu 22.04 и 24.04 подходят).

## 1. Код и зависимости

```bash
sudo mkdir -p /opt/audit-sibir && sudo chown $USER /opt/audit-sibir
git clone https://github.com/skbmaksim82-byte/audit-sibir /opt/audit-sibir
cd /opt/audit-sibir/server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo mkdir -p /var/lib/audit-sibir && sudo chown www-data:www-data /var/lib/audit-sibir
```

## 2. Служба systemd

Файл `/etc/systemd/system/audit-sibir.service`:

```ini
[Unit]
Description=Аудит магазина (пилот)
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/audit-sibir/server
Environment=AUDIT_DB=/var/lib/audit-sibir/audit.db
Environment=AUDIT_PHOTOS=/var/lib/audit-sibir/photos
Environment=AUDIT_PILOT_STORES=11472
Environment=AUDIT_PHOTO_DAYS=120
ExecStart=/opt/audit-sibir/server/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8010 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

`AUDIT_PILOT_STORES` — магазины, открытые в приложении, через запятую. Если строку убрать, откроются все 78 магазинов. Нужен ровно один процесс (`--workers 1`).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now audit-sibir
curl -s http://127.0.0.1:8010/api/health   # {"ok":true,...,"stores":["11472"]}
```

## 3. nginx

В блок `server { ... }` вашего домена с HTTPS (там же, где Дашборд паспортов) добавьте:

```nginx
location = /audit { return 301 /audit/; }
location /audit/ {
    proxy_pass http://127.0.0.1:8010/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    client_max_body_size 10m;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Откройте `https://<ваш-домен>/audit/`. Должен появиться экран входа с надписью «Пилот: магазин 11472».

## 4. Пользователи и пароли

```bash
cd /opt/audit-sibir/server
sudo -u www-data AUDIT_DB=/var/lib/audit-sibir/audit.db AUDIT_PHOTOS=/var/lib/audit-sibir/photos .venv/bin/python manage.py pilot 11472
```

Команда создаст четыре входа и напечатает пароли в консоли: `dp1` (ДП Новосибирск 2), `dm11472` (ДМ магазина), `sales` (ДПР), `region` (ДР). Передайте пароли лично, в чат их не пишите.

Другие команды (с теми же переменными окружения):

| Команда | Что делает |
| --- | --- |
| `manage.py passwd dm11472` | новый пароль; старые входы на телефонах сбрасываются |
| `manage.py off dp1` / `on dp1` | отключить / включить вход |
| `manage.py add dm11813` | добавить человека, когда пилот расширится |
| `manage.py list` | кто заведён |

## 5. Обновление

```bash
cd /opt/audit-sibir && git pull && sudo systemctl restart audit-sibir
```

Данные и фото лежат в `/var/lib/audit-sibir` и при обновлении не трогаются.

## Резервная копия

Каждую ночь копируйте папку `/var/lib/audit-sibir`. Для базы на ходу безопаснее так:

```bash
sqlite3 /var/lib/audit-sibir/audit.db ".backup /backup/audit-$(date +%F).db"
```

## Как это работает

- Телефон хранит копию данных и работает без связи. Изменения уходят на сервер, когда связь появляется. Пока что-то не отправлено, в шапке видна плашка «нет связи · ждёт отправки N».
- Обновления от других участников приходят раз в 20 секунд и при каждом возврате в приложение.
- Права проверяются на сервере. ДМ видит только свой магазин, ДП — своё подразделение. Визиты ведёт ДП, исправления — ДМ. Оценку ставит ДПР, потом ДР; ДР не может оценить раньше ДПР.
- Фото удаляются через 120 дней (`AUDIT_PHOTO_DAYS`). Подписи в актах не удаляются.
- На GitHub Pages приложение по-прежнему работает в демо-режиме на тестовых данных.
