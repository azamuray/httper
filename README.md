# HTTP-сервис заказов

FastAPI, Python 3.11+, in-memory хранилище и метрики Prometheus.

## Локальный запуск

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Документация API: http://localhost:8000/docs.

```sh
curl -i -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"order_id":"order-1","customer_id":"customer-1","amount":50,"currency":"USD"}'
curl -i http://localhost:8000/orders/order-1
curl http://localhost:8000/metrics
```

## Контракт

- `POST /orders`: создание заказа, `201`. Ждёт случайную задержку 0,05–0,5 с и сохранение.
- `GET /orders/{order_id}`: заказ (`200`) или `404`; обрабатываемый заказ ещё не доступен.
- `GET /metrics`: текстовый формат Prometheus.
- Повторный идентификатор, в том числе во время обработки: `409`.
- Отрицательная сумма: `400`; неверные/отсутствующие поля: `422`.
- Идентификаторы — непустые строки, валюта — три заглавные латинские буквы.
- Ноль допустим. NaN и бесконечность запрещены. Неизвестные поля запрещены.
- Деньги хранятся как `Decimal`; в JSON-ответе `amount` — строка для сохранения точности.
  Во входном JSON принимается число или десятичная строка.

## Метрики

| Имя | Тип | Семантика |
| --- | --- | --- |
| `orders_created_total` | Counter | Успешно сохранённые заказы |
| `orders_amount_sum` | Histogram | Суммы созданных заказов; бакеты 10, 50, 100, 500, 1000, +Inf |
| `request_duration_seconds` | Histogram | Время HTTP-запросов; лейблы `method`, `endpoint` |
| `orders_in_progress` | Gauge | Заказы в обработке; уменьшается также при ошибках и отмене |
| `orders_validation_errors_total` | Counter | Запросы с отрицательной суммой |

`endpoint` содержит шаблон `/orders/{order_id}`, а для неизвестных маршрутов — `unmatched`.
Измеряются также ошибки и `/metrics`; собственное измерение текущего scrape появится
в следующем scrape. Измерение завершается после отправки ответа приложением.

Имя Histogram сохранено из ТЗ: серии `orders_amount_sum_bucket`,
`orders_amount_sum_count`, `orders_amount_sum_sum`. Суммы разных валют объединяются
без конвертации; значения Prometheus представлены числами с плавающей точкой.

Пример конфигурации Prometheus (сервис должен быть доступен по указанному адресу):

```yaml
scrape_configs:
  - job_name: orders
    static_configs:
      - targets: ['localhost:8000']
```

## Проверки

```sh
ruff check .
ruff format --check .
mypy --strict
pytest -q
```

Тесты используют отдельные приложения/реестры метрик и управляемую обработку вместо
случайного ожидания. Проверяют API, метрики, конкурентные дубликаты, ошибки и отмену.

## Docker

```sh
docker build -t httper:local .
docker run --rm --name httper -p 8000:8000 httper:local
```

Контейнер запускается от непривилегированного пользователя с **одним worker**.
Хранилище и метрики локальны для процесса и сбрасываются при перезапуске.
Несколько workers/реплик создадут независимые хранилища, поэтому не поддерживаются.

Зависимости ограничены диапазонами в `pyproject.toml`; lock-файла пока нет.
История изменений — в [CHANGELOG.md](CHANGELOG.md).
