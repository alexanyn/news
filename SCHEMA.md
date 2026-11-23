# Схемы данных

## metrics.jsonl
Каждая строка – JSON-объект одного прогона.

Поля:
- `run_started_at` (string) – ISO-время запуска (МСК).
- `schedule` (string) – расписание ("09:00", "12:00", ...).
- `status` (string) – итог запуска: `sent`, `empty_no_candidates`, `empty_no_digest_items`, `send_failed` или `crashed`. Исторически встречается также `gemini_failed` (legacy; текущий код этот статус не создаёт).
- `errors` (list) – массив строк ошибок.
- `duration_sec` (float) – общая длительность прогона.
- `fetch_duration_sec` (float) – время сбора новостей (включая все параллельные запросы).
- `gemini_total_duration_sec` (float) – общее время запроса к Gemini (включая ретраи).
- `postprocess_duration_sec` (float) – время постобработки (построение HTML, классификация).
- `send_duration_sec` (float) – время отправки в Telegram.
- `collection` (object) – статистика сбора:
  - `rss_feeds_total` (int)
  - `rss` (object) – `raw_seen`, `skip_sent`, `skip_window`, `skip_no_title`, `skip_similar_title`, `included`.
  - `telegram_channels_total` (int)
  - `telegram` (object) – аналогично.
  - `telegram_included` (int)
- `gemini` (object):
  - `latency_sec` (float) – время одного успешного вызова (без ретраев).
  - `attempts_used` (int)
  - `fallback_used` (bool)
  - `error_type` (string, опционально) – `safety_block`, `rate_limit`, `server_error`, `invalid_request`, `max_retries_exceeded`.
  - `error_detail` (string, опционально)
- `digest` (object, если анализ и постобработка дошли до формирования дайджеста):
  - `total_items` (int)
  - `by_category` (object) – ключи категорий, значения `{"world": int, "russia": int}`.
- `digest.validation` (object, начиная с версии проверки provenance) – `input_items`, `accepted_items`, `rejected_bad_id`, `rejected_unknown_id`, `rejected_empty_summary`, `rejected_duplicate_id`, `rejected_duplicate_url`, `rejected_bad_url`, `rejected_provenance` (все int).
- `telegram` (object, условно) – `success` (bool), `chunks_sent` (int).
- `alert_sent` (bool) – отправлен ли алерт.
- `sources_used` (list) – имена источников, давших материал (уникальные).
- `quality` (object, начиная с версии проверки provenance, условно) – `validation_version`, `input_items`, `accepted_items`, `published_items`, `validation` и `quality_pass`. Для исторических запусков поле отсутствует; это нормально.

Запись с ошибкой может не содержать `digest`, `telegram`, `sources_used`, `quality` и отдельных timing-полей. Статус `gemini_failed` встречается в исторических данных, но текущий код его не формирует.

## sent_urls.json
Список объектов:
- `url` (string)
- `added_at` (float) – Unix timestamp
- `source` (string) – всегда "parsed_feed"

## recent_titles.json
Список объектов:
- `title` (string)
- `added_at` (float) – Unix timestamp

## last_run.json
Объект: ключ – имя расписания, значение – Unix timestamp логической границы.

## rss_health.json
Объект:
- `checked_at` (string) – ISO-время проверки
- `stale_threshold_days` (int)
- `rss` и `telegram` – объекты со статистикой и массивом `details`:
  - `source_name`, `url`/`username`, `status` (`ok`, `stale`, `dead`), `entries_count`, `latest_entry_age_days`, `error`.

## stale_sources.json (генерируется rss_health_check.py)
- `stale_urls` (list) – URL источников, признанных мёртвыми или протухшими > 14 дней.
- `checked_at` (string) – время последней проверки.
