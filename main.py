print("=== ЗАПУСК СКРИПТА ВЕРСИИ 6.11 (FIXED_MODEL_HISTORY_AND_TELEGRAM_SAFETY) ===")

import os
import re
import json
import time
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

# Расписание: логическое время публикации дайджестов (UTC+0)
# Скрипт может запускаться раньше этих времён (например, в 3:00 вместо 8:00),
# но собирать новости будет ЛОГИЧЕСКИ за нужный промежуток времени.
SCHEDULES = {
    "morning": {"hour": 8, "minute": 0},      # 08:00 логически
    "afternoon": {"hour": 13, "minute": 0},   # 13:00 логически
    "evening": {"hour": 19, "minute": 0},     # 19:00 логически
}

def get_schedule_boundary(schedule_name):
    """
    Возвращает логическое время ГРАНИЧНОЕ дайджеста (время, ДО КОТОРОГО собираются новости).
    
    Если сейчас 03:15 и запущен "morning" дайджест:
    - Логическая граница = 08:00 СЕГОДНЯ (если ещё не наступила) или ВЧЕРА (если уже прошла)
    - Вчера в 08:00 был последний morning → собираем с вчера 08:00 до сегодня 08:00
    
    Если сейчас 09:15 и запущен "morning" дайджест:
    - Логическая граница = 08:00 СЕГОДНЯ (уже прошла, используем как текущую)
    """
    if schedule_name not in SCHEDULES:
        raise ValueError(f"Unknown schedule: {schedule_name}. Valid: {list(SCHEDULES.keys())}")
    
    sched = SCHEDULES[schedule_name]
    hour, minute = sched["hour"], sched["minute"]
    
    now = time.time()
    now_local = time.localtime(now)
    
    # Строим время "сегодня HH:MM:00"
    today_boundary = time.mktime(time.struct_time((
        now_local.tm_year,
        now_local.tm_mon,
        now_local.tm_mday,
        hour, minute, 0,
        0, 0, -1
    )))
    
    # Если сейчас ДО этого времени, то граница — вчера, не сегодня
    if now < today_boundary:
        today_boundary -= 24 * 3600
    
    return today_boundary

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
        print(f"💾 Сохранено время последнего запуска {schedule_name}: {time.ctime(boundary)}")
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
    "capitaleconomics": "Capital Economics",
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

    # rss.app feed ID → каноническое имя источника. get_source_name ищет
    # подстроку в URL ленты; для rss.app-ссылок URL не содержит домен
    # оригинального сайта (только rss.app/feeds/<id>.xml), поэтому сопоставляем
    # по уникальному ID фида, который сохраняется в конце URL.
    "QTDcfTTqQ0hm1o91.xml": "Эксперт",
    "iuMD8g2rxB14m0mz.xml": "Эксперт",
    "iT2Pt3BurL81siKW.xml": "Council on Foreign Relations",
    "O80a99tgLfAFfWZa.xml": "Council on Foreign Relations",
    "AOv0nmn998dUthN2.xml": "CSIS",
    "0UicvtRq1ayThLrG.xml": "RAND Corporation",
    "3OxJShbgzLo6cbye.xml": "NATO",
    "jqU6AAh8tOqfvcuN.xml": "VoxEU"
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
    "https://eng.globalaffairs.ru/feed/",
    "https://globalaffairs.ru/feed/",
    "https://carnegieendowment.org/rss/publications/",
    "https://www.atlanticcouncil.org/feed/",
    "https://www.brookings.edu/feed/",
    "https://www.crisisgroup.org/rss.xml",
    "https://russiancouncil.ru/rss/",
    "https://news.google.com/rss/search?q=site:imemo.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:veb.ru+институт&hl=ru&gl=RU&ceid=RU:ru",
    "https://www.csr.ru/rss/",
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
    "https://www.interfax.ru/business/rss.asp",
    "https://www.technologyreview.com/feed/",
    "https://stratechery.com/feed/",
    "https://techcrunch.com/feed/",
    "https://restofworld.org/feed/",
    "https://www.iea.org/rss/news",
    "https://www.eia.gov/rss/todayinenergy.xml",
    "https://www.opec.org/opec_web/en/rss/press_releases.xml",
    "https://www.state.gov/feed/",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://news.google.com/rss/search?q=site:congress.gov&hl=en-US&gl=US&ceid=US:en",
    "https://ec.europa.eu/commission/presscorner/api/rss",
    "https://www.ecb.europa.eu/rss/press.xml",
    "https://news.un.org/feed/subscribe/en/news/all/rss.xml",

    # ===== Замена умерших RSS через rss.app (обновление раз в 24ч на бесплатном тарифе) =====
    # Оригинальные RSS этих источников отдавали 403/404 напрямую — сайты либо
    # удалили RSS-ленту, либо блокируют автоматические запросы. rss.app скрейпит
    # сайт сам и отдаёт результат в виде RSS, обходя эти ограничения.
    "https://rss.app/feeds/QTDcfTTqQ0hm1o91.xml",  # expert.ru
    "https://rss.app/feeds/iuMD8g2rxB14m0mz.xml",  # expert.ru/mnenie
    "https://rss.app/feeds/iT2Pt3BurL81siKW.xml",  # cfr.org/expert-takes
    "https://rss.app/feeds/O80a99tgLfAFfWZa.xml",  # cfr.org/backgrounders
    "https://rss.app/feeds/AOv0nmn998dUthN2.xml",  # csis.org/analysis
    "https://rss.app/feeds/0UicvtRq1ayThLrG.xml",  # rand.org/pubs.html
    "https://rss.app/feeds/3OxJShbgzLo6cbye.xml",  # nato.int/news-and-events/articles/news
    "https://rss.app/feeds/jqU6AAh8tOqfvcuN.xml",  # cepr.org
]

# Telegram-каналы обрабатываются отдельно от RSS_FEEDS: у них нет RSS-ленты,
# контент собирается парсингом публичной веб-версии t.me/s/<channel>.
# Financial Times заменён на этот канал вместо RSS с ft.com, так как сайт FT
# требует подписку, а канал публикует статьи бесплатно (с переводом на русский).
TELEGRAM_CHANNELS = [
    {"username": "the_financial_times_journal", "source_name": "Financial Times (Telegram)"},
]

LOCAL_POLITICS_KEYWORDS = [
    "муниципал", "выбор", "депутат", "областной", "районный", "край",
    "администрац", "мэр", "губернатор", "чиновник", "снять", "уволен",
    "назначен", "отставка", "главный", "голосов"
]

# Страховочный фильтр на случай, если модель всё же пропустит локальную
# криминальную хронику или бытовой курьёз без международного значения —
# дополняет исключения, заданные в промпте для Gemini (см. generate_analytical_json).
LOCAL_CRIME_AND_TRIVIA_KEYWORDS = [
    "убил жену", "убил мужа", "убил дочь", "убил сына", "убил родствен",
    "застрелил", "зарезал", "ДТП", "сбил насмерть", "поджог дома",
    "бытовое убийство", "семейная ссора закончилась",
]

def fetch_feed(url, timeout=15):
    # Увеличенный таймаут для источников, которые исторически падали по TIMEOUT
    # (нестабильные/медленные серверы — русские аналитические центры).
    req_timeout = 30 if ("globalaffairs.ru" in url or "csr.ru" in url) else timeout

    # ВНИМАНИЕ: отключение проверки SSL-сертификата — это компромисс по
    # безопасности, применяется точечно ТОЛЬКО для csr.ru из-за конкретной
    # ошибки "hostname mismatch" на их сертификате (не наша проблема, а
    # неправильно настроенный сертификат на стороне csr.ru). Если сайт
    # почему-либо станет отдавать вредоносный контент через MITM, это не
    # будет обнаружено. Риск невысокий (публичный RSS госоргана), но стоит
    # знать, что это осознанное исключение, а не общее правило.
    verify_ssl = False if "csr.ru" in url else True

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
        headers["User-Agent"] = "NewsDigestBot arsenalexanyn@gmail.com"

    try:
        response = requests.get(
            url,
            timeout=req_timeout,
            headers=headers,
            verify=verify_ssl
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
              f"{time.ctime(last_boundary)} >= текущей {time.ctime(current_boundary)}). "
              f"Похоже на повторный/ручной запуск. Пересобираем окно за последние 24 часа "
              f"вместо пустого/некорректного диапазона.")
        last_boundary = current_boundary - 24 * 3600
    
    print(f"📅 {schedule_name.upper()} дайджест: собираем новости с {time.ctime(last_boundary)} до {time.ctime(current_boundary)}")
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
