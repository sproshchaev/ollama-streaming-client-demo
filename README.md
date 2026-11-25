# ollama-streaming-client-demo

[![Check](https://github.com/sproshchaev/ollama-streaming-client-demo/actions/workflows/check.yml/badge.svg)](https://github.com/sproshchaev/ollama-streaming-client-demo/actions/workflows/check.yml)
[![License MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Сергей Прощаев**

[![Website](https://img.shields.io/badge/prosoft.pages.dev-blue?logo=googlechrome&logoColor=white)](https://prosoft.pages.dev)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-sproshchaev-0A66C2)](https://www.linkedin.com/in/sproshchaev/)
[![Habr](https://img.shields.io/badge/Habr-sproshchaev-65A3BE?logo=habr&logoColor=white)](https://habr.com/ru/users/sproshchaev/)

---

## 1. Что это

Локальный HTTP-стенд сравнивает два клиента нативного `/api/generate` Ollama. Первый собирает текст до EOF. Второй проверяет завершение генерации и сохраняет полноценный ответ один раз на операцию. Стенд сопровождает [статью на Хабре](https://habr.com/ru/companies/otus/articles/1101922/).

Разделы — [Состав](#2-что-внутри) · [Требования](#3-требования) · [Быстрый старт](#4-быстрый-старт) · [Использование](#5-использование) · [Ограничения](#6-ограничения) · [Лицензия](#7-лицензия) · [Ссылки](#8-полезные-ссылки)

## 2. Что внутри

- `client.py` читает поток и сохраняет ответ по ключу операции.
- `experiment.py` запускает HTTP-стенд и сравнивает клиентов.
- `fixtures/` содержит два тела ответов настоящей Ollama и их контрольные суммы.
- `expected.txt` описывает ожидаемые результаты всех проверок.

## 3. Требования

Python 3.9 или новее, Bash и `shasum`. Достаточно стандартной библиотеки Python. Тестовый сервер открывает временный порт на `127.0.0.1`.

## 4. Быстрый старт

```bash
./run.sh
```

Скрипт проверяет контрольные суммы записей и сравнивает результат с `expected.txt`.

## 5. Использование

| Сценарий | Чтение до EOF | Проверка завершения |
| --- | --- | --- |
| Запись `stop` | Принят | Принят |
| EOF до `done` | Принят | Отклонен |
| `error` после HTTP 200 | Принят | Отклонен |
| Запись `length` | Принят | Отклонен |
| Обрыв после `done`, без завершающего HTTP-чанка | Принят | Отклонен |
| Тело короче объявленного `Content-Length` | Принят | Отклонен |
| UTF-8 разрезан, следующий чанк содержит конец строки и новую строку | Принят | Принят |
| Повтор той же операции | 2 записи | 1 запись |

`stop.ndjson` и `length.ndjson` записаны из тела HTTP 200 с типом `application/x-ndjson`. Сервер Ollama 0.32.15, модель `llama3.1:8b` Q4_K_M, digest `46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`. Оба запроса к `/api/generate` отправлены с `stream: true`, `num_ctx: 512`, `num_gpu: 0` и `temperature: 0`. Для `stop` запрос `Reply with one word: orange.` с `num_predict: 64`. Для `length` запрос `Count from one to ten with a word for each number.` с `num_predict: 1`. В первом ответе четыре строки NDJSON, во втором две. Стенд воспроизводит отказы поверх этих записей и формирует два собственных потока для проверки UTF-8 и повтора.

## 6. Ограничения

- Стенд проверяет конкретные варианты нативного `/api/generate`. Другие API используют свои форматы и признаки завершения.
- Для этого приложения результат с `done_reason: length` неполный. Полнота потока и качество текста проверяются отдельно.
- Транспортные проверки опираются на Python HTTP-библиотеку и остаток `Content-Length`. Лимит строки и тайм-аут чтения в примере дополняются лимитом ответа и дедлайном операции в прикладном коде.
- Повтор запроса может снова запустить вычисление. Уникальный ключ защищает запись в SQLite в этом примере. Внешние побочные эффекты требуют своей защиты.

## 7. Лицензия

[MIT](LICENSE).

## 8. Полезные ссылки

- [Статья на Хабре](https://habr.com/ru/companies/otus/articles/1101922/)
- [Ollama API — потоковые ответы](https://docs.ollama.com/api/streaming)
- [Ollama API — ошибки](https://docs.ollama.com/api/errors)
- [Спецификация NDJSON](https://github.com/ndjson/ndjson-spec)
