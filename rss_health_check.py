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

STALE_THRESHOLD_DAYS = CONFIG["rss_health"]["stale_threshold_days"]
HEALTH_FILE = "rss_health.json"
STALE_SOURCES_FILE = "stale_sources.json"

def _latest_entry_age_days(published_values):
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
        "status": "dead",
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

    # Формируем список мёртвых и протухших >14 дней
    stale_urls = []
    for r in rss_results:
        if r["status"] == "dead":
            stale_urls.append(r["url"])
        elif r["status"] == "stale" and r["latest_entry_age_days"] and r["latest_entry_age_days"] > 14:
            stale_urls.append(r["url"])
    with open(STALE_SOURCES_FILE, "w", encoding="utf-8") as f:
        json.dump({"stale_urls": stale_urls, "checked_at": report["checked_at"]}, f, indent=2)

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

    print(f"\n🧹 Создан файл {STALE_SOURCES_FILE} с {len(stale_urls)} источниками для автоматического исключения.")

if __name__ == "__main__":
    main()
