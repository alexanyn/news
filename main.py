print("=== ЗАПУСК СКРИПТА ВЕРСИИ 6.13 (RICH_MESSAGE_AND_BOUNDARY_FIX) ===")

import os
import re
import json
import time
import datetime
import requests
import feedparser
from html import escape as html_escape
from bs4 import BeautifulSoup
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1. Переменные окружения
gemini_api_key = os.environ.get("GEMINI_API_KEY")
bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")

if not gemini_api_key or not bot_token or not chat_id:
    raise ValueError("Ошибка: Проверьте GEMINI_API_KEY, TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Secrets!")

# ИСПРАВЛЕНО 2026-08-19: убран telebot.TeleBot — библиотека pyTelegramBotAPI
# не поддерживает sendRichMessage (см. _send_one_chunk), поэтому отправка
# теперь идёт напрямую через requests.post, а сам объект bot больше нигде
# не используется. bot_token и CHAT_ID остаются — они нужны для URL запроса.
CHAT_ID = chat_id

HISTORY_FILE = "sent_urls.json"
LAST_RUN_FILE = "last_run.json"

# ИСПРАВЛЕНО 2026-08-16: сбор новостей раньше шёл строго последовательно —
# один запрос за другим. Со старым списком (~37 источников) это укладывалось
# в разумное время, но после расширения списка до 92 RSS-лент (из них 43 —
# через Google News, то есть много запросов к одному и тому же хосту подряд)
# худший случай по времени вырос до ~15-20+ минут только на сбор, а ведь
# после него ещё идёт обращение к Gemini (тоже с retry, тоже может занять
# заметное время) — и всё это должно уложиться в timeout-minutes workflow.
# Параллельные запросы (каждый со своим independent timeout=15с) сокращают
# худший случай по времени примерно в MAX_FETCH_WORKERS раз, не трогая
# таймаут отдельного запроса. Значение подобрано с запасом: достаточно,
# чтобы кардинально сократить общее время, но не настолько агрессивно,
# чтобы выглядеть как DDoS для отдельных небольших сайтов.
MAX_FETCH_WORKERS = 10

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

# ИСПРАВЛЕНО 2026-08-18: порядок расписаний в сутках. Используется, чтобы
# каждый дайджест отталкивался от границы ПРЕДЫДУЩЕГО по хронологии
# дайджеста, а не от своего же вчерашнего запуска — см. get_previous_schedule_name
# и объяснение у места использования в collect_all_news.
SCHEDULE_ORDER = ["morning", "afternoon", "evening"]

def get_previous_schedule_name(schedule_name):
    """Возвращает имя расписания, которое логически предшествует данному
    в суточном цикле: evening -> afternoon, afternoon -> morning,
    morning -> evening (ПРОШЛОГО дня, Python корректно даёт последний
    элемент списка при индексе -1)."""
    idx = SCHEDULE_ORDER.index(schedule_name)
    return SCHEDULE_ORDER[idx - 1]

def get_schedule_boundary(schedule_name):
    """
    Возвращает логическое время ГРАНИЧНОЕ дайджеста (время, ДО КОТОРОГО собираются
    новости), как Unix timestamp. Считается в московском времени (см. выше).

    ИСПРАВЛЕНО 2026-08-19: раньше эта функция откатывала границу на вчера,
    если текущее время ещё не достигло целевого часа расписания (например,
    07:07 < 08:00 → граница "morning" съезжала на вчерашние 08:00). Это было
    рассчитано на сценарий "скрипт запускается ровно в момент публикации или
    позже неё" — но с cron-job.org скрипт систематически запускается ЗАРАНЕЕ
    (например, около 07:07 для morning в 08:00), именно чтобы буфер
    wait_until_publish_time успел отработать до заявленного часа. При таком
    заблаговременном запуске "сегодня ещё не пробило 08:00" не означает
    "текущий morning — это вчерашний", это означает "текущий morning — это
    СЕГОДНЯШНИЙ, просто скрипт стартовал заранее". Старая логика путала эти
    два случая, из-за чего current_boundary съезжал на день назад, ниже по
    коду last_boundary (граница предыдущего расписания) внезапно оказывался
    "в будущем" относительно current_boundary, срабатывала ветка "АНОМАЛИЯ" и
    окно сбора аварийно откатывалось ещё на 24ч — а поскольку это окно почти
    целиком уже было в sent_urls_history с прошлых прогонов, дайджест выходил
    почти пустым (реальный случай: 19.08 утренний дайджест, см. лог с
    "АНОМАЛИЯ" и итоговыми 3-5 новостей на категорию вместо обычных 8-12).

    Правильная граница "текущего" расписания — это ВСЕГДА сегодняшний
    hour:minute из SCHEDULES, независимо от того, наступил он уже по часам
    или нет: если скрипт запущен под именем "morning", то это и есть
    сегодняшний morning, а не вчерашний. Отката на вчера здесь больше нет —
    вместо этого используется публикационное время как раньше (см.
    get_next_publish_time), а сама граница сбора теперь всегда "сегодня".
    """
    if schedule_name not in SCHEDULES:
        raise ValueError(f"Unknown schedule: {schedule_name}. Valid: {list(SCHEDULES.keys())}")

    sched = SCHEDULES[schedule_name]
    hour, minute = sched["hour"], sched["minute"]

    now_dt = now_moscow()

    today_boundary_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)

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

    # --- Добавлено 2026-08-16 (третья итерация) ---
    "asia.nikkei.com": "Nikkei Asia",
    "readthediff.com": "The Diff",
    "politico.eu": "Politico Europe",
    "vedomosti.ru": "Ведомости",
    "vc.ru": "VC.ru",
    "scmp.com": "South China Morning Post",
    "tass.com": "ТАСС",
    "semafor.com": "Semafor",
    "bruegel.org": "Bruegel",
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
    "https://government.ru/en/all/rss",  # апгрейд 2026-08-16: нашлась прямая официальная лента, была на Google News fallback
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

    # ============================================================
    # Добавлено 2026-08-16 (третья итерация): источники по личному
    # списку пользователя + критический отбор из подборки 5 ИИ
    # (Grok/ChatGPT/Deepseek/Perplexity/Gemini, файл News_istochniki.rtf).
    # Логика отбора и то, что сознательно НЕ добавлено — см. сопроводительное
    # сообщение. Все URL ниже проверены вручную 2026-08-16.
    # ============================================================

    # --- Личный список пользователя ---
    "https://asia.nikkei.com/rss/feed/nar",              # Nikkei Asia
    "https://www.readthediff.com/feed",                  # The Diff (Byrne Hobart) — Substack, /feed работает на любом домене платформы
    "https://www.politico.eu/feed",                      # Politico Europe
    "https://www.vedomosti.ru/rss/news",                  # Ведомости
    "https://vc.ru/rss/all",                              # VC.ru

    # --- Критический отбор из списка 5 ИИ: geo/перспективный охват ---
    # Азия — единственный полностью "слепой" регион до этого момента;
    # все 5 ИИ независимо назвали SCMP/Nikkei топ-приоритетом.
    "https://www.scmp.com/rss/91/feed",                   # South China Morning Post — общая лента (HK/Китай/мир)

    # ТАСС — все 5 ИИ назвали его независимо; это госагентство-аналог
    # Reuters/AP по скорости и охвату, отличается от уже имеющихся
    # Интерфакса/Коммерсанта по характеру (агентская лента vs деловая пресса).
    "http://tass.com/rss/v2.xml",                         # ТАСС (англоязычная официальная лента)

    # Semafor — намеренно выбран из общего пула западных изданий (Guardian,
    # NYT, WaPo, NPR, France24, DW и т.д. — НЕ добавлены, см. сообщение),
    # т.к. это единственное издание, целенаправленно работающее на стыке
    # геополитики+бизнеса+технологий, а не ещё один универсальный ньюсрум.
    "https://semafor.com/rss.xml",                        # Semafor

    # Bruegel — единственный европейский (не американский, не российский)
    # think tank в списке; страница с их RSS не обновлялась с 2013 и сайт
    # с тех пор переехал на новую CMS, поэтому вместо риска мёртвой ссылки — fallback.
    "https://news.google.com/rss/search?q=site:bruegel.org&hl=en-US&gl=US&ceid=US:en",  # Bruegel
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
    # Добавлено 2026-08-16 (третья итерация): у обоих нет прямого RSS,
    # только Telegram. Оба канала официальные, проверены вручную.
    {"username": "rerussia_org", "source_name": "Re:Russia"},
    {"username": "istories_media", "source_name": "Важные истории"},   # маркировано в РФ как нежелательная организация — как и у нескольких источников выше (Meduza, The Bell)
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

def clean_rss_summary(raw_summary, max_len=400):
    """Очищает поле summary/description из RSS-записи: убирает HTML-теги
    (частое явление — многие ленты кладут в summary целый HTML-абзац со
    ссылками), схлопывает пробелы/переносы строк, обрезает до max_len
    символов. Возвращает "" для пустого/отсутствующего значения — вызывающий
    код в этом случае просто не добавляет поле Summary в данные для Gemini."""
    if not raw_summary:
        return ""
    try:
        text = BeautifulSoup(raw_summary, "html.parser").get_text(separator=" ", strip=True)
    except Exception:
        text = raw_summary
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


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

    # ИСПРАВЛЕНО 2026-08-18: раньше last_boundary читался из СВОЕГО ЖЕ
    # предыдущего запуска (last_run.json[schedule_name]) — это давало окно
    # сбора ~24 часа для КАЖДОГО из трёх дайджестов в сутках, с огромным
    # перекрытием между ними (например, morning и afternoon перекрывались
    # почти полностью). Различие дайджестов целиком держалось на
    # sent_urls_history — если история искажалась (повторный/ручной запуск,
    # задержка одного из дайджестов и т.п.), следующий дайджест мог
    # оказаться почти пустым, даже если формально окно было широким:
    # реальный случай — evening после хаотичного дня с задержками откатился
    # на 24ч как "похоже на повторный запуск", но почти всё в этом окне уже
    # было в истории отправленного с более раннего прогона того же дня.
    #
    # Теперь last_boundary берётся из границы ПРЕДЫДУЩЕГО по хронологии
    # дайджеста (для evening это afternoon, для afternoon — morning, для
    # morning — evening ПРОШЛОГО дня). Окно сбора становится узким и
    # предсказуемым (~5-6 часов, ровно как описано в комментариях к cron
    # в workflow), не зависит от того, запускался ли этот же дайджест
    # сегодня повторно, и не полагается на sent_urls_history как на
    # единственную линию защиты от дублей (хотя она остаётся полезной
    # подстраховкой на случай пересечения окон при ручных перезапусках).
    previous_schedule = get_previous_schedule_name(schedule_name)
    last_boundary = get_last_run_time(previous_schedule)

    if last_boundary is None:
        # Предыдущее по хронологии расписание вообще ни разу не запускалось
        # (первый день работы скрипта) — берём с запасом 24 часа назад.
        last_boundary = current_boundary - 24 * 3600
        print(f"⚠️  Расписание '{previous_schedule}' (предыдущее для {schedule_name}) ещё "
              f"ни разу не запускалось, установлено окно в 24 часа назад")
    elif last_boundary >= current_boundary:
        # Аномалия: граница предыдущего расписания оказалась в будущем
        # относительно текущей. При новой логике это не должно происходить
        # в норме (в отличие от старой логики, где это была ожидаемая
        # ситуация при повторном запуске того же расписания) — если
        # сработало, это сигнал о более серьёзной проблеме с часами/данными,
        # а не штатный повторный прогон. Откатываемся на 24ч, чтобы не
        # получить пустое/отрицательное окно, и громко предупреждаем.
        print(f"⚠️  АНОМАЛИЯ: граница расписания '{previous_schedule}' ({fmt_msk(last_boundary)}) "
              f">= текущей границы {schedule_name} ({fmt_msk(current_boundary)}). "
              f"Это не должно происходить при нормальной работе — откатываемся на 24ч " 
              f"и стоит проверить last_run.json вручную.")
        last_boundary = current_boundary - 24 * 3600
    
    print(f"📅 {schedule_name.upper()} дайджест: собираем новости с {fmt_msk(last_boundary)} до {fmt_msk(current_boundary)}")
    print(f"📡 Начинаем парсинг {len(RSS_FEEDS)} лент (параллельно, до {MAX_FETCH_WORKERS} одновременно)...")

    # ИСПРАВЛЕНО 2026-08-18: диагностические счётчики. Раньше в логе было
    # видно только финальное число "собрано N новостей" — если N маленькое,
    # непонятно, была ли причина в узком окне, в истории отправленного или
    # просто в затишье новостей. Теперь видно разбивку по причине пропуска,
    # это делает будущую диагностику подобных ситуаций вопросом одной
    # секунды, а не догадок по коду.
    stats = {"raw_seen": 0, "skip_sent": 0, "skip_window": 0, "skip_no_title": 0, "included": 0}

    # ИСПРАВЛЕНО 2026-08-16: было — строго последовательный for-цикл, худший
    # случай по времени рос линейно с числом лент. Теперь сетевые запросы
    # (fetch_feed) выполняются в пуле потоков, а вся работа с news_db /
    # raw_data_list (общее изменяемое состояние) по-прежнему выполняется
    # ТОЛЬКО в главном потоке, в теле цикла ниже — это не гонка данных,
    # т.к. рабочие потоки лишь скачивают и парсят фид и ничего не пишут в
    # общие структуры.
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as executor:
        future_to_url = {executor.submit(fetch_feed, url): url for url in RSS_FEEDS}
        for future in as_completed(future_to_url):
            feed_url = future_to_url[future]
            try:
                parsed = future.result()
            except Exception as e:
                print(f"⚠️  Необработанное исключение в потоке при получении {feed_url}: {e}")
                continue

            if not parsed or not parsed.entries:
                continue

            source_name = get_source_name(feed_url)

            for entry in parsed.entries[:5]:
                stats["raw_seen"] += 1
                url = entry.get("link", "")
                if not url or url in sent_urls_history:
                    stats["skip_sent"] += 1
                    continue

                # ===== КЛЮЧЕВОЙ ФИЛЬТР: по дате публикации и логическому окну =====
                published_str = entry.get("published", "")
                published_ts = parse_published_time(published_str)

                # Включаем новость только если её дата публикации попадает в нужное окно:
                # last_boundary <= published < current_boundary
                if published_ts is not None:
                    if published_ts < last_boundary or published_ts >= current_boundary:
                        # Новость вне окна — пропускаем
                        stats["skip_window"] += 1
                        continue
                # Если дата не парсится (published_ts is None), берём новость на доверие
                # (предполагаем, что RSS отдаёт свежий контент в нужном порядке)

                title = entry.get("title", "").strip()
                if not title:
                    stats["skip_no_title"] += 1
                    continue

                # ИСПРАВЛЕНО 2026-08-18: раньше Gemini получала только голый
                # заголовок — этого достаточно для факта "что случилось", но
                # недостаточно, чтобы объяснить "почему" и "что дальше" (по
                # явному запросу пользователя). У большинства RSS-лент есть
                # поле summary/description с 1-3 предложениями о статье —
                # раньше оно просто игнорировалось. Берём его как реальный
                # материал для содержательного описания вместо пустых общих
                # фраз. HTML-теги внутри summary (частое явление в RSS) чистим,
                # длину ограничиваем, чтобы не раздувать промпт до Gemini.
                summary_snippet = clean_rss_summary(entry.get("summary", ""))

                stats["included"] += 1
                news_id = str(len(news_db))
                news_db[news_id] = {
                    "url": url,
                    "title": title,
                    "source_name": source_name,
                    "published": published_str
                }

                if summary_snippet:
                    raw_data_list.append(f"ID:{news_id}|Title:{title}|Summary:{summary_snippet}|Source:{source_name}")
                else:
                    raw_data_list.append(f"ID:{news_id}|Title:{title}|Source:{source_name}")
                # ИСПРАВЛЕНО: URL больше НЕ добавляется в историю здесь.
                # Раньше новость считалась "отправленной" уже на этапе сбора из RSS,
                # то есть до того, как она реально прошла через Gemini и ушла в Telegram.
                # Если Gemini или Telegram падали, новость терялась на 7 дней, хотя
                # фактически никуда не отправлялась. Теперь запись в историю происходит
                # только после подтверждённой отправки в Telegram (см. build_html_digest
                # и блок __main__).
    
    print(f"✅ Собрано {len(news_db)} новостей из {len(RSS_FEEDS)} RSS-источников")
    print(f"   📊 Разбивка RSS: всего просмотрено {stats['raw_seen']}, "
          f"уже отправлялось ранее {stats['skip_sent']}, вне окна времени {stats['skip_window']}, "
          f"без заголовка {stats['skip_no_title']}, включено {stats['included']}")

    if TELEGRAM_CHANNELS:
        print(f"📡 Начинаем парсинг {len(TELEGRAM_CHANNELS)} Telegram-каналов (параллельно)...")
        tg_collected = 0
        tg_stats = {"raw_seen": 0, "skip_sent": 0, "skip_window": 0}
        with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(TELEGRAM_CHANNELS))) as executor:
            future_to_channel = {
                executor.submit(fetch_telegram_channel, channel["username"]): channel
                for channel in TELEGRAM_CHANNELS
            }
            for future in as_completed(future_to_channel):
                channel = future_to_channel[future]
                source_name = channel["source_name"]
                try:
                    messages = future.result()
                except Exception as e:
                    print(f"⚠️  Необработанное исключение при получении канала @{channel['username']}: {e}")
                    continue

                # Как и для RSS, берём только несколько последних сообщений за проход,
                # чтобы не заваливать Gemini старым контентом при первом запуске.
                for msg in messages[-5:]:
                    tg_stats["raw_seen"] += 1
                    url = msg["link"]
                    if not url or url in sent_urls_history:
                        tg_stats["skip_sent"] += 1
                        continue

                    # Аналогичный фильтр по дате для Telegram-сообщений
                    published_str = msg["published"]
                    published_ts = parse_published_time(published_str)

                    if published_ts is not None:
                        if published_ts < last_boundary or published_ts >= current_boundary:
                            tg_stats["skip_window"] += 1
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
        print(f"   📊 Разбивка Telegram: всего просмотрено {tg_stats['raw_seen']}, "
              f"уже отправлялось ранее {tg_stats['skip_sent']}, вне окна времени {tg_stats['skip_window']}, "
              f"включено {tg_collected}")

    raw_data_prompt = "\n".join(raw_data_list)
    return news_db, raw_data_prompt

def generate_analytical_json(raw_data_prompt):
    prompt_template = """Проанализируй следующие новости и дай структурированный JSON анализ.
    
    Верни ТОЛЬКО валидный JSON без пояснений и Markdown, в следующем формате:
    {
        "geopolitics": [{"id": "1", "summary_ru": "Развёрнутое резюме на русском (2-4 предложения)", "is_russia": false}, ...],
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
    
    ВАЖНО (глубина summary_ru): читатель этого дайджеста хочет не просто знать,
    что что-то произошло, а ПОНИМАТЬ происходящее — почему это случилось и
    что это может значить дальше. Поэтому summary_ru — это не заголовок в
    одну строку, а 2-4 предложения, которые по возможности включают:
      1) что произошло (факт);
      2) контекст/причину — ЕСЛИ она есть в исходных данных (поле Summary
         у новости, если оно присутствует) или является общеизвестным
         фактом — почему это произошло именно сейчас, что этому предшествовало;
      3) вероятное значение/последствия — ЕСЛИ это можно обоснованно вывести
         из данных — что это может изменить, на кого повлиять.
    Опирайся на поля Title И Summary у каждой новости (Summary есть не у
    всех — там, где его нет, работай с Title). НЕ СОЧИНЯЙ факты, даты, цифры
    или причинно-следственные связи, которых нет в предоставленных данных и
    которые не являются общеизвестными. Если по новости известен только сам
    факт без понятной причины/значения — дай короткое точное summary_ru БЕЗ
    выдуманного "анализа", это лучше, чем гладкая, но пустая по содержанию
    фраза вроде "это может повлиять на дальнейшее развитие событий" ни о чём
    конкретно. Точность важнее объёма.
    
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

    ВАЖНО (дубли): часто одно и то же событие освещают НЕСКОЛЬКО источников
    из списка (например, Reuters, AP и ТАСС могут написать об одном и том же
    решении ЦБ или заявлении политика). Если в raw-данных несколько записей
    по смыслу описывают ОДНО И ТО ЖЕ событие — выбери из них только ОДНУ
    (id того источника, который точнее или авторитетнее) и включи в итоговый
    JSON один-единственный пункт, а не отдельную запись на каждый источник,
    рассказавший об этом. Это правило НЕ относится к по-настоящему РАЗНЫМ
    новостям на одну тему — например, "ЦБ снизил ставку" и "рынки отреагировали
    ростом на решение ЦБ" это два разных, валидных пункта, а не дубли.
    
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
    4. Для категорий technology и business — рядовые корпоративные пресс-релизы
       без значимого влияния: анонсы небольших обновлений продукта, партнёрства
       без крупных финансовых деталей, общие материалы вида "компания X
       поделилась мнением о тренде Y" от консалтинговых компаний без по-настоящему
       нового вывода, некрупные раунды финансирования без признаков значимости.
       ОСТАВЛЯЙ: крупные сделки/M&A, значимые раунды финансирования, регуляторные
       решения в отношении технологических/бизнес-компаний, смену руководства
       крупных компаний, продукты/решения с заметным рыночным значением, значимую
       финансовую отчётность. Ориентир: указан ли в новости конкретный масштаб
       (сумма сделки, доля рынка, число пользователей и т.п.) — просто "компания
       сделала X" без масштаба обычно и есть рядовой пресс-релиз, который надо
       исключить.
    
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
            # ИСПРАВЛЕНО 2026-08-16: было 8192 — это всего ~12% от реального
            # потолка gemini-3.6-flash (65 536 токенов, см. ai.google.dev/
            # gemini-api/docs/models). Подняли до 32768 после расширения списка
            # источников.
            # ИСПРАВЛЕНО 2026-08-18: summary_ru теперь 2-4 предложения (причины,
            # последствия) вместо одной строки — по объёму это в 2-4 раза больше
            # текста на каждую новость. 32768 могло стать тесно в насыщенный
            # новостями день. Подняли до полного потолка модели — лишних
            # токенов это не тратит (модель не обязана использовать весь лимит,
            # это именно потолок на случай необходимости), а стоимость на одном
            # вызове пренебрежимо мала даже при полном использовании.
            "maxOutputTokens": 65536,
            "responseMimeType": "application/json",
            # thinking_level "minimal" — для задачи классификации/структурного
            # вывода не нужен глубокий reasoning, это быстрее и дешевле.
            "thinkingConfig": {"thinkingLevel": "minimal"}
        }
    }

    max_retries = 5
    for attempt in range(max_retries):
        try:
            # ИСПРАВЛЕНО 2026-08-18: таймаут увеличен с 220 до 280 — вслед за
            # ростом maxOutputTokens до полного потолка модели (65536), ответ
            # в затратном по времени случае генерируется дольше.
            response = requests.post(url, json=payload, timeout=280)
            
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
    # ИСПРАВЛЕНО 2026-08-18: раньше summary_ru была одной короткой строкой,
    # перенос строки внутри неё был маловероятен. Теперь резюме — 2-4
    # предложения, и если Gemini вдруг вставит внутрь \n (JSON это
    # технически допускает), это сломает логику _pack_lines_into_chunks,
    # которая устроена как "одна новость = одна строка". Схлопываем любые
    # пробельные последовательности (включая переносы строк) в один пробел,
    # чтобы это исключить в принципе.
    text = re.sub(r'\s+', ' ', text)
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
    """Отправляет один чанк текста через sendRichMessage. Возвращает True при
    подтверждённом успехе.

    ДОБАВЛЕНО 2026-08-19: переход с sendMessage (лимит 4096 символов) на
    sendRichMessage (Telegram Bot API 10.1+, добавлен июнь 2026) — метод
    подтверждённо работает с этим ботом и каналом (см. rich_message_test/,
    реальный вызов дал HTTP 200 и корректно разобранную структуру блоков).
    Используется простой режим InputRichMessage — тот же готовый HTML, что
    скрипт и раньше формировал для parse_mode="HTML", просто передаётся в
    поле rich_message.html вместо параметра text. Лимит на этот HTML —
    32768 символов вместо 4096, поэтому дайджест почти всегда укладывается
    в одно сообщение целиком (см. _pack_lines_into_chunks и её новый limit).

    ИСПРАВЛЕНО 2026-08-19 (повторно, после первого боевого прогона):
    sendRichMessage парсит HTML в структуру блоков и НЕ воспринимает голые
    "\\n" как переносы строк — в отличие от старого sendMessage с
    parse_mode="HTML", где "\\n" в тексте всегда рендерился как перенос
    строки. Без этой правки весь дайджест уходил ОДНИМ параграфом: все
    заголовки категорий и новости склеивались в одну сплошную строку без
    единого переноса (реальный случай — утренний дайджест 19.08, см.
    сообщение пользователя с полным текстом поста). Сигнал об этом был
    уже в самом первом тестовом вызове (rich_message_test/): несмотря на
    "\\n\\n" в исходном HTML теста, Telegram вернул ОДИН блок
    {"type": "paragraph", ...} — это и есть доказательство, что "\\n"
    схлопывается, просто тогда это не было замечено.
    Правильный способ получить видимые переносы строк — явные теги <br>
    (подтверждено сторонними источниками, включая билдер HTML именно под
    sendRichMessage: пример "<p>...!<br>дальше текст...</p>" показывает,
    что <br> внутри параграфа даёт перенос строки, а голый "\\n" — нет).
    Конверсия сделана здесь, а не на этапе генерации HTML в
    build_html_digest, потому что _pack_lines_into_chunks режет текст на
    сообщения именно по "\\n" (одна новость = одна строка при упаковке) —
    если заменить "\\n" на "<br>" раньше, логика упаковки по строкам
    сломается. Поэтому "\\n" остаётся внутренним разделителем строк во всём
    пайплайне вплоть до этой точки, и превращается в "<br>" только в самый
    последний момент, непосредственно перед формированием API-запроса.
    Двойной перенос (пустая строка между категориями) даёт "<br><br>", что
    даёт видимую пустую строку — то же самое поведение, что было в старом
    sendMessage-режиме.

    Вызывается напрямую через requests, в обход pyTelegramBotAPI — библиотека
    метод не поддерживает (не единственная: python-telegram-bot на момент
    проверки тоже без типизированной поддержки), а сам метод достаточно
    простой, чтобы не тащить ради него отдельную зависимость.

    Осознанно НЕТ отката на старый sendMessage при ошибке: если
    sendRichMessage вдруг откажет, ошибка должна быть видна в логах прогона
    как есть, а не маскироваться тихим переключением на другой формат
    отправки — так проще заметить и разобраться, если Telegram изменит
    поведение метода."""
    # ВАЖНО: payload намеренно ограничен ровно теми полями, что были в
    # реально проверенном тестовом вызове (rich_message_test/), который дал
    # HTTP 200 — см. подтверждённую структуру ответа в логах теста от
    # 2026-08-18. Параметр про отключение превью ссылок (был
    # disable_web_page_preview=True в старом sendMessage через telebot) сюда
    # намеренно НЕ добавлен: неизвестно, поддерживает ли sendRichMessage
    # такое поле и как оно называется, а угадывать формат непроверенного
    # параметра в боевом коде — плохая идея. Если после перехода в реальных
    # дайджестах появятся крупные превью-карточки под ссылками, это будет
    # видно сразу и параметр можно будет добавить осознанно, а не наугад.
    html_for_api = chunk.replace("\n", "<br>")
    url = f"https://api.telegram.org/bot{bot_token}/sendRichMessage"
    payload = {
        "chat_id": chat_id,
        "rich_message": {"html": html_for_api},
    }
    try:
        response = requests.post(url, json=payload, timeout=30)
        data = response.json()
        if response.status_code == 200 and data.get("ok"):
            return True
        print(f"❌ Telegram отклонил sendRichMessage (HTTP {response.status_code}): "
              f"{data.get('description', data)}")
        return False
    except requests.exceptions.RequestException as e:
        print(f"❌ Сетевая ошибка при отправке sendRichMessage: {e}")
        return False
    except Exception as e:
        print(f"❌ Непредвиденная ошибка отправки в Telegram: {e}")
        return False


def _pack_lines_into_chunks(text, limit=32000):
    """Разбивает text на сообщения не длиннее limit символов.

    ИСПРАВЛЕНО 2026-08-19: limit поднят с 3900 (старый предел sendMessage,
    4096 символов с запасом) до 32000 (новый предел sendRichMessage, 32768
    символов с небольшим запасом — см. _send_one_chunk). При обычном объёме
    дайджеста этого хватает, чтобы всё уместилось в один чанк вообще без
    дробления; логика ниже остаётся на случай редкого насыщенного дня, когда
    даже 32000 не хватает — тогда упаковка по-прежнему работает, просто
    результат будет 2+ сообщений вместо обычного одного.

    ИСПРАВЛЕНО 2026-08-17: раньше упаковка шла в два уровня — сначала по
    границам категорий (двойной перевод строки), и только если ОТДЕЛЬНАЯ
    категория целиком не влезала — по отдельным новостям. Из-за этого
    возникало два конкретных бага, которые пользователь поймал вживую:
      1) Если первая категория сама по себе была близка к лимиту, заголовок
         дайджеста ("МИРОВАЯ ПОВЕСТКА") не успевал "приклеиться" к ней на
         уровне абзацев и улетал ОТДЕЛЬНЫМ сообщением сам по себе.
      2) Когда категория дробилась по новостям, "хвостик" (последние 2-3
         новости, не поместившиеся в первый чанк) никогда не пытался
         склеиться со следующей категорией, даже если места хватало с
         запасом — потому что деление на уровне абзацев уже "зафиксировало"
         границы ДО того, как стало известно, что категория не влезает.
    Починено переходом на ОДИН проход по отдельным строкам (одна новость =
    одна строка) без искусственных границ на уровне абзацев — заголовок,
    хвостик и следующая категория теперь просто жадно упаковываются в одно
    сообщение, пока есть место.

    Дополнительно: не оставляет заголовок категории ("<b>...</b>") последней
    строкой сообщения без единого пункта следом — переносит такой "голый"
    заголовок в начало следующего сообщения, чтобы он не был оторван от
    своего контента.

    Одна строка сама по себе длиннее limit (в реальности почти невозможно
    для одной новости) — крайний случай: такая строка попадёт в сообщение
    одна, даже если это означает превышение limit; дробить новость по
    буквам смысла нет."""
    lines = text.split("\n")
    chunks = []
    current = []

    def is_dangling_header(ls):
        return bool(ls) and ls[-1].strip().startswith("<b>") and ls[-1].strip().endswith("</b>")

    i = 0
    while i < len(lines):
        candidate = current + [lines[i]]
        if current and len("\n".join(candidate)) > limit:
            if is_dangling_header(current):
                dangling = current.pop()
                joined = "\n".join(current).strip()
                if joined:
                    chunks.append(joined)
                current = [dangling]
            else:
                joined = "\n".join(current).strip()
                if joined:
                    chunks.append(joined)
                current = []
            continue
        current = candidate
        i += 1

    joined = "\n".join(current).strip()
    if joined:
        chunks.append(joined)

    return chunks


def send_telegram_message(chat_id, text):
    # ИСПРАВЛЕНО: функция теперь возвращает bool — реально ли отправка удалась.
    # Раньше вызывающий код считал любую попытку успешной, даже если Telegram
    # отверг сообщение.
    if not text.strip():
        return False

    # ИСПРАВЛЕНО 2026-08-19: порог короткого пути поднят с 4000 (под старый
    # sendMessage) до 32000 (под sendRichMessage, см. _send_one_chunk и
    # _pack_lines_into_chunks). При обычном объёме дайджеста весь текст
    # проходит этим путём и уходит одним сообщением без дробления вообще.
    if len(text) <= 32000:
        return _send_one_chunk(chat_id, text)

    all_chunks = _pack_lines_into_chunks(text, limit=32000)

    # ИСПРАВЛЕНО 2026-08-16: после расширения списка источников итоговое
    # число чанков в одном дайджесте может стать заметно больше, чем раньше
    # (было обычно 1-2 на весь диджест, теперь реалистично больше). Отправка
    # нескольких сообщений подряд без пауз рискует упереться в flood control
    # Telegram (ошибка 429 "Too Many Requests: retry after..."), а
    # _send_one_chunk при такой ошибке не ждёт и не повторяет попытку —
    # чанк просто терялся бы. Пауза в 1с между сообщениями держит скорость
    # отправки в безопасных пределах.
    all_ok = True
    for i, chunk in enumerate(all_chunks):
        if i > 0:
            time.sleep(1)
        all_ok = _send_one_chunk(chat_id, chunk) and all_ok

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

        # ИСПРАВЛЕНО 2026-08-19: мир и Россия раньше уходили ДВУМЯ отдельными
        # сообщениями (лимит sendMessage в 4096 символов не позволял надёжно
        # держать оба блока в одном). После перехода на sendRichMessage
        # (лимит 32768, см. _send_one_chunk) весь дайджест почти всегда
        # укладывается в одно сообщение целиком — решено объединить оба
        # блока в один rich message, а не держать искусственное разделение,
        # оставшееся от старого лимита.
        #
        # Побочный эффект объединения: отправка теперь атомарна — либо весь
        # дайджест (мир + Россия) подтверждён и все его URL уходят в историю,
        # либо ничего не подтверждено и всё остаётся на следующий запуск.
        # Раньше при частичном сбое (например, мир ушёл, а Россия — нет из-за
        # сетевой ошибки между двумя вызовами) один блок подтверждался, а
        # другой нет; такой частичный случай больше невозможен по конструкции,
        # так как это один вызов API, а не два подряд.
        combined_parts = [html for html in (world_html, russia_html) if html.strip()]
        combined_html = "\n\n".join(combined_parts)
        combined_urls = world_urls + russia_urls

        if combined_html.strip():
            print("📤 Отправляем дайджест (мир + Россия)...")
            if send_telegram_message(CHAT_ID, combined_html):
                sent_anything = True
                confirmed_urls.extend(combined_urls)
            else:
                print("❌ Не удалось отправить дайджест — новости останутся необработанными для следующего запуска.")

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
