# ЭнергоСбыт — лабораторная работа №1

Python HTTP-сервис и ClickHouse 26.7: схема «звезда», SCD 1/2/3,
корректировки показаний и аннулирование установки счётчиков.
Python использует только стандартную библиотеку; установка pip-пакетов не нужна.

## Запуск

Нужны Docker с Compose 2.20+ (поддержка include), GNU Make и Python 3.10+ на хосте.
Все параметры подключения находятся в `.env.local`; файл включён в Git.
Корневой `docker-compose.yml` подключает `compose.services.yml` через `include`
с `env_file: .env.local`. Поэтому обычные `docker compose logs` и
`docker compose down -v` также загружают настройки без флага `--env-file`.
Пароль в нём учебный, публичный. Авторизации HTTP API по условию нет.

```sh
make run
make setup
curl http://localhost:8080/ping
curl http://localhost:8123/ping
```

`make run` собирает приложение и ждёт готовности ClickHouse.
`make setup` создаёт таблицы, генерирует CSV и загружает их пакетами по 100 000 строк.
Порты хоста читаются из `.env.local`; внутри Docker используются имя сервиса
`clickhouse` и его HTTP-порт 8123, как требует схема сети Compose.
`volume/` хранит данные ClickHouse, `data/` — CSV; обе папки исключены из Git.

| Команда | Назначение |
| --- | --- |
| `make run` | Запуск ClickHouse и приложения |
| `make stop` | Остановка контейнеров с сохранением данных |
| `make gen` | Пересоздание детерминированных CSV |
| `make load` | Очистка семи таблиц и загрузка CSV |
| `make setup` | Создание схемы, генерация и загрузка |
| `make teardown` | Удаление семи таблиц вместе с данными |
| `make test` | Проверки валидации; интеграционные тесты включаются отдельно |

**Повторный `make load` заменяет данные всех семи таблиц**, включая изменения,
внесённые через API. Это сознательная стратегия загрузки учебного датасета:
повторная загрузка не создаёт дублей даже в MergeTree и VersionedCollapsingMergeTree.
Перед очисткой проверяется наличие всех CSV. Во время загрузки API использовать не следует.
После ошибки загрузки повторите `make load`; транзакции между таблицами нет.
`make teardown` оставляет пустую базу и CSV для повторного запуска `make setup`.

В Windows без GNU Make эквивалентные команды в PowerShell:

```powershell
docker compose --env-file .env.local up -d --build --wait
py -3 -m scripts.manage schema
py -3 -m scripts.generate
py -3 -m scripts.manage load
# После работы:
py -3 -m scripts.manage teardown
docker compose --env-file .env.local down
```

## Данные и ключи

| Таблица | CSV-строк | Строк с FINAL после загрузки |
| --- | ---: | ---: |
| dim_realty | 1 000 | 1 000 |
| dim_gauges | 1 100 | 1 100 |
| dim_tariffs | 2 | 2 |
| dim_users | 1 000 | 1 000 |
| fact_gauge_bindings | 1 200 | 1 000 |
| fact_meter_readings | 1 128 880 | 1 096 000 |
| fact_invoice | 36 000 | 36 000 |

Период: 2023-11-01 — 2026-10-31, включая 29 февраля 2024 года: 1096 дней.
Ровно 3% показаний имеют дополнительную корректирующую строку.
Показания накопительные; счета считаются по месячному потреблению, а не по
накопленному значению. У первых 100 объектов 2025-11-01 заменяется счётчик,
накопление на новом счётчике начинается с нуля. Замена происходит на границе месяца.
Тариф меняется 2025-11-01: 550/275 → 620/310 копеек за дневной/ночной кВт·ч.
Срок оплаты — 10-е число следующего месяца. Все деньги целочисленные.

- `dim_realty`, `dim_gauges`: MergeTree, `ORDER BY id` для поиска объекта.
- `dim_users`: ReplacingMergeTree(updated_at), `ORDER BY id`. Пользователь один;
  ФИО и телефоны обновляются синхронной мутацией всех строк этого id, поэтому
  старое ФИО не остаётся даже без FINAL, а история телефона ограничена `phone_prev`.
- `dim_tariffs`: ReplacingMergeTree(updated_at), `ORDER BY (id, valid_from)`.
  Разные интервалы SCD 2 не схлопываются между собой.
- `fact_gauge_bindings`: VersionedCollapsingMergeTree(sign, version),
  `ORDER BY (realty_id, gauge_id)`. Отмена копирует ключ, время и версию установки;
  следующая установка получает увеличенную версию. Без партиций, чтобы отмена
  могла схлопнуться с исходной установкой независимо от времени замены.
- `fact_meter_readings`: ReplacingMergeTree(created_at),
  `ORDER BY (realty_id, reading_date)`, месячные партиции по reading_date.
  Смена user/gauge/tariff при корректировке не создаёт вторую запись за дату.
- `fact_invoice`: MergeTree, `ORDER BY (realty_id, created_at)`, месячные партиции
  по created_at. Удобны выборки счетов объекта и обслуживание месячных данных.

Небольшие справочники не партиционируются. Чтение изменяемых данных использует
`FINAL` и не зависит от фоновых слияний. `OPTIMIZE` для работы API не требуется.
Время — UTC. Сервис работает одним процессом; записи сериализуются блокировкой.
Для двух корректировок в одну секунду версия равна максимуму текущего UTC-времени
и предыдущей версии + 1 секунда. Это устраняет коллизии DateTime.
Горизонтальное масштабирование записывающего приложения здесь не предусмотрено.

## HTTP API

Коллекция Postman: `api/energosbyt.postman_collection.json`.
Переменная `base_url` по умолчанию равна `http://localhost:8080`.
Коллекция содержит все методы, примеры ответов и проверки статусов.

| Метод | Путь | Результат |
| --- | --- | --- |
| GET | `/ping` | 200, пустое тело |
| GET | `/users/{id}` | Собственник, текущий и предыдущий телефоны |
| PATCH | `/users/{id}` | Изменение fio и/или phone, 204 |
| GET | `/realty/{id}` | Объект недвижимости |
| POST | `/realty/{id}/readings` | Показание или корректировка, 201 |
| GET | `/realty/{id}/readings?date_from=2026-10-01&date_to=2026-10-31` | Актуальные показания, границы включительны |
| GET | `/realty/{id}/invoices?limit=10&offset=0` | Счета по убыванию времени, count до пагинации |
| GET | `/tariffs?date=2026-10-27` | Версия тарифа: valid_from ≤ date < valid_to |
| POST | `/realty/{id}/gauges` | Аннулирование установки и новая установка, 201 |

Ошибки возвращаются как `{"message":"..."}`, отсутствующие сущности — 404.
Пустой список показаний — 200 и `{"readings":[],"count":0}`.
`limit=0` возвращает пустую страницу, сохраняя общее число счетов.
Телефон без истории имеет `phone_prev=""`; повтор того же телефона историю не сдвигает.

## Проверка сценариев SCD через HTTP и SQL

Откройте SQL-консоль (пароль и база берутся из окружения контейнера):

```sh
docker compose --env-file .env.local exec clickhouse sh -c 'exec clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --database "$CLICKHOUSE_DATABASE"'
```

Следующие примеры выполняются после свежего `make setup`.

### SCD 1: смена ФИО

```sh
curl -X PATCH http://localhost:8080/users/1 -H 'Content-Type: application/json' -d '{"fio":"New owner name"}'
```

```sql
SELECT fio FROM dim_users WHERE id = 1;
SELECT count() FROM dim_users WHERE id = 1 AND fio = 'Owner 1';
```

Ожидается новое ФИО и 0 строк со старым. Используется `ALTER TABLE ... UPDATE`
с `mutations_sync=1`; история ФИО не создаётся.

### SCD 3: две смены телефона

```sh
curl -X PATCH http://localhost:8080/users/1 -H 'Content-Type: application/json' -d '{"phone":"+79990000001"}'
curl -X PATCH http://localhost:8080/users/1 -H 'Content-Type: application/json' -d '{"phone":"+79990000002"}'
```

```sql
SELECT phone, phone_prev FROM dim_users FINAL WHERE id = 1;
```

Ожидаются `+79990000002` и `+79990000001`.

### SCD 2: изменение тарифа с 1 ноября

```sql
SELECT id, price_day_kwph, price_night_kwph, valid_from, valid_to
FROM dim_tariffs FINAL ORDER BY id, valid_from;
```

Два интервала: до 2025-11-01 и начиная с этой даты. Аналогично сравните
`GET /tariffs?date=2025-10-31` и `GET /tariffs?date=2025-11-01`.
Для добавления следующего интервала сначала вставляется обновлённая копия открытой
версии с новым `valid_to` и большим `updated_at`, затем новая версия с новым
`valid_from` и `valid_to='2099-01-01 00:00:00'`. Это сохраняет предыдущие цены.

### Пересчёт старого счёта по историческому тарифу

```sql
SELECT i.created_at, i.price,
       i.day_kw * t.price_day_kwph + i.night_kw * t.price_night_kwph AS recalculated
FROM fact_invoice AS i
INNER JOIN (SELECT * FROM dim_tariffs FINAL) AS t ON i.tariff_id = t.id
WHERE i.realty_id = 1
  AND i.created_at >= t.valid_from AND i.created_at < t.valid_to
ORDER BY i.created_at;
```

`price = recalculated` для каждого месяца, включая месяцы до изменения тарифа.
Запрос ничего не изменяет: старые счета остаются в исходной сумме.

### Корректировка показания

```sh
curl -X POST http://localhost:8080/realty/1/readings -H 'Content-Type: application/json' -d '{"user_id":1,"gauge_id":1001,"tariff_id":1,"reading_date":"2026-10-01","day_kw":1250,"night_kw":300}'
curl -X POST http://localhost:8080/realty/1/readings -H 'Content-Type: application/json' -d '{"user_id":1,"gauge_id":1001,"tariff_id":1,"reading_date":"2026-10-01","day_kw":1251,"night_kw":301}'
```

```sql
SELECT * FROM fact_meter_readings FINAL
WHERE realty_id = 1 AND reading_date = '2026-10-01';
```

Одна строка, 1251/301. Старые строки не удаляются приложением; до слияния их можно
увидеть без FINAL. После фонового слияния физически остаётся победившая версия.

### Замена счётчика

```sh
curl -X POST http://localhost:8080/realty/1/gauges -H 'Content-Type: application/json' -d '{"gauge_id":1100}'
```

```sql
SELECT * FROM fact_gauge_bindings WHERE realty_id = 1 ORDER BY version, sign;
SELECT * FROM fact_gauge_bindings FINAL WHERE realty_id = 1;
```

После FINAL активна одна установка: gauge_id=1100, sign=1, version=3.
До слияния видна компенсирующая строка с sign=-1 и версией прежней установки.

### DELETE: ошибочная строка с лишним нулём

```sql
INSERT INTO fact_invoice VALUES
(1, 1, 1, 1001, 100, 50, 775000, '2026-12-10', '2026-11-01 00:00:00');
ALTER TABLE fact_invoice DELETE
WHERE realty_id = 1 AND created_at = '2026-11-01 00:00:00' AND price = 775000
SETTINGS mutations_sync = 1;
SELECT count() FROM fact_invoice
WHERE realty_id = 1 AND created_at = '2026-11-01 00:00:00' AND price = 775000;
```

Результат 0; существующие счета датасета не затрагиваются.

## Автоматические проверки

```sh
make test
# Интеграционные проверки меняют тестовые объекты 900 и 950:
make setup
INTEGRATION=1 make test
# Восстановить исходный датасет перед автогрейдером:
make load
```

PowerShell: `$env:INTEGRATION='1'; py -3 -m unittest discover -s tests -v`.
Проверяются объёмы после FINAL, суммы всех счетов, SCD 1/3, граница SCD 2,
корректировка за дату, замена счётчика, ошибки и пагинация.

`.labrc` содержит `LAB=1`. Исходные `.github/` и `CODEOWNERS` сохранены согласно
CONTRIBUTING.md. Для сдачи создайте PR из рабочей ветки в main/master своего
репозитория и дождитесь успешной проверки и ревью преподавателя.
