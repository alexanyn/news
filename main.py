print("=== ЗАПУСК СКРИПТА ВЕРСИИ 6.12 (FIXED_TIMEZONE_AND_PUBLISH_WAIT) ===")

import os
import re
import json
import time
import datetime
import requests
import feedparser
from html import escape as html_escape
from bs4 import BeautifulSoup
import telebot
from telebot.apihelper import ApiTelegramException
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1. Переменные окружения
gemini_api_key = os.environ.get("GEMINI_API_KEY")
bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")

if not gemini_api_key or not bot_token or not chat_id:
    raise ValueError("Ошибка: Проверьте GEMINI_API_KEY, TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Secrets!")

bot = telebot.TeleBot(bot_token)
CHAT_ID = chat_id

HISTORY_FILE = "sent_urls.json"
LAST_RUN_FILE = "last_run.json"

# ВАЖНО: GitHub Actions раннеры работают в UTC, независимо от `timezone:` в
# cron-триггере (тот `timezone:` влияет только на МОМЕНТ СРАБАТЫВАНИЯ cron,
# а не на системное время самой машины). Раньше SCHEDULES ниже интерпретировались
# через time.localtime()/time.mktime(), которые на раннере читают именно UTC —
# то есть "08:00" по факту означало 08:00 UTC = 11:00 МСК, а не 08:00 МСК, как
# задумывалось. Из-за этого расписание "плыло" на 3 часа относительно ожиданий.
#
# Исправление: все вычисления границ расписания ниже явно делаются в московском
# времени (UTC+3, без перехода на летнее/зимнее время — Россия его не использует).
MOSCOW_OFFSET = datetime.timedelta(hours=3)
MOSCOW_TZ = datetime.timezone(MOSCOW_OFFSET)

def now_moscow():
    """Текущее время как offset-aware datetime в московском часовом поясе."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(MOSCOW_TZ)

# Расписание: логическое время публикации дайджестов (по Москве)
# Скрипт может запускаться раньше этих времён (например, в 3:00 вместо 8:00),
# но собирать новости будет ЛОГИЧЕСКИ за нужный промежуток времени, а
# ПУБЛИКОВАТЬ будет строго не раньше момента наступления этого времени
# (см. wait_until_publish_time).
SCHEDULES = {
    "morning": {"hour": 8, "minute": 0},      # 08:00 МСК логически
    "afternoon": {"hour": 13, "minute": 0},   # 13:00 МСК логически
    "evening": {"hour": 19, "minute": 0},     # 19:00 МСК логически
}

def get_schedule_boundary(schedule_name):
    """
    Возвращает логическое время ГРАНИЧНОЕ дайджеста (время, ДО КОТОРОГО собираются
    новости), как Unix timestamp. Считается в московском времени (см. выше).
    
    Если сейчас 03:15 МСК и запущен "morning" дайджест:
    - Логическая граница = 08:00 МСК СЕГОДНЯ (если ещё не наступила) или ВЧЕРА (если уже прошла)
    - Вчера в 08:00 был последний morning → собираем с вчера 08:00 до сегодня 08:00
    
    Если сейчас 09:15 МСК и запущен "morning" дайджест:
    - Логическая граница = 08:00 МСК СЕГОДНЯ (уже прошла, используем как текущую)
    """
    if schedule_name not in SCHEDULES:
        raise ValueError(f"Unknown schedule: {schedule_name}. Valid: {list(SCHEDULES.keys())}")
    
    sched = SCHEDULES[schedule_name]
    hour, minute = sched["hour"], sched["minute"]
    
    now_dt = now_moscow()
    
    today_boundary_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    # Если сейчас ДО этого времени, то граница — вчера, не сегодня
    if now_dt < today_boundary_dt:
        today_boundary_dt -= datetime.timedelta(days=1)
    
    return today_boundary_dt.timestamp()

def get_next_publish_time(schedule_name):
    """
    Возвращает Unix timestamp СЛЕДУЮЩЕГО наступления логического времени публикации
    (по Москве) для данного расписания — то есть момент, до которого скрипт должен
    ДОЖДАТЬСЯ перед фактической отправкой в Telegram, чтобы afternoon/evening не
    публиковались сразу после сбора (что и вызывало эффект "дайджест выходит через
    минуты после запуска workflow", а не в заявленное время).
    
    В отличие от get_schedule_boundary (которая может вернуть уже ПРОШЕДШУЮ границу
    для расчёта окна сбора), эта функция всегда возвращает БУДУЩИЙ момент.
    """
    if schedule_name not in SCHEDULES:
        raise ValueError(f"Unknown schedule: {schedule_name}. Valid: {list(SCHEDULES.keys())}")
    
    sched = SCHEDULES[schedule_name]
    hour, minute = sched["hour"], sched["minute"]
    
    now_dt = now_moscow()
    target_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    # Если целевое время уже прошло сегодня — значит, публикация должна быть
    # СЕГОДНЯ (мы просто запустились рано и должны подождать), либо, если время
    # уже давно прошло (запуск сильно задержался), публикуем немедленно.
    # Логика: цель всегда "ближайшее наступление hour:minute, которое не более
    # чем на PUBLISH_GRACE_SECONDS в прошлом" — иначе (сильное опоздание) публикуем сразу.
    if now_dt > target_dt:
        seconds_late = (now_dt - target_dt).total_seconds()
        if seconds_late > PUBLISH_GRACE_SECONDS:
            # Опоздали слишком сильно (например, ручной запуск днём) — не ждать
            # до следующего дня, публикуем немедленно.
            return now_dt.timestamp()
    
    return target_dt.timestamp()

# Если запуск задержался больше чем на это время после целевого часа публикации,
# считаем ожидание бессмысленным и публикуем сразу, а не ждём почти сутки.
PUBLISH_GRACE_SECONDS = 30 * 60  # 30 минут

def wait_until_publish_time(schedule_name):
    """
    Блокирует выполнение до наступления логического времени публикации (по Москве),
    либо возвращается немедленно, если это время уже наступило (в пределах
    PUBLISH_GRACE_SECONDS) или было пропущено намного раньше.
    
    Именно это устраняет проблему "дайджест публикуется сразу после запуска
    workflow вместо заявленного времени": сбор новостей может начаться заранее
    (в 03:00/08:00/14:00), но реальная отправка в Telegram откладывается до
    08:00/13:00/19:00 включительно.
    """
    target_ts = get_next_publish_time(schedule_name)
    now_ts = time.time()
    wait_seconds = target_ts - now_ts
    
    if wait_seconds <= 0:
        print(f"⏱️  Целевое время публикации уже наступило, публикуем немедленно.")
        return
    
    target_readable = datetime.datetime.fromtimestamp(target_ts, MOSCOW_TZ).strftime("%H:%M:%S МСК")
    print(f"⏳ Ждём до {target_readable} перед публикацией ({int(wait_seconds)} сек)...")
    time.sleep(wait_seconds)

def fmt_msk(ts):
    """Форматирует Unix timestamp как читаемую строку в московском времени (для логов)."""
    return datetime.datetime.fromtimestamp(ts, MOSCOW_TZ).strftime("%a %b %d %H:%M:%S %Y МСК")

def get_last_run_time(schedule_name):
    """Возвращает ЛОГИЧЕСКУЮ границу последнего запуска расписания (не реальное время)"""
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and schedule_name in data:
                    return data[schedule_name]
        except Exception as e:
            print(f"Ошибка чтения файла last_run: {e}")
    return None

def save_run_time(schedule_name):
    """Сохраняет ЛОГИЧЕСКУЮ границу текущего запуска (для отсчёта следующего окна)"""
    data = {}
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Ошибка чтения last_run при сохранении: {e}")
    
    # Сохраняем логическую границу расписания, а не реальное время запуска
    boundary = get_schedule_boundary(schedule_name)
    data[schedule_name] = boundary
    
    try:
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"💾 Сохранено время последнего запуска {schedule_name}: {fmt_msk(boundary)}")
    except Exception as e:
        print(f"Ошибка записи last_run: {e}")

def load_sent_urls():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data and isinstance(data[0], str):
                    return {url: time.time() for url in data}
                return {item["url"]: item["added_at"] for item in data}
        except Exception as e:
            print(f"Ошибка чтения файла истории: {e}")
    return {}

def save_sent_urls(sent_dict):
    current_time = time.time()
    cleaned_dict = {
        url: ts for url, ts in sent_dict.items() 
        if (current_time - ts) <= (7 * 24 * 3600)
    }
    
    structured_list = [
        {"url": url, "added_at": ts, "source": "parsed_feed"} 
        for url, ts in cleaned_dict.items()
    ]
    
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(structured_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения истории: {e}")

# 2. Таблица каноничных названий
FEED_CANONICAL_NAMES = {
    "reuters.com": "Reuters",
    "apnews.com": "Associated Press",
    "bbci.co.uk": "BBC World News",
    "aljazeera.com": "Al Jazeera",
    "ft.com": "Financial Times",
    "bloomberg.com": "Bloomberg",
    "dj.com": "Wall Street Journal",
    "interfax.ru": "Интерфакс",
    "kommersant.ru": "Коммерсантъ",
    "themoscowtimes.com": "The Moscow Times",
    "meduza.io": "Meduza",
    "foreignaffairs.com": "Foreign Affairs",
    "foreignpolicy.com": "Foreign Policy",
    "worldpoliticsreview.com": "World Politics Review",
    "eng.globalaffairs.ru": "Russia in Global Affairs",
    "globalaffairs.ru": "Россия в глобальной политике",
    "expert.ru": "Эксперт",
    "csis.org": "CSIS",
    "chathamhouse.org": "Chatham House",
    "cfr.org": "Council on Foreign Relations",
    "rand.org": "RAND Corporation",
    "iiss.org": "IISS",
    "carnegieendowment.org": "Carnegie Endowment",
    "atlanticcouncil.org": "Atlantic Council",
    "brookings.edu": "Brookings Institution",
    "crisisgroup.org": "International Crisis Group",
    "russiancouncil.ru": "РСМД",
    "valdaiclub.com": "Валдайский клуб",
    "imemo.ru": "ИМЭМО РАН",
    "veb.ru": "Институт ВЭБ",
    "csr.ru": "ЦСР",
    "forecast.ru": "ЦМАКП",
    "imf.org": "IMF",
    "bis.org": "BIS",
    "worldbank.org": "World Bank",
    "oecd.org": "OECD",
    "wto.org": "ВТО",
    "fred.stlouisfed.org": "FRED",
    "project-syndicate.org": "Project Syndicate",
    "cepr.org": "VoxEU",
    "capital+economics": "Capital Economics",  # исправлено 2026-08-16: было "capitaleconomics" (без +), не совпадало с URL ниже и никогда не резолвилось
    "mckinsey.com": "McKinsey",
    "bcg.com": "BCG",
    "bain.com": "Bain",
    "deloitte.com": "Deloitte",
    "imaa-institute.org": "IMAA",
    "due+diligence": "FT Due Diligence",
    "sec.gov": "SEC",
    "technologyreview.com": "MIT Tech Review",
    "theinformation.com": "The Information",
    "stratechery.com": "Stratechery",
    "techcrunch.com": "TechCrunch",
    "restofworld.org": "Rest of World",
    "iea.org": "IEA",
    "eia.gov": "EIA",
    "opec.org": "ОПЕК",
    "nato.int": "NATO",
    "whitehouse.gov": "White House",
    "state.gov": "U.S. State Department",
    "treasury.gov": "U.S. Treasury",
    "federalreserve.gov": "Federal Reserve",
    "congress.gov": "Congress",
    "ec.europa.eu": "European Commission",
    "ecb.europa.eu": "European Central Bank",
    "un.org": "UN News",
    "kremlin.ru": "Kremlin",
    "government.ru": "Правительство РФ",
    "duma.gov.ru": "Госдума",
    "mid.ru": "МИД РФ",
    "cbr.ru": "ЦБ РФ",
    "minfin.gov.ru": "Минфин РФ",
    "rosstat.gov.ru": "Росстат",
    "rbc.ru": "РБК",
    "money+stuff": "Bloomberg Money Stuff",
    "axios.com": "Axios",
    "firstft": "FT FirstFT",
    "politico.com": "Politico",
    "gzero": "Eurasia Group / GZERO",
    "econs.online": "Econs",
    "thebell.io": "The Bell",
    "ourworldindata.org": "Our World in Data",

    # --- Добавлено 2026-08-16: источников не было в словаре вообще ---
    "economist.com": "The Economist",
    "dealbreaker.com": "Dealbreaker",
    "minchenko": "Minchenko Consulting",

    # --- Добавлено 2026-08-16 (источники от пользователя) ---
    "finmarket.ru": "Финмаркет",
    "cnews.ru": "CNews",
    "tadviser.ru": "TAdviser",
    "energyland.info": "EnergyLand.info",
}

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://news.google.com/rss/search?q=site:bloomberg.com&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    "https://www.interfax.ru/rss.asp",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://www.themoscowtimes.com/rss/news",
    "https://meduza.io/rss/all",
    "https://www.foreignaffairs.com/rss.xml",
    "https://foreignpolicy.com/feed/",
    "https://www.worldpoliticsreview.com/feed/",
    "https://carnegieendowment.org/rss/publications/",
    "https://www.atlanticcouncil.org/feed/",
    "https://www.brookings.edu/feed/",
    "https://www.crisisgroup.org/rss.xml",
    "https://russiancouncil.ru/rss/",
    "https://news.google.com/rss/search?q=site:imemo.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:veb.ru+институт&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:forecast.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://www.wto.org/english/news_e/news_e.rss",
    "https://news.google.com/rss/search?q=site:fred.stlouisfed.org&hl=en-US&gl=US&ceid=US:en",
    "https://www.project-syndicate.org/rss",
    "https://news.google.com/rss/search?q=%22Capital+Economics%22&hl=en-US&gl=US&ceid=US:en",
    "https://www.mckinsey.com/insights/rss",
    "https://news.google.com/rss/search?q=site:bcg.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:bain.com+insights+-jobs+-careers+-hiring&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:deloitte.com+%22M%26A+trends%22+OR+report+OR+study+-jobs+-careers+-hiring&hl=en-US&gl=US&ceid=US:en",
    "https://imaa-institute.org/feed/",
    "https://news.google.com/rss/search?q=%22FT+Due+Diligence%22&hl=en-US&gl=US&ceid=US:en",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=10-K&dateb=&owner=include&count=40&output=atom",
    "https://www.technologyreview.com/feed/",
    "https://stratechery.com/feed/",
    "https://techcrunch.com/feed/",
    "https://restofworld.org/feed/",
    "https://www.eia.gov/rss/todayinenergy.xml",

    # ============================================================
    # Добавлено 2026-08-16.
    #
    # Важно: часть источников ниже УЖЕ ЧИСЛИЛАСЬ в FEED_CANONICAL_NAMES
    # (CSIS, Chatham House, CFR, RAND, IISS, Валдай, ОПЕК, Белый дом,
    # Госдеп, Минфин США, Еврокомиссия, ЕЦБ, ООН, Кремль, Правительство
    # РФ, Госдума, МИД РФ, ЦБ РФ, Минфин РФ, Росстат, РБК), но ни для
    # одного из них не было строки в RSS_FEEDS — то есть имя было
    # "заготовлено", а лента никогда не парсилась. comparison_sources.md
    # засчитывал их как "покрыто ✅" только по наличию имени в словаре,
    # что и создавало иллюзию более высокого покрытия, чем есть на деле.
    #
    # Для 10 источников ниже (см. первый блок) найдены и вручную
    # проверены прямые официальные RSS/Atom-ленты. Для остальных
    # официального публичного RSS нет (современный JS-сайт без отдаваемого
    # XML, либо сайт блокирует автоматические запросы без браузера) —
    # для них используется тот же обходной путь через Google News,
    # что уже применяется выше для Reuters/AP/Bloomberg/FRED/Capital
    # Economics и т.д. Он не заменяет полноценный RSS 1:1, но даёт
    # регулярный поток заголовков с этих сайтов.
    #
    # ВНИМАНИЕ (эксплуатационный риск): ниже добавлено ~25 новых
    # запросов к news.google.com поверх уже имеющихся ~11. Google может
    # начать резать/капчить массовые автоматические запросы с одного IP
    # (особенно с общих раннеров GitHub Actions) — если после обновления
    # начнут проваливаться СТАРЫЕ ленты через Google News, а не только
    # новые, это первая вероятная причина. Решения: развести запуски по
    # времени, поставить между fetch_feed вызовами time.sleep(1-2 сек),
    # или в перспективе перейти на самостоятельно поднятый RSS-Bridge/
    # RSSHub для части источников.
    # ============================================================

    # --- Прямые официальные ленты (проверены вручную 2026-08-16) ---
    "https://www.federalreserve.gov/feeds/press_all.xml",                # Federal Reserve — все пресс-релизы
    "https://news.un.org/feed/subscribe/en/news/all/rss.xml",            # UN News — общая лента
    "https://www.ecb.europa.eu/rss/press.html",                          # ECB — пресс-релизы/речи/интервью (реальный XML; .html в пути — так устроены все RSS-адреса ЕЦБ, это не опечатка)
    "https://ec.europa.eu/commission/presscorner/api/rss",               # European Commission — Daily news, press releases, statements
    "http://en.kremlin.ru/events/president/news/feed",                   # Kremlin — официальная лента (англоязычная версия сайта президента)
    "http://www.cbr.ru/rss/RssPress",                                    # ЦБ РФ — официальные пресс-релизы
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",                 # РБК — общая новостная лента
    "https://www.chathamhouse.org/path/whatsnew.xml",                    # Chatham House — все новые материалы сайта
    "https://www.whitehouse.gov/news/feed/",                             # White House — Releases
    "https://rss.politico.com/playbook.xml",                             # Politico Playbook

    # --- Международные институты и правительства без публичного RSS (Google News fallback) ---
    "https://news.google.com/rss/search?q=site:imf.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:worldbank.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:oecd.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:bis.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:iea.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:opec.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:nato.int&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:treasury.gov&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:state.gov&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:congress.gov&hl=en-US&gl=US&ceid=US:en",

    # --- Think tanks без ленты, хотя имя уже было в словаре ---
    "https://news.google.com/rss/search?q=site:csis.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:cfr.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:rand.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:iiss.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:valdaiclub.com&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:csr.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:cepr.org+voxeu&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:ourworldindata.org&hl=en-US&gl=US&ceid=US:en",

    # --- Российские первоисточники без ленты ---
    "https://news.google.com/rss/search?q=site:government.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:duma.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:mid.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:minfin.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:rosstat.gov.ru&hl=ru&gl=RU&ceid=RU:ru",

    # --- "Критические пропуски" из missing_sources_summary.txt (аналитика/рассылки) ---
    "https://news.google.com/rss/search?q=site:economist.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:theinformation.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22Money+Stuff%22+Bloomberg&hl=en-US&gl=US&ceid=US:en",       # Мэтт Левайн
    "https://news.google.com/rss/search?q=site:axios.com+Markets&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:gzeromedia.com&hl=en-US&gl=US&ceid=US:en",                # Eurasia Group / GZERO
    "https://news.google.com/rss/search?q=site:econs.online&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:dealbreaker.com&hl=en-US&gl=US&ceid=US:en",               # см. примечание в сопроводительном сообщении — сайт мог снизить активность, не проверял вручную
    "https://news.google.com/rss/search?q=%22Minchenko+Consulting%22&hl=ru&gl=RU&ceid=RU:ru",

    # ============================================================
    # Добавлено 2026-08-16 (по запросу пользователя, источники принесены
    # им лично для категорий ЭКОНОМИКА, ТЕХНОЛОГИИ, ЭНЕРГЕТИКА):
    # ============================================================

    # --- Экономика: Финмаркет (основной объём) + Frank Media (банки/финтех/ЦБ) ---
    "http://www.finmarket.ru/rss/mainnews.asp",                     # Финмаркет — главные новости (проверено: реальные, датированные материалы)
    # Frank Media: прямого публичного RSS у сайта нет (не нашёл ни на сайте,
    # ни через поиск), зато есть официальный Telegram-канал издания — см.
    # TELEGRAM_CHANNELS ниже.

    # --- Технологии: CNews (объём) + TAdviser (глубина, госконтракты, импортозамещение) ---
    "https://www.cnews.ru/inc/rss/news.xml",                        # CNews — все новости (официальная лента с cnews.ru/rss)
    # TAdviser: страница со списком лент (tadviser.ru/index.php/RSS) отдаёт 404
    # при прямом обращении, точный XML-адрес подтвердить не удалось — временно
    # через Google News. Если у вас есть время проверить вручную в браузере
    # (зайти на https://www.tadviser.ru/index.php/RSS и скопировать ссылки
    # "Новости ИТ-рынка России" и правой кнопкой "копировать адрес ссылки"),
    # эти два fallback-запроса стоит заменить на настоящий RSS.
    "https://news.google.com/rss/search?q=site:tadviser.ru&hl=ru&gl=RU&ceid=RU:ru",                      # TAdviser — новости
    "https://news.google.com/rss/search?q=site:tadviser.ru+аналитика&hl=ru&gl=RU&ceid=RU:ru",            # TAdviser — аналитика

    # --- Энергетика: Neftegaz.ru (нефть и газ) + EnergyLand (электроэнергетика, ВИЭ, атом) ---
    # Neftegaz.ru: прямого публичного RSS не нашёл, зато есть официальный
    # Telegram-канал издания — см. TELEGRAM_CHANNELS ниже.
    "https://news.google.com/rss/search?q=site:energyland.info&hl=ru&gl=RU&ceid=RU:ru",                  # EnergyLand.info
]

# Telegram-каналы обрабатываются отдельно от RSS_FEEDS: у них нет RSS-ленты,
# контент собирается парсингом публичной веб-версии t.me/s/<channel>.
# Financial Times заменён на этот канал вместо RSS с ft.com, так как сайт FT
# требует подписку, а канал публикует статьи бесплатно (с переводом на русский).
TELEGRAM_CHANNELS = [
    {"username": "the_financial_times_journal", "source_name": "Financial Times"},
    # Добавлено 2026-08-16: у The Bell нет публичного RSS (thebell.io — платная
    # подписка), но есть официальный публичный Telegram-канал самого издания
    # (проверено вручную 2026-08-16, канал активен). Издание маркировано в РФ
    # как «иностранный агент» — как и Meduza, которая уже есть в этом скрипте.
    {"username": "thebell_io", "source_name": "The Bell"},
    # Добавлено 2026-08-16 (по запросу пользователя): у обоих источников ниже
    # нет прямого публичного RSS, зато есть официальные, активные Telegram-каналы
    # самих изданий (проверено вручную 2026-08-16).
    {"username": "frank_media", "source_name": "Frank Media"},          # банки, финтех, регулирование ЦБ
    {"username": "neftegazchannel", "source_name": "Neftegaz.RU"},      # нефть и газ
]

LOCAL_POLITICS_KEYWORDS = [
    "муниципал", "депутат", "областной", "районный",
    "администрац", "мэр", "губернатор", "чиновник"
]
# ИСПРАВЛЕНО 2026-08-16: убрано 8 слов из исходного списка — все они
# многозначны и в норме означают что-то ДРУГОЕ в деловых/технологических
# новостях, а не локальную политику. Оставлять их значило гарантированно
# резать часть контента именно в категориях ЭКОНОМИКА/БИЗНЕС/ТЕХНОЛОГИИ:
#   "край"      — чаще "на переднем крае технологий", а не Пермский край
#   "выбор"     — чаще "выбор стратегии/поставщика", а не выборы
#   "снять"     — "снять ограничения", "снять квартиру", "снять фильм"
#   "главный"   — "главный экономист", "главный офис", "главный конкурент"
#   "голосов"   — "голосовой помощник", "голосовые технологии" (ловило даже
#                 новости именно про ТЕХНОЛОГИИ, где полно voice AI в 2026)
#   "уволен", "назначен", "отставка" — это не локальная политика, а
#     ЛЕГИТИМНЫЕ деловые новости (смена CEO/CFO — важное корпоративное
#     событие для категории БИЗНЕС, а не то, что нужно вырезать)
# Оставлены только термины с низким риском ложных срабатываний в бизнес-
# контексте: они почти всегда означают именно локальную/региональную
# администрацию, а не что-то ещё.

# Страховочный фильтр на случай, если модель всё же пропустит локальную
# криминальную хронику или бытовой курьёз без международного значения —
# дополняет исключения, заданные в промпте для Gemini (см. generate_analytical_json).
LOCAL_CRIME_AND_TRIVIA_KEYWORDS = [
    "убил жену", "убил мужа", "убил дочь", "убил сына", "убил родствен",
    "застрелил", "зарезал", "ДТП", "сбил насмерть", "поджог дома",
    "бытовое убийство", "семейная ссора закончилась",
]

def fetch_feed(url, timeout=15):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8"
    }

    # SEC.gov требует специфичный формат User-Agent вида "Имя email@domain",
    # иначе блокирует запрос (это их официальное требование к автоматическим
    # обращениям, не обход защиты). Замени email на свой реальный адрес —
    # SEC может заблокировать IP при массовых запросах без валидного контакта.
    if "sec.gov" in url:
        headers["User-Agent"] = "NewsDigestBot your_email@example.com"

    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers=headers
        )
        response.raise_for_status()
        return feedparser.parse(response.content)
    except requests.exceptions.Timeout:
        print(f"Timeout при получении {url}")
        return None
    except requests.exceptions.SSLError as e:
        print(f"Ошибка SSL {url}: {e}")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"Ошибка парсинга RSS {url}: {e}")
        return None
    except Exception as e:
        print(f"Пустая/битая лента {url}: {e}")
        return None

def parse_published_time(published_str):
    """
    Парсит дату публикации из RSS/Telegram в Unix timestamp.
    Поддерживает форматы:
    - RFC 2822 (RSS): "Mon, 13 Aug 2026 06:12:00 +0000"
    - ISO 8601 (Telegram): "2026-08-13T06:12:00+00:00" или "2026-08-13T06:12:00Z"
    
    Возвращает Unix timestamp или None, если парсинг не удался.
    """
    if not published_str or not isinstance(published_str, str):
        return None
    
    # Сначала пытаемся парсить как RFC 2822 (RSS)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(published_str)
        return dt.timestamp()
    except Exception:
        pass
    
    # Если RFC 2822 не сработал, пытаемся ISO 8601
    try:
        from datetime import datetime
        # Нормализуем 'Z' на конце в '+00:00'
        iso_str = published_str.replace('Z', '+00:00')
        # Python 3.7+ поддерживает fromisoformat с timezone
        dt = datetime.fromisoformat(iso_str)
        return dt.timestamp()
    except Exception:
        pass
    
    # Оба формата не сработали — вернём None, фолбэк в collect_all_news
    return None

def get_source_name(url):
    for domain, name in FEED_CANONICAL_NAMES.items():
        if domain.lower() in url.lower():
            return name
    return url.split("//")[1].split("/")[0] if "//" in url else url

def fetch_telegram_channel(username, timeout=15):
    """
    Парсит публичную веб-версию Telegram-канала (t.me/s/<username>) и возвращает
    список сообщений в формате [{"title": str, "link": str, "published": str}, ...].
    Не использует официальный Telegram API — работает с общедоступной HTML-страницей
    предпросмотра, доступной без авторизации. Разметка (tgme_widget_message_*)
    стабильна годами и используется в открытых проектах (RSSHub и др.), но при
    изменении вёрстки Telegram парсинг может потребовать обновления селекторов.
    """
    url = f"https://t.me/s/{username}"
    try:
        response = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"Timeout при получении Telegram-канала {username}")
        return []
    except requests.exceptions.HTTPError as e:
        print(f"Ошибка получения Telegram-канала {username}: {e}")
        return []
    except Exception as e:
        print(f"Не удалось получить Telegram-канал {username}: {e}")
        return []

    try:
        soup = BeautifulSoup(response.content, "html.parser")
    except Exception as e:
        print(f"Не удалось распарсить HTML Telegram-канала {username}: {e}")
        return []

    messages = []
    for msg_div in soup.select("div.tgme_widget_message"):
        post_id = msg_div.get("data-post", "")
        if not post_id:
            continue

        text_div = msg_div.select_one(".tgme_widget_message_text")
        if not text_div:
            # Сообщение без текста (только фото/видео без подписи) — пропускаем,
            # т.к. новостному дайджесту нужен текст для анализа.
            continue

        # Пост канала обычно оформлен как <b>заголовок</b> + <i>резюме</i>,
        # за которыми следуют служебные ссылки ("Читать статью полностью",
        # "Присоединиться к каналу") и хэштеги — их не берём, чтобы не
        # засорять текст, который пойдёт в модель для анализа.
        bold_tag = text_div.find("b")
        italic_tag = text_div.find("i")
        headline = bold_tag.get_text(strip=True) if bold_tag else ""
        summary_part = italic_tag.get_text(strip=True) if italic_tag else ""

        if headline:
            title = f"{headline}. {summary_part}".strip() if summary_part else headline
        else:
            # Формат поста без <b>/<i> — берём текст целиком как есть
            title = text_div.get_text(separator=" ", strip=True)

        if not title:
            continue

        time_tag = msg_div.select_one(".tgme_widget_message_date time")
        published = time_tag.get("datetime", "") if time_tag else ""

        link = f"https://t.me/{post_id}"
        messages.append({"title": title, "link": link, "published": published})

    return messages

def collect_all_news(sent_urls_history, schedule_name="morning"):
    """
    Собирает новости для дайджеста в нужном ЛОГИЧЕСКОМ временном окне.
    
    schedule_name: 'morning' (08:00), 'afternoon' (13:00), 'evening' (19:00)
    
    Логика:
    - Текущая логическая граница = get_schedule_boundary(schedule_name)
    - Последняя логическая граница = get_last_run_time(schedule_name)
    - Собираем новости, опубликованные МЕЖДУ этими двумя границами
    
    Это позволяет собирать новости за нужный промежуток (08:00–13:00, 13:00–19:00 и т.д.),
    даже если физически скрипт запускается раньше логического времени публикации.
    """
    news_db = {}
    raw_data_list = []
    
    # Вычисляем окно сбора
    current_boundary = get_schedule_boundary(schedule_name)
    last_boundary = get_last_run_time(schedule_name)
    
    if last_boundary is None:
        # Первый запуск этого расписания — берём новости с предыдущей логической границы
        last_boundary = current_boundary - 24 * 3600
        print(f"⚠️  Первый запуск {schedule_name} дайджеста, установлено окно в 24 часа назад")
    elif last_boundary >= current_boundary:
        # Этот дайджест уже собирался сегодня (last_boundary "догнал" current_boundary).
        # Обычно это значит, что скрипт для этого schedule_name запущен повторно
        # в тот же логический день (например, вручную через workflow_dispatch после
        # штатного запуска, или тестовый прогон). Окно не может быть нулевым/отрицательным —
        # раздвигаем его назад на 24 часа от текущей границы, чтобы дайджест не оказался
        # пустым, но явно предупреждаем, что это, вероятно, повторный запуск.
        print(f"⚠️  {schedule_name} дайджест уже собирался для этого цикла (последняя граница "
              f"{fmt_msk(last_boundary)} >= текущей {fmt_msk(current_boundary)}). "
              f"Похоже на повторный/ручной запуск. Пересобираем окно за последние 24 часа "
              f"вместо пустого/некорректного диапазона.")
        last_boundary = current_boundary - 24 * 3600
    
    print(f"📅 {schedule_name.upper()} дайджест: собираем новости с {fmt_msk(last_boundary)} до {fmt_msk(current_boundary)}")
    print(f"📡 Начинаем парсинг {len(RSS_FEEDS)} лент...")
    
    for idx, feed_url in enumerate(RSS_FEEDS, 1):
        parsed = fetch_feed(feed_url)
        if not parsed or not parsed.entries:
            continue
        
        source_name = get_source_name(feed_url)
        
        for entry in parsed.entries[:5]:
            url = entry.get("link", "")
            if not url or url in sent_urls_history:
                continue
            
            # ===== КЛЮЧЕВОЙ ФИЛЬТР: по дате публикации и логическому окну =====
            published_str = entry.get("published", "")
            published_ts = parse_published_time(published_str)
            
            # Включаем новость только если её дата публикации попадает в нужное окно:
            # last_boundary <= published < current_boundary
            if published_ts is not None:
                if published_ts < last_boundary or published_ts >= current_boundary:
                    # Новость вне окна — пропускаем
                    continue
            # Если дата не парсится (published_ts is None), берём новость на доверие
            # (предполагаем, что RSS отдаёт свежий контент в нужном порядке)
            
            title = entry.get("title", "").strip()
            if not title:
                continue
            
            news_id = str(len(news_db))
            news_db[news_id] = {
                "url": url,
                "title": title,
                "source_name": source_name,
                "published": published_str
            }
            
            raw_data_list.append(f"ID:{news_id}|Title:{title}|Source:{source_name}")
            # ИСПРАВЛЕНО: URL больше НЕ добавляется в историю здесь.
            # Раньше новость считалась "отправленной" уже на этапе сбора из RSS,
            # то есть до того, как она реально прошла через Gemini и ушла в Telegram.
            # Если Gemini или Telegram падали, новость терялась на 7 дней, хотя
            # фактически никуда не отправлялась. Теперь запись в историю происходит
            # только после подтверждённой отправки в Telegram (см. build_html_digest
            # и блок __main__).
    
    print(f"✅ Собрано {len(news_db)} новостей из {len(RSS_FEEDS)} RSS-источников")

    if TELEGRAM_CHANNELS:
        print(f"📡 Начинаем парсинг {len(TELEGRAM_CHANNELS)} Telegram-каналов...")
        tg_collected = 0
        for channel in TELEGRAM_CHANNELS:
            username = channel["username"]
            source_name = channel["source_name"]
            messages = fetch_telegram_channel(username)

            # Как и для RSS, берём только несколько последних сообщений за проход,
            # чтобы не заваливать Gemini старым контентом при первом запуске.
            for msg in messages[-5:]:
                url = msg["link"]
                if not url or url in sent_urls_history:
                    continue

                # Аналогичный фильтр по дате для Telegram-сообщений
                published_str = msg["published"]
                published_ts = parse_published_time(published_str)
                
                if published_ts is not None:
                    if published_ts < last_boundary or published_ts >= current_boundary:
                        continue

                title = msg["title"]
                if not title:
                    continue

                news_id = str(len(news_db))
                news_db[news_id] = {
                    "url": url,
                    "title": title,
                    "source_name": source_name,
                    "published": published_str
                }

                raw_data_list.append(f"ID:{news_id}|Title:{title}|Source:{source_name}")
                tg_collected += 1

        print(f"✅ Собрано {tg_collected} новостей из Telegram-каналов")

    raw_data_prompt = "\n".join(raw_data_list)
    return news_db, raw_data_prompt

def generate_analytical_json(raw_data_prompt):
    prompt_template = """Проанализируй следующие новости и дай структурированный JSON анализ.
    
    Верни ТОЛЬКО валидный JSON без пояснений и Markdown, в следующем формате:
    {
        "geopolitics": [{"id": "1", "summary_ru": "Краткое резюме", "is_russia": false}, ...],
        "economics": [...],
        "business": [...],
        "technology": [...],
        "energy": [...],
        "security": [...]
    }
    
    Категории:
    - geopolitics: международные события, дипломатия
    - economics: макроэкономика, финансы, данные
    - business: корпоративные события, M&A
    - technology: IT, инновации
    - energy: энергетика, полезные ископаемые
    - security: конфликты, оборона
    
    ВАЖНО: для КАЖДОЙ новости обязательно проставь поле "is_russia": true или false
    (true — если новость о России или напрямую касается России, false — во всех
    остальных случаях). Не пропускай это поле ни для одной новости.
    
    Уточнение к "is_russia": считай true для новостей О РОССИИ НА ЛЮБОМ УРОВНЕ —
    федеральном (указ президента, закон, ЦБ РФ), региональном (решение
    правительства области, региональный бюджет) И корпоративном (российская
    компания — Сбербанк, Яндекс, Газпром и т.п. — сделала что-то значимое).
    Не занижай true только потому, что новость не о федеральной власти.
    
    ВАЖНО (бизнес vs. локальная политика): если новость по существу о компании,
    инвестициях, технологиях или экономике, но при этом упоминает взаимодействие
    с администрацией/губернатором/министерством (льготы, разрешение, госконтракт,
    субсидия) — классифицируй её по существу (business/technology/economics/energy),
    а не отбрасывай как "локальную политику". Пример: "Губернатор подписал соглашение
    о налоговых льготах для IT-парка" — это technology или business, а не мусор.
    Также не путай смену руководителя КОМПАНИИ (новый CEO, отставка CFO) с
    "локальной политикой" — это всегда валидная новость для business.
    
    ИСКЛЮЧИ ПОЛНОСТЬЮ (не включай в JSON вообще) следующие типы новостей, даже если
    формально они пришли из релевантного источника:
    1. Локальные криминальные и бытовые происшествия без международного или
       политического резонанса — убийства, ДТП, пожары, местные преступления и т.п.,
       касающиеся конкретного города/района/семьи и не имеющие значения за пределами
       локального сообщества. Отличие: покушение на президента — оставляем (есть
       политический резонанс); бытовое убийство в одной семье — исключаем.
    2. Курьёзы, lifestyle-заметки, необычные бытовые тренды без практической или
       аналитической ценности для делового/политического мониторинга — например,
       "мода на X в стране Y", необычные потребительские привычки, вирусные истории
       без значимых последствий.
    3. Вакансии, карьерные страницы, списки открытых позиций в компаниях — это не
       новости, даже если компания релевантна (Bain, Deloitte, McKinsey и т.п.).
    
    Если сомневаешься, оставлять новость или нет, — задай себе вопрос: "Повлияет ли
    это на международную политику, экономику, бизнес или технологии, или это просто
    любопытный факт/локальное происшествие?" Любопытные факты и локальные
    происшествия — исключай.
    
    Входящие новости:
    __INPUT_DATA__
    """
    
    prompt = prompt_template.replace("__INPUT_DATA__", raw_data_prompt)
    # ИСПРАВЛЕНО (повторно, 12.08.2026): gemini-2.5-flash больше недоступна новым
    # пользователям ("no longer available to new users" — Google снял её с эксплуатации
    # раньше объявленного срока в октябре 2026). На момент этого исправления
    # gemini-3.6-flash — актуальная стабильная GA-модель линейки Flash.
    # Модель Gemini API меняется чаще, чем ожидалось: если 404 повторится, проверь
    # актуальный список на https://ai.google.dev/gemini-api/docs/models
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={gemini_api_key}"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            # ИСПРАВЛЕНО: temperature/top_p/top_k у моделей линейки Gemini 3.x
            # (включая gemini-3.6-flash) устарели и игнорируются — убраны, чтобы
            # не вводить в заблуждение при чтении кода.
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            # thinking_level "minimal" — для задачи классификации/структурного
            # вывода не нужен глубокий reasoning, это быстрее и дешевле.
            "thinkingConfig": {"thinkingLevel": "minimal"}
        }
    }

    max_retries = 5
    for attempt in range(max_retries):
        try:
            # ИСПРАВЛЕНО: Таймаут увеличен до 120, так как ответ на 8192 токенов генерируется долго
            response = requests.post(url, json=payload, timeout=120)
            
            if response.status_code == 429:
                # ИСПРАВЛЕНО: верхняя граница ожидания снижена (было до 300с за попытку,
                # суммарно скрипт мог зависать на 15+ минут в cron-задаче).
                wait_time = min(20 * (2 ** attempt), 90)
                print(f"⏸️  Rate limit 429. Ждем {wait_time}с (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            
            if response.status_code == 503:
                wait_time = min(15 * (2 ** attempt), 90)
                print(f"⏸️  API перегружена (503). Ждем {wait_time}с (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            
            if response.status_code == 400:
                # ИСПРАВЛЕНО: ошибка 400 (некорректный запрос) не исправится повтором —
                # раньше скрипт всё равно уходил в retry-цикл впустую.
                print(f"❌ Некорректный запрос к Gemini (400): {response.text[:200]}")
                break
            
            if response.status_code >= 500:
                wait_time = min(10 * (2 ** attempt), 90)
                print(f"⚠️  Ошибка сервера ({response.status_code}). Ждем {wait_time}с...")
                time.sleep(wait_time)
                continue
            
            if response.status_code == 200:
                result = response.json()
                return result["candidates"][0]["content"]["parts"][0]["text"]
            
            print(f"❌ Ошибка Gemini API ({response.status_code}): {response.text[:200]}")
            response.raise_for_status()
            
        except requests.exceptions.Timeout:
            wait_time = 10 * (attempt + 1)
            print(f"⏸️  Timeout API. Ждем {wait_time}с...")
            time.sleep(wait_time)
            continue
        except Exception as e:
            print(f"⚠️  Исключение: {str(e)[:100]}")
            if attempt < max_retries - 1:
                time.sleep(5 * (attempt + 1))
            continue

    print("❌ Gemini API недоступна после 5 попыток. Используем fallback.")
    return json.dumps({
        "geopolitics": [],
        "economics": [],
        "business": [],
        "technology": [],
        "energy": [],
        "security": []
    })

def clean_json_str(raw_str):
    clean = raw_str.strip()
    if clean.startswith("```json"):
        clean = clean[7:]
    elif clean.startswith("```"):
        clean = clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    return clean.strip()

def sanitize_summary_text(text):
    if not text:
        return ""
    text = re.sub(r'\s*[\(\[\{][^\)\]\}]*(RSS|Feed|News|Новости|Лента)[^\)\]\}]*[\)\]\}]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\([^\)]*\)\s*$', '', text)
    return text.strip(' .-_')

def build_html_digest(raw_response, news_db):
    json_clean = clean_json_str(raw_response)
    try:
        data = json.loads(json_clean)
        
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        elif not isinstance(data, dict):
            data = {}
            
    except Exception as e:
        print(f"⚠️  Ошибка парсинга JSON: {e}. Используем пустую структуру.")
        data = {}

    expected_categories = ["geopolitics", "economics", "business", "technology", "energy", "security"]
    
    total_items = sum(
        len(data.get(cat)) if isinstance(data.get(cat), list) else 0 
        for cat in expected_categories
    )
    
    if total_items == 0:
        print("⚠️  Модель не вернула ни одной новости (fallback структура)")

    sections = [
        ("geopolitics", "🌍 ГЕОПОЛИТИКА И МАКРО-РЕШЕНИЯ"),
        ("economics", "📈 ЭКОНОМИКА И ИНСТИТУТЫ"),
        ("business", "💼 БИЗНЕС И M&A"),
        ("technology", "🧬 ТЕХНОЛОГИИ И ИННОВАЦИИ"),
        ("energy", "⚡ ЭНЕРГЕТИКА И РЕСУРСЫ"),
        ("security", "🪖 БЕЗОПАСНОСТЬ И КОНФЛИКТЫ")
    ]

    seen_urls_in_digest = set()

    def build_one(target_is_russia, header):
        html_output = f"{header}\n\n"
        any_valid_anywhere = False
        # ИСПРАВЛЕНО: собираем URL, реально попавшие именно в этот блок
        # (world или russia отдельно), чтобы в историю попадали только те новости,
        # чей блок был подтверждённо отправлен — после отправки, а не заранее.
        section_urls = []

        for key, title in sections:
            raw_items = data.get(key)
            items = raw_items if isinstance(raw_items, list) else []
            
            filtered_items = [it for it in items if isinstance(it, dict) and bool(it.get("is_russia")) == target_is_russia]
            
            html_output += f"<b>{title}</b>\n"
            valid_items_count = 0
            
            for item in filtered_items:
                news_id = str(item.get("id", "")).strip()
                summary = sanitize_summary_text(item.get("summary_ru", ""))

                if news_id not in news_db or not summary:
                    continue

                low_summary = summary.lower()
                if any(kw in low_summary for kw in LOCAL_POLITICS_KEYWORDS):
                    continue
                if any(kw.lower() in low_summary for kw in LOCAL_CRIME_AND_TRIVIA_KEYWORDS):
                    continue

                url = news_db[news_id]["url"]
                if url in seen_urls_in_digest:
                    continue
                seen_urls_in_digest.add(url)

                source_name = news_db[news_id]["source_name"]
                # ИСПРАВЛЕНО: экранируем URL и текст перед вставкой в HTML.
                # Раньше спецсимволы (< > & ") из summary или URL могли сломать
                # Telegram-сообщение и увести его в текстовый fallback вместе с тегами.
                safe_url = html_escape(url, quote=True)
                safe_summary = html_escape(summary, quote=False)
                safe_source = html_escape(source_name, quote=False)
                html_output += f"• {safe_summary} (<a href=\"{safe_url}\">{safe_source}</a>)\n"
                valid_items_count += 1
                section_urls.append(url)

            if valid_items_count == 0:
                html_output += "• <i>Существенных сдвигов за прошедшие часы не зафиксировано</i>\n"
            else:
                any_valid_anywhere = True

            html_output += "\n"

        result_html = html_output.strip() if any_valid_anywhere else ""
        return result_html, (section_urls if result_html else [])

    world_html, world_urls = build_one(False, "🌍 <b>МИРОВАЯ ПОВЕСТКА</b>")
    russia_html, russia_urls = build_one(True, "🇷🇺 <b>РОССИЯ</b>")

    return world_html, russia_html, world_urls, russia_urls

def _send_one_chunk(chat_id, chunk):
    """Отправляет один чанк текста. Возвращает True при подтверждённом успехе."""
    try:
        bot.send_message(chat_id, chunk, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except ApiTelegramException as e:
        print(f"Ошибка отправки HTML ({e}). Отправка обычным текстом.")
        try:
            bot.send_message(chat_id, chunk)
            return True
        except Exception as e2:
            # ИСПРАВЛЕНО: раньше повторная отправка обычным текстом ничем не была
            # защищена — сетевая ошибка или проблема с chat_id роняла весь процесс.
            print(f"❌ Не удалось отправить сообщение даже как обычный текст: {e2}")
            return False
    except Exception as e:
        print(f"❌ Непредвиденная ошибка отправки в Telegram: {e}")
        return False


def send_telegram_message(chat_id, text):
    # ИСПРАВЛЕНО: функция теперь возвращает bool — реально ли отправка удалась.
    # Раньше вызывающий код считал любую попытку успешной, даже если Telegram
    # отверг сообщение.
    if not text.strip():
        return False

    if len(text) <= 4000:
        return _send_one_chunk(chat_id, text)

    blocks = text.split("\n\n")
    current_chunk = ""
    all_ok = True
    for block in blocks:
        if len(current_chunk) + len(block) + 2 <= 3900:
            current_chunk += block + "\n\n"
        else:
            if current_chunk.strip():
                all_ok = _send_one_chunk(chat_id, current_chunk.strip()) and all_ok
            current_chunk = block + "\n\n"

    if current_chunk.strip():
        all_ok = _send_one_chunk(chat_id, current_chunk.strip()) and all_ok

    return all_ok

if __name__ == "__main__":
    import sys
    
    # Читаем schedule_name из аргумента командной строки
    # Использование: python main.py morning / python main.py afternoon / python main.py evening
    schedule_name = "morning"  # default
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg in SCHEDULES:
            schedule_name = arg
        else:
            print(f"⚠️  Неизвестное расписание '{arg}', используется 'morning'. Допустимые: {list(SCHEDULES.keys())}")
    
    print(f"🕐 Запущен дайджест: {schedule_name}")
    
    sent_urls_history = load_sent_urls()

    news_db, raw_data_prompt = collect_all_news(sent_urls_history, schedule_name)

    if raw_data_prompt.strip():
        print("📊 Запрашиваем анализ из Gemini API...")
        raw_json = generate_analytical_json(raw_data_prompt)
        world_html, russia_html, world_urls, russia_urls = build_html_digest(raw_json, news_db)

        # ВАЖНО: сбор новостей и обращение к Gemini могут завершиться намного
        # раньше заявленного времени публикации (например, workflow запущен в
        # 08:00, чтобы успеть обработать afternoon-дайджест к 13:00, но сама
        # обработка занимает 3-5 минут). Раньше скрипт публиковал сразу же —
        # из-за этого дайджесты выходили "вслед за запуском workflow", а не в
        # заявленное время (08:00/13:00/19:00 МСК), и казалось, что несколько
        # дайджестов подряд выходят почти одновременно. Теперь публикация
        # намеренно откладывается до точного момента (см. wait_until_publish_time).
        wait_until_publish_time(schedule_name)

        sent_anything = False
        # ИСПРАВЛЕНО: в историю теперь попадают только те URL, чьи блоки были
        # реально и успешно отправлены в Telegram — не все собранные из RSS.
        confirmed_urls = []

        if world_html.strip():
            print("📤 Отправляем мировую повестку...")
            if send_telegram_message(CHAT_ID, world_html):
                sent_anything = True
                confirmed_urls.extend(world_urls)
            else:
                print("❌ Не удалось отправить мировую повестку — новости останутся необработанными для следующего запуска.")
            time.sleep(2)

        if russia_html.strip():
            print("📤 Отправляем новости о России...")
            if send_telegram_message(CHAT_ID, russia_html):
                sent_anything = True
                confirmed_urls.extend(russia_urls)
            else:
                print("❌ Не удалось отправить новости о России — они останутся необработанными для следующего запуска.")

        if sent_anything:
            now = time.time()
            for url in confirmed_urls:
                sent_urls_history[url] = now
            save_sent_urls(sent_urls_history)
            
            # ===== ВАЖНО: сохраняем ЛОГИЧЕСКОЕ время этого расписания =====
            # Это позволяет следующему запуску этого расписания знать, с какой
            # логической границы начинать сбор новостей, независимо от того,
            # когда физически запустился скрипт.
            save_run_time(schedule_name)
            
            print("✅ Диджест отправлен успешно!")
        else:
            print("ℹ️  Новостей для публикации не найдено, либо отправка не удалась — история не обновлена.")
    else:
        print("ℹ️  Новых материалов за прошедшие часы не обнаружено.")
