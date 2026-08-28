"""
Еженедельная проверка здоровья RSS-лент и Telegram-каналов проекта.

ДОБАВЛЕНО 2026-08-27. Сознательно ОТДЕЛЁН от main.py и от основного
пайплайна дайджеста (см. rss_health.yml — отдельный workflow, отдельное
расписание): проверка ~100+ источников занимает заметное время даже
параллельно, и это ненужный риск/задержка в критичном пути отправки
дайджеста 5 раз в день. Источники не отваливаются за часы — раз в неделю
для этой проверки более чем достаточно, и она может себе позволить
собственный, более щедрый бюджет по времени, не деля его с Gemini/Telegram.

Импортирует список источников и функции сбора НАПРЯМУЮ из main.py (RSS_FEEDS,
TELEGRAM_CHANNELS, fetch_feed, fetch_telegram_channel, get_source_name,
parse_published_time) — список источников должен быть ОДИН на весь проект,
дублирование здесь было бы источником рассинхронизации. Импорт не требует
реальных GEMINI_API_KEY/TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (см.
_require_runtime_env_vars в main.py — проверка вызывается только при запуске
main.py как скрипта, не при импорте).

Результат — rss_health.json (перезаписывается целиком при каждом запуске,
это снимок ТЕКУЩЕГО состояния, не история; тренды по времени видно в
дашборде через git-историю самого файла при желании).
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from main import (
    RSS_FEEDS,
    TELEGRAM_CHANNELS,
    MAX_FETCH_WORKERS,
    CONFIG,
    fetch_feed,
    fetch_telegram_channel,
    get_source_name,
    parse_published_time,
    now_moscow,
)

# Лента технически отвечает, но последняя запись старше этого — считаем
# "протухшей" (stale), отдельно от полностью "мёртвой" (dead, не отвечает
# вообще или отвечает пустотой). Разделение важно: protuhshaya лента иногда
# значит "источник ещё жив, но temporarily не публикует", а не "лента умерла
# навсегда и её надо удалять из списка".
# ДОБАВЛЕНО 2026-08-27: читается из того же config.json, что и main.py (ключ
# "rss_health.stale_threshold_days") — единая точка настройки на весь проект,
# не два независимых числа, которые могут разойтись друг с другом со временем.
STALE_THRESHOLD_DAYS = CONFIG["rss_health"]["stale_threshold_days"]
HEALTH_FILE = "rss_health.json"


def _latest_entry_age_days(published_values):
    """Из списка сырых строк даты публикации возвращает возраст самой свежей
    записи в днях, либо None, если ни одну дату не удалось распарсить."""
    latest_ts = None
    for raw in published_values:
        ts = parse_published_time(raw)
        if ts and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
    if latest_ts is None:
        return None
    return (time.time() - latest_ts) / 86400


def check_one_feed(url):
    source_name = get_source_name(url)
    result = {
        "url": url,
        "source_name": source_name,
        "status": "dead",  # dead | stale | ok
        "entries_count": 0,
        "latest_entry_age_days": None,
        "error": None,
    }
    try:
        parsed = fetch_feed(url, timeout=20)
        if not parsed or not getattr(parsed, "entries", None):
            result["error"] = "Пустой ответ или нет записей"
            return result

        result["entries_count"] = len(parsed.entries)
        age_days = _latest_entry_age_days(e.get("published", "") for e in parsed.entries[:5])

        if age_days is None:
            # Есть записи, но даты не распарсились — считаем "ok" на доверии
            # (main.py в collect_all_news делает то же допущение при сборе).
            result["status"] = "ok"
            return result

        result["latest_entry_age_days"] = round(age_days, 1)
        result["status"] = "stale" if age_days > STALE_THRESHOLD_DAYS else "ok"

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def check_one_telegram(channel):
    username = channel["username"]
    source_name = channel["source_name"]
    result = {
        "username": username,
        "source_name": source_name,
        "status": "dead",
        "entries_count": 0,
        "latest_entry_age_days": None,
        "error": None,
    }
    try:
        messages = fetch_telegram_channel(username, timeout=20)
        if not messages:
            result["error"] = "Пустой ответ или нет сообщений"
            return result

        result["entries_count"] = len(messages)
        age_days = _latest_entry_age_days(m.get("published", "") for m in messages[-5:])

        if age_days is None:
            result["status"] = "ok"
            return result

        result["latest_entry_age_days"] = round(age_days, 1)
        result["status"] = "stale" if age_days > STALE_THRESHOLD_DAYS else "ok"

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def _sort_key(r):
    # Мёртвые сначала, потом протухшие, потом здоровые — чтобы проблемы
    # сразу были видны в начале rss_health.json, не нужно листать до конца.
    order = {"dead": 0, "stale": 1, "ok": 2}
    return (order.get(r["status"], 3), r["source_name"])


def main():
    print(f"🩺 Проверка здоровья {len(RSS_FEEDS)} RSS-источников и {len(TELEGRAM_CHANNELS)} Telegram-каналов...")

    rss_results = []
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        futures = {executor.submit(check_one_feed, url): url for url in RSS_FEEDS}
        for future in as_completed(futures):
            rss_results.append(future.result())

    tg_results = []
    if TELEGRAM_CHANNELS:
        with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(TELEGRAM_CHANNELS))) as executor:
            futures = {executor.submit(check_one_telegram, ch): ch for ch in TELEGRAM_CHANNELS}
            for future in as_completed(futures):
                tg_results.append(future.result())

    dead_rss = [r for r in rss_results if r["status"] == "dead"]
    stale_rss = [r for r in rss_results if r["status"] == "stale"]
    dead_tg = [r for r in tg_results if r["status"] == "dead"]
    stale_tg = [r for r in tg_results if r["status"] == "stale"]

    report = {
        "checked_at": now_moscow().isoformat(),
        "stale_threshold_days": STALE_THRESHOLD_DAYS,
        "rss": {
            "total": len(rss_results),
            "ok": len(rss_results) - len(dead_rss) - len(stale_rss),
            "stale": len(stale_rss),
            "dead": len(dead_rss),
            "details": sorted(rss_results, key=_sort_key),
        },
        "telegram": {
            "total": len(tg_results),
            "ok": len(tg_results) - len(dead_tg) - len(stale_tg),
            "stale": len(stale_tg),
            "dead": len(dead_tg),
            "details": sorted(tg_results, key=_sort_key),
        },
    }

    with open(HEALTH_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"✅ RSS: {report['rss']['ok']}/{report['rss']['total']} ok, "
          f"{report['rss']['stale']} протухли, {report['rss']['dead']} мертвы")
    print(f"✅ Telegram: {report['telegram']['ok']}/{report['telegram']['total']} ok, "
          f"{report['telegram']['stale']} протухли, {report['telegram']['dead']} мертвы")

    if dead_rss:
        print("\n💀 Мёртвые RSS-источники:")
        for r in dead_rss:
            print(f"   - {r['source_name']} ({r['url']}) — {r['error']}")
    if stale_rss:
        print(f"\n⏳ Протухшие RSS-источники (нет свежего контента > {STALE_THRESHOLD_DAYS} дн.):")
        for r in stale_rss:
            print(f"   - {r['source_name']} — последняя запись {r['latest_entry_age_days']} дн. назад")
    if dead_tg:
        print("\n💀 Мёртвые Telegram-каналы:")
        for r in dead_tg:
            print(f"   - {r['source_name']} (@{r['username']}) — {r['error']}")
    if stale_tg:
        print(f"\n⏳ Протухшие Telegram-каналы (нет свежего контента > {STALE_THRESHOLD_DAYS} дн.):")
        for r in stale_tg:
            print(f"   - {r['source_name']} — последняя запись {r['latest_entry_age_days']} дн. назад")


if __name__ == "__main__":
    main()
