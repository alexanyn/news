import os
import re
import json
import time
import difflib
import datetime
import asyncio
import aiohttp
import argparse
import sys
import diskcache
import requests
import feedparser
from html import escape as html_escape
from bs4 import BeautifulSoup
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONFIG_FILE = "config.json"
PROMPT_TEMPLATE_FILE = "prompt_template.txt"
PROMPT_TEMPLATE = None

DEFAULT_CONFIG = {
    "dedup": {
        "title_similarity_threshold_same_run": 0.55,
        "title_similarity_threshold_cross_run": 0.62,
        "recent_titles_window_hours": 48,
        "recent_prompt_window_hours": 12,
        "recent_prompt_max_items": 150,
    },
    "digest": {
        "max_items_per_category_per_region": 7,
    },
    "collection": {
        "max_fetch_workers": 10,
    },
    "alerts": {
        "on_statuses": ["crashed", "send_failed", "empty_no_candidates", "empty_no_digest_items"],
    },
    "rss_health": {
        "stale_threshold_days": 7,
    },
    "fetch": {
        "rss_timeout_seconds": 25,
        "telegram_timeout_seconds": 20,
    },
    "company_significance": {
        "global_major": [],
        "global_well_known": [],
        "russian_major": [],
    },
    "pr_source_domains": [],
    "ignore_stale": False,
    "scheduling": {},
}

def _deep_merge(base, override):
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user_config = json.load(f)
    except Exception as e:
        print(f"⚠️  Ошибка чтения {CONFIG_FILE} ({e}) — используются встроенные значения по умолчанию")
        return dict(DEFAULT_CONFIG)
    return _deep_merge(DEFAULT_CONFIG, user_config)

CONFIG = load_config()

gemini_api_key = os.environ.get("GEMINI_API_KEY")
bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")
alert_chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID") or chat_id
alert_webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
CHAT_ID = chat_id

def _require_runtime_env_vars():
    if not gemini_api_key or not bot_token or not chat_id:
        raise ValueError("Ошибка: Проверьте GEMINI_API_KEY, TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Secrets!")

HISTORY_FILE = "sent_urls.json"
LAST_RUN_FILE = "last_run.json"
RECENT_TITLES_FILE = "recent_titles.json"
METRICS_FILE = "metrics.jsonl"
PENDING_FILE = "pending_digest.html"
STALE_SOURCES_FILE = "stale_sources.json"

_cache = None

def get_cache():
    global _cache
    if _cache is None:
        _cache = diskcache.Cache(".http_cache")
    return _cache

MOSCOW_OFFSET = datetime.timedelta(hours=3)
MOSCOW_TZ = datetime.timezone(MOSCOW_OFFSET)

def now_moscow():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(MOSCOW_TZ)

def fmt_msk(ts):
    return datetime.datetime.fromtimestamp(ts, MOSCOW_TZ).strftime("%a %b %d %H:%M:%S %Y МСК")

SCHEDULES = {
    "09:00": {"hour": 9, "minute": 0},
    "12:00": {"hour": 12, "minute": 0},
    "15:00": {"hour": 15, "minute": 0},
    "18:00": {"hour": 18, "minute": 0},
    "21:00": {"hour": 21, "minute": 0},
}
SCHEDULE_ORDER = ["09:00", "12:00", "15:00", "18:00", "21:00"]

def get_previous_schedule_name(schedule_name):
    idx = SCHEDULE_ORDER.index(schedule_name)
    return SCHEDULE_ORDER[idx - 1]

def get_schedule_boundary(schedule_name):
    if schedule_name not in SCHEDULES:
        raise ValueError(f"Unknown schedule: {schedule_name}")
    sched = SCHEDULES[schedule_name]
    hour, minute = sched["hour"], sched["minute"]
    now_dt = now_moscow()
    today_boundary_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today_boundary_dt.timestamp()

def get_next_publish_time(schedule_name):
    if schedule_name not in SCHEDULES:
        raise ValueError(f"Unknown schedule: {schedule_name}")
    sched = SCHEDULES[schedule_name]
    hour, minute = sched["hour"], sched["minute"]
    now_dt = now_moscow()
    target_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_dt > target_dt:
        seconds_late = (now_dt - target_dt).total_seconds()
        if seconds_late > 1800:
            return now_dt.timestamp()
    return target_dt.timestamp()

def wait_until_publish_time(schedule_name):
    target_ts = get_next_publish_time(schedule_name)
    now_ts = time.time()
    diff = target_ts - now_ts
    if diff > 300:
        print(f"⚠️  Запуск слишком рано (до цели {int(diff)}с). Публикуем немедленно (cron-job должен обеспечивать точность).")
    elif diff > 0:
        print(f"⏳ Ждём {int(diff)}с до целевого времени...")
        time.sleep(diff)
    else:
        print("⏱️  Целевое время уже наступило, публикуем немедленно.")

def load_prompt_template():
    global PROMPT_TEMPLATE
    if PROMPT_TEMPLATE is not None:
        return PROMPT_TEMPLATE
    try:
        with open(PROMPT_TEMPLATE_FILE, "r", encoding="utf-8") as f:
            PROMPT_TEMPLATE = f.read()
    except Exception:
        PROMPT_TEMPLATE = """Проанализируй следующие новости и дай структурированный JSON анализ.
Верни ТОЛЬКО валидный JSON.
Категории: geopolitics, economics, business, technology, energy, security, pr.
Для каждой новости укажи is_russia (true/false).
__COMPANY_FILTER__
__RECENT_CONTEXT__
Входящие новости:
__INPUT_DATA__"""
    return PROMPT_TEMPLATE

TITLE_SIMILARITY_THRESHOLD_SAME_RUN = CONFIG["dedup"]["title_similarity_threshold_same_run"]
TITLE_SIMILARITY_THRESHOLD_CROSS_RUN = CONFIG["dedup"]["title_similarity_threshold_cross_run"]
RECENT_TITLES_WINDOW_HOURS = CONFIG["dedup"]["recent_titles_window_hours"]
RECENT_PROMPT_WINDOW_SECONDS = CONFIG["dedup"]["recent_prompt_window_hours"] * 3600
RECENT_PROMPT_MAX_ITEMS = CONFIG["dedup"]["recent_prompt_max_items"]
MAX_ITEMS_PER_CATEGORY_PER_REGION = CONFIG["digest"]["max_items_per_category_per_region"]
MAX_FETCH_WORKERS = CONFIG["collection"]["max_fetch_workers"]

_TITLE_STOPWORDS = {
    "и", "в", "во", "не", "на", "с", "со", "по", "для", "из", "от", "до",
    "за", "к", "ко", "о", "об", "у", "а", "но", "или", "что", "это", "как",
    "его", "её", "их", "он", "она", "они", "мы", "вы", "будет", "было",
    "были", "есть", "чем", "также", "уже", "после", "при", "же", "то",
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "is", "are", "was", "were", "be", "been", "as",
    "it", "its", "this", "that", "will", "has", "have", "had",
}

def _normalize_title(title):
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _prep_title(title):
    norm = _normalize_title(title)
    words = {w for w in norm.split() if (len(w) > 2 or w.isdigit()) and w not in _TITLE_STOPWORDS}
    return norm, words

def _similarity_prepped(prep_a, prep_b):
    norm_a, words_a = prep_a
    norm_b, words_b = prep_b
    if not norm_a or not norm_b:
        return 0.0
    seq_ratio = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
    if words_a and words_b:
        jaccard = len(words_a & words_b) / len(words_a | words_b)
    else:
        jaccard = 0.0
    return max(seq_ratio, jaccard)

def _find_similar_title(prep, pool, threshold):
    for other_title, other_prep in pool:
        if _similarity_prepped(prep, other_prep) >= threshold:
            return other_title
    return None

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

def load_recent_titles():
    if os.path.exists(RECENT_TITLES_FILE):
        try:
            with open(RECENT_TITLES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка чтения файла недавних заголовков: {e}")
    return []

def save_recent_titles(titles_list):
    current_time = time.time()
    cutoff = RECENT_TITLES_WINDOW_HOURS * 3600
    cleaned = [
        item for item in titles_list
        if (current_time - item.get("added_at", 0)) <= cutoff
    ]
    try:
        with open(RECENT_TITLES_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения файла недавних заголовков: {e}")

def save_pending_digest(html):
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print("💾 Сохранён неотправленный дайджест для повторной попытки.")
    except Exception as e:
        print(f"⚠️  Не удалось сохранить pending digest: {e}")

def load_pending_digest():
    if not os.path.exists(PENDING_FILE):
        return None
    try:
        mtime = os.path.getmtime(PENDING_FILE)
        if time.time() - mtime > 7200:
            os.remove(PENDING_FILE)
            return None
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None

def clear_pending_digest():
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)

_FALLBACK_FEED_CANONICAL_NAMES = {
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
    "capital+economics": "Capital Economics",
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
    "economist.com": "The Economist",
    "dealbreaker.com": "Dealbreaker",
    "minchenko": "Minchenko Consulting",
    "finmarket.ru": "Финмаркет",
    "cnews.ru": "CNews",
    "tadviser.ru": "TAdviser",
    "energyland.info": "EnergyLand.info",
    "asia.nikkei.com": "Nikkei Asia",
    "readthediff.com": "The Diff",
    "politico.eu": "Politico Europe",
    "vedomosti.ru": "Ведомости",
    "vc.ru": "VC.ru",
    "scmp.com": "South China Morning Post",
    "tass.com": "ТАСС",
    "semafor.com": "Semafor",
    "bruegel.org": "Bruegel",
    "provokemedia.com": "PRovoke Media",
    "feedburner.com/PrweekUsNews": "PRWeek",
    "prdaily.com": "PR Daily",
    "cipr.co.uk": "CIPR",
    "odwyerpr.com": "O'Dwyer's PR",
    "sostav.ru": "Sostav.ru",
    "raso.ru": "РАСО",
    "akospr.ru": "АКОС",
    "adweek.com": "Adweek",
    "thedrum.com": "The Drum",
    "adindex.ru": "AdIndex",
    "cossa.ru": "Cossa",
}

_FALLBACK_RSS_FEEDS = [
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
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
    "https://www.ecb.europa.eu/rss/press.html",
    "https://ec.europa.eu/commission/presscorner/api/rss",
    "http://en.kremlin.ru/events/president/news/feed",
    "http://www.cbr.ru/rss/RssPress",
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "https://www.chathamhouse.org/path/whatsnew.xml",
    "https://www.whitehouse.gov/news/feed/",
    "https://rss.politico.com/playbook.xml",
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
    "https://news.google.com/rss/search?q=site:csis.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:cfr.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:rand.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:iiss.org&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:valdaiclub.com&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:csr.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:cepr.org+voxeu&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:ourworldindata.org&hl=en-US&gl=US&ceid=US:en",
    "https://government.ru/en/all/rss",
    "https://news.google.com/rss/search?q=site:duma.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:mid.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:minfin.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:rosstat.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:economist.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:theinformation.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22Money+Stuff%22+Bloomberg&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:axios.com+Markets&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:gzeromedia.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:econs.online&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:dealbreaker.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=%22Minchenko+Consulting%22&hl=ru&gl=RU&ceid=RU:ru",
    "http://www.finmarket.ru/rss/mainnews.asp",
    "https://www.cnews.ru/inc/rss/news.xml",
    "https://news.google.com/rss/search?q=site:tadviser.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:tadviser.ru+аналитика&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:energyland.info&hl=ru&gl=RU&ceid=RU:ru",
    "https://asia.nikkei.com/rss/feed/nar",
    "https://www.readthediff.com/feed",
    "https://www.politico.eu/feed",
    "https://www.vedomosti.ru/rss/news",
    "https://vc.ru/rss/all",
    "https://www.scmp.com/rss/91/feed",
    "http://tass.com/rss/v2.xml",
    "https://semafor.com/rss.xml",
    "https://news.google.com/rss/search?q=site:bruegel.org&hl=en-US&gl=US&ceid=US:en",
    "https://www.provokemedia.com/newsfeed/provoke-media-latest",
    "http://feeds.feedburner.com/PrweekUsNews",
    "https://www.prdaily.com/feed",
    "https://newsroom.cipr.co.uk/feed/en",
    "https://news.google.com/rss/search?q=site:odwyerpr.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:sostav.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:raso.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:akospr.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://www.adweek.com/feed/",
    "https://news.google.com/rss/search?q=site:thedrum.com&hl=en-US&gl=US&ceid=US:en",
    "https://adindex.ru/news/news.rss",
    "https://news.google.com/rss/search?q=site:cossa.ru&hl=ru&gl=RU&ceid=RU:ru"
]

_FALLBACK_TELEGRAM_CHANNELS = [
    {"username": "the_financial_times_journal", "source_name": "Financial Times"},
    {"username": "thebell_io", "source_name": "The Bell"},
    {"username": "frank_media", "source_name": "Frank Media"},
    {"username": "neftegazchannel", "source_name": "Neftegaz.RU"},
    {"username": "rerussia_org", "source_name": "Re:Russia"},
    {"username": "istories_media", "source_name": "Важные истории"},
]

def load_sources():
    rss_feeds = CONFIG.get("rss_feeds") or _FALLBACK_RSS_FEEDS
    tg_channels = CONFIG.get("telegram_channels") or _FALLBACK_TELEGRAM_CHANNELS
    feed_canonical = CONFIG.get("feed_canonical_names") or _FALLBACK_FEED_CANONICAL_NAMES

    if not CONFIG.get("ignore_stale", False):
        stale_urls = []
        if os.path.exists(STALE_SOURCES_FILE):
            try:
                with open(STALE_SOURCES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    stale_urls = data.get("stale_urls", [])
            except Exception as e:
                print(f"⚠️  Ошибка чтения stale_sources.json: {e}")
        if stale_urls:
            rss_feeds = [url for url in rss_feeds if url not in stale_urls]
            print(f"🧹 Исключено {len(stale_urls)} мёртвых источников согласно stale_sources.json")
    return rss_feeds, tg_channels, feed_canonical

RSS_FEEDS, TELEGRAM_CHANNELS, FEED_CANONICAL_NAMES = load_sources()

def get_source_name(url):
    for domain, name in FEED_CANONICAL_NAMES.items():
        if domain.lower() in url.lower():
            return name
    return url.split("//")[1].split("/")[0] if "//" in url else url

def parse_published_time(published_str):
    if not published_str or not isinstance(published_str, str):
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(published_str)
        return dt.timestamp()
    except Exception:
        pass
    try:
        iso_str = published_str.replace('Z', '+00:00')
        dt = datetime.datetime.fromisoformat(iso_str)
        return dt.timestamp()
    except Exception:
        pass
    return None

async def fetch_feed_async(url, session, timeout=None):
    if timeout is None:
        timeout = CONFIG.get("fetch", {}).get("rss_timeout_seconds", 25)
    
    cache = get_cache()
    key = f"rss:{url}"
    cached = cache.get(key)
    if cached is not None and time.time() - cached["timestamp"] < 600:
        return cached["data"]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8"
    }
    if "sec.gov" in url:
        headers["User-Agent"] = "NewsDigestBot your_email@example.com"
    
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), headers=headers) as resp:
            content = await resp.text()
            parsed = feedparser.parse(content)
            cache.set(key, {"timestamp": time.time(), "data": parsed})
            return parsed
    except asyncio.TimeoutError:
        print(f"Timeout при получении {url}")
        return None
    except aiohttp.ClientError as e:
        # ClientError включает ошибки соединения, закрытые сокеты и т.п.
        print(f"ClientError при запросе {url}: {e}")
        return None
    except UnicodeDecodeError as e:
        print(f"Ошибка декодирования {url}: {e}")
        return None
    except Exception as e:
        print(f"Ошибка при запросе {url}: {e}")
        return None

async def fetch_telegram_channel_async(username, session, timeout=None):
    if timeout is None:
        timeout = CONFIG.get("fetch", {}).get("telegram_timeout_seconds", 20)
    
    url = f"https://t.me/s/{username}"
    cache = get_cache()
    key = f"tg:{username}"
    cached = cache.get(key)
    if cached is not None and time.time() - cached["timestamp"] < 600:
        return cached["data"]
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), headers=headers) as resp:
            html = await resp.text()
            soup = BeautifulSoup(html, "html.parser")
            messages = []
            for msg_div in soup.select("div.tgme_widget_message"):
                post_id = msg_div.get("data-post", "")
                if not post_id:
                    continue
                text_div = msg_div.select_one(".tgme_widget_message_text")
                if not text_div:
                    continue
                bold_tag = text_div.find("b")
                italic_tag = text_div.find("i")
                headline = bold_tag.get_text(strip=True) if bold_tag else ""
                summary_part = italic_tag.get_text(strip=True) if italic_tag else ""
                if headline:
                    title = f"{headline}. {summary_part}".strip() if summary_part else headline
                else:
                    title = text_div.get_text(separator=" ", strip=True)
                if not title:
                    continue
                time_tag = msg_div.select_one(".tgme_widget_message_date time")
                published = time_tag.get("datetime", "") if time_tag else ""
                link = f"https://t.me/{post_id}"
                messages.append({"title": title, "link": link, "published": published})
            cache.set(key, {"timestamp": time.time(), "data": messages})
            return messages
    except asyncio.TimeoutError:
        print(f"Timeout при получении Telegram-канала {username}")
        return []
    except aiohttp.ClientError as e:
        print(f"ClientError при запросе Telegram-канала {username}: {e}")
        return []
    except Exception as e:
        print(f"Не удалось получить Telegram-канал {username}: {e}")
        return []

def clean_rss_summary(raw_summary, max_len=400):
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

async def collect_all_news(sent_urls_history, recent_titles_history, schedule_name="09:00"):
    news_db = {}
    raw_data_list = []
    
    current_boundary = get_schedule_boundary(schedule_name)
    previous_schedule = get_previous_schedule_name(schedule_name)
    last_boundary = get_last_run_time(previous_schedule)
    
    if last_boundary is None:
        last_boundary = current_boundary - 24 * 3600
        print(f"⚠️  Расписание '{previous_schedule}' ещё не запускалось, установлено окно в 24 часа назад")
    elif last_boundary >= current_boundary:
        print(f"⚠️  АНОМАЛИЯ: граница расписания '{previous_schedule}' ({fmt_msk(last_boundary)}) "
              f">= текущей границы {schedule_name} ({fmt_msk(current_boundary)}). Откатываемся на 24ч.")
        last_boundary = current_boundary - 24 * 3600
    
    print(f"📅 {schedule_name.upper()} дайджест: собираем новости с {fmt_msk(last_boundary)} до {fmt_msk(current_boundary)}")
    print(f"📡 Начинаем парсинг {len(RSS_FEEDS)} лент (асинхронно, до {MAX_FETCH_WORKERS} одновременно)...")
    
    stats = {"raw_seen": 0, "skip_sent": 0, "skip_window": 0, "skip_no_title": 0, "skip_similar_title": 0, "included": 0}
    accepted_title_pool = []
    recent_title_pool = [
        (item["title"], _prep_title(item["title"]))
        for item in recent_titles_history if item.get("title")
    ]
    
    rss_timeout = CONFIG.get("fetch", {}).get("rss_timeout_seconds", 25)
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(MAX_FETCH_WORKERS)
        async def fetch_with_semaphore(url):
            async with sem:
                return await fetch_feed_async(url, session, rss_timeout)
        
        tasks = [fetch_with_semaphore(url) for url in RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for idx, parsed in enumerate(results):
            feed_url = RSS_FEEDS[idx]
            if isinstance(parsed, Exception):
                print(f"⚠️  Ошибка при получении {feed_url}: {parsed}")
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
                published_str = entry.get("published", "")
                published_ts = parse_published_time(published_str)
                if published_ts is not None:
                    if published_ts < last_boundary or published_ts >= current_boundary:
                        stats["skip_window"] += 1
                        continue
                title = entry.get("title", "").strip()
                if not title:
                    stats["skip_no_title"] += 1
                    continue
                this_title_prep = _prep_title(title)
                dup_match = (
                    _find_similar_title(this_title_prep, accepted_title_pool, TITLE_SIMILARITY_THRESHOLD_SAME_RUN)
                    or _find_similar_title(this_title_prep, recent_title_pool, TITLE_SIMILARITY_THRESHOLD_CROSS_RUN)
                )
                if dup_match:
                    stats["skip_similar_title"] += 1
                    print(f"   🔁 Похоже на уже включённое/опубликованное, пропущено: \"{title[:70]}\" ≈ \"{dup_match[:70]}\"")
                    continue
                accepted_title_pool.append((title, this_title_prep))
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
    
    print(f"✅ Собрано {len(news_db)} новостей из {len(RSS_FEEDS)} RSS-источников")
    print(f"   📊 Разбивка RSS: всего просмотрено {stats['raw_seen']}, "
          f"уже отправлялось ранее {stats['skip_sent']}, вне окна времени {stats['skip_window']}, "
          f"без заголовка {stats['skip_no_title']}, похоже на уже опубликованное {stats['skip_similar_title']}, "
          f"включено {stats['included']}")
    
    tg_collected = 0
    tg_stats = {"raw_seen": 0, "skip_sent": 0, "skip_window": 0, "skip_similar_title": 0}
    if TELEGRAM_CHANNELS:
        print(f"📡 Начинаем парсинг {len(TELEGRAM_CHANNELS)} Telegram-каналов (асинхронно)...")
        tg_timeout = CONFIG.get("fetch", {}).get("telegram_timeout_seconds", 20)
        async with aiohttp.ClientSession() as session:
            sem = asyncio.Semaphore(min(MAX_FETCH_WORKERS, len(TELEGRAM_CHANNELS)))
            async def fetch_tg(channel):
                async with sem:
                    return await fetch_telegram_channel_async(channel["username"], session, tg_timeout)
            
            tasks = [fetch_tg(channel) for channel in TELEGRAM_CHANNELS]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for idx, messages in enumerate(results):
                channel = TELEGRAM_CHANNELS[idx]
                source_name = channel["source_name"]
                if isinstance(messages, Exception):
                    print(f"⚠️  Ошибка при получении канала @{channel['username']}: {messages}")
                    continue
                for msg in messages[-5:]:
                    tg_stats["raw_seen"] += 1
                    url = msg["link"]
                    if not url or url in sent_urls_history:
                        tg_stats["skip_sent"] += 1
                        continue
                    published_str = msg["published"]
                    published_ts = parse_published_time(published_str)
                    if published_ts is not None:
                        if published_ts < last_boundary or published_ts >= current_boundary:
                            tg_stats["skip_window"] += 1
                            continue
                    title = msg["title"]
                    if not title:
                        continue
                    this_title_prep = _prep_title(title)
                    dup_match = (
                        _find_similar_title(this_title_prep, accepted_title_pool, TITLE_SIMILARITY_THRESHOLD_SAME_RUN)
                        or _find_similar_title(this_title_prep, recent_title_pool, TITLE_SIMILARITY_THRESHOLD_CROSS_RUN)
                    )
                    if dup_match:
                        tg_stats["skip_similar_title"] += 1
                        print(f"   🔁 Похоже на уже включённое/опубликованное (Telegram), пропущено: \"{title[:70]}\" ≈ \"{dup_match[:70]}\"")
                        continue
                    accepted_title_pool.append((title, this_title_prep))
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
              f"похоже на уже опубликованное {tg_stats['skip_similar_title']}, включено {tg_collected}")
    
    now_ts = time.time()
    recently_published_for_prompt = [
        item["title"] for item in recent_titles_history
        if item.get("title") and (now_ts - item.get("added_at", 0)) <= RECENT_PROMPT_WINDOW_SECONDS
    ][:RECENT_PROMPT_MAX_ITEMS]
    
    raw_data_prompt = "\n".join(raw_data_list)
    collection_stats = {
        "rss_feeds_total": len(RSS_FEEDS),
        "rss": dict(stats),
        "telegram_channels_total": len(TELEGRAM_CHANNELS),
        "telegram": dict(tg_stats),
        "telegram_included": tg_collected,
    }
    
    return news_db, raw_data_prompt, recently_published_for_prompt, collection_stats

def generate_analytical_json(raw_data_prompt, recently_published_titles=None):
    template = load_prompt_template()
    
    recently_published_titles = recently_published_titles or []
    if recently_published_titles:
        recent_block = (
            "\n    СПРАВОЧНО — эти заголовки уже были опубликованы в недавних "
            "дайджестах (последние ~12 часов), читатель их уже видел. Если "
            "среди входящих новостей ниже есть такая, что по сути описывает "
            "ТО ЖЕ САМОЕ событие, что и один из этих заголовков — даже другим "
            "источником, другими словами или на другом языке — НЕ включай её "
            "снова. Включай повторно ТОЛЬКО если новость содержит существенно "
            "НОВУЮ фактуру:\n"
            + "\n".join(f"    - {t}" for t in recently_published_titles)
            + "\n"
        )
    else:
        recent_block = ""
    
    company_filter = ""
    cs = CONFIG.get("company_significance", {})
    if cs.get("global_major"):
        company_filter += "Крупные мировые компании (по капитализации/выручке): " + ", ".join(cs["global_major"]) + ".\n"
    if cs.get("global_well_known"):
        company_filter += "Широко узнаваемые компании (не обязательно в топе): " + ", ".join(cs["global_well_known"]) + ".\n"
    if cs.get("russian_major"):
        company_filter += "Российские компании-лидеры (топ отрасли в РФ): " + ", ".join(cs["russian_major"]) + ".\n"
    if company_filter:
        company_filter = "ФИЛЬТР ПО ЗНАЧИМОСТИ КОМПАНИЙ:\n" + company_filter + "\nДля business/technology новости о компаниях включай, только если компания в одном из этих списков (кроме случаев, когда само событие значимо для отрасли). Для pr это правило НЕ применяется.\n"
    
    prompt = template.replace("__RECENT_CONTEXT__", recent_block)
    prompt = prompt.replace("__INPUT_DATA__", raw_data_prompt)
    prompt = prompt.replace("__MAX_ITEMS__", str(MAX_ITEMS_PER_CATEGORY_PER_REGION))
    prompt = prompt.replace("__COMPANY_FILTER__", company_filter)
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={gemini_api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 65536,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "minimal"}
        }
    }
    
    gemini_call_start = time.time()
    max_retries = 7  # увеличено с 5 до 7
    attempts_made = 0
    gemini_meta = {
        "latency_sec": None,
        "attempts_used": 0,
        "fallback_used": False,
        "error_type": None,
        "error_detail": None,
    }
    
    for attempt in range(max_retries):
        attempts_made = attempt + 1
        try:
            response = requests.post(url, json=payload, timeout=280)
            
            if response.status_code == 400:
                try:
                    error_data = response.json()
                    error_message = error_data.get("error", {}).get("message", "")
                    if "safety" in error_message.lower() or "blocked" in error_message.lower():
                        print(f"⛔ Gemini заблокировал запрос по соображениям безопасности: {error_message[:200]}")
                        gemini_meta["error_type"] = "safety_block"
                        gemini_meta["error_detail"] = error_message
                        break
                    else:
                        print(f"❌ Некорректный запрос к Gemini (400): {response.text[:200]}")
                        gemini_meta["error_type"] = "invalid_request"
                        gemini_meta["error_detail"] = error_message
                        break
                except:
                    print(f"❌ Некорректный запрос к Gemini (400): {response.text[:200]}")
                    gemini_meta["error_type"] = "invalid_request"
                    break
            
            if response.status_code == 429:
                try:
                    data = response.json()
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    wait_time = min(retry_after, 60)
                except:
                    wait_time = 10
                print(f"⏸️  Rate limit 429. Ждём {wait_time}с (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            
            if response.status_code == 503:
                # увеличена максимальная задержка до 120 секунд
                wait_time = min(15 * (2 ** attempt), 120)
                print(f"⏸️  API перегружена (503). Ждем {wait_time}с (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            
            if response.status_code >= 500:
                wait_time = min(10 * (2 ** attempt), 90)
                print(f"⚠️  Ошибка сервера ({response.status_code}). Ждем {wait_time}с...")
                time.sleep(wait_time)
                continue
            
            if response.status_code == 200:
                result = response.json()
                gemini_meta["latency_sec"] = round(time.time() - gemini_call_start, 1)
                gemini_meta["attempts_used"] = attempts_made
                gemini_meta["fallback_used"] = False
                return result["candidates"][0]["content"]["parts"][0]["text"], gemini_meta
            
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
    
    print("❌ Gemini API недоступна после всех попыток. Используем fallback.")
    gemini_meta["latency_sec"] = round(time.time() - gemini_call_start, 1)
    gemini_meta["attempts_used"] = attempts_made
    gemini_meta["fallback_used"] = True
    gemini_meta["error_type"] = "max_retries_exceeded"
    # Возвращаем пустой JSON, build_html_digest построит fallback из сырых новостей
    return json.dumps({
        "geopolitics": [],
        "economics": [],
        "business": [],
        "technology": [],
        "energy": [],
        "security": [],
        "pr": []
    }), gemini_meta

def postprocess_pr_classification(data, news_db):
    pr_domains = CONFIG.get("pr_source_domains", [])
    if not pr_domains:
        return data
    pr_items = []
    for category in list(data.keys()):
        if category == "pr":
            continue
        items = data.get(category, [])
        kept = []
        for item in items:
            news_id = str(item.get("id", ""))
            if news_id in news_db:
                url = news_db[news_id]["url"]
                domain = url.split("//")[1].split("/")[0].lower() if "//" in url else ""
                if any(pr_domain in domain for pr_domain in pr_domains):
                    pr_items.append(item)
                else:
                    kept.append(item)
            else:
                kept.append(item)
        data[category] = kept
    if pr_items:
        if "pr" not in data:
            data["pr"] = []
        data["pr"].extend(pr_items)
    return data

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
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s*[\(\[\{][^\)\]\}]*(RSS|Feed|News|Новости|Лента)[^\)\]\}]*[\)\]\}]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\([^\)]*\)\s*$', '', text)
    return text.strip(' .-_')

LOCAL_POLITICS_KEYWORDS = [
    "муниципал", "депутат", "областной", "районный",
    "администрац", "мэр", "губернатор", "чиновник"
]
LOCAL_CRIME_AND_TRIVIA_KEYWORDS = [
    "убил жену", "убил мужа", "убил дочь", "убил сына", "убил родствен",
    "застрелил", "зарезал", "ДТП", "сбил насмерть", "поджог дома",
    "бытовое убийство", "семейная ссора закончилась",
]

CATEGORY_LABELS = {
    "geopolitics": "Геополитика",
    "economics": "Экономика",
    "business": "Бизнес",
    "technology": "Технологии",
    "energy": "Энергетика",
    "security": "Безопасность",
    "pr": "PR",
}

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
    
    data = postprocess_pr_classification(data, news_db)
    
    expected_categories = ["geopolitics", "economics", "business", "technology", "energy", "security", "pr"]
    total_items = sum(
        len(data.get(cat)) if isinstance(data.get(cat), list) else 0 
        for cat in expected_categories
    )
    
    # === FALLBACK: если Gemini не дала новостей, но есть собранные ===
    if total_items == 0 and news_db:
        print("⚠️  Gemini не вернула категорий, используем сырые заголовки как fallback.")
        # Создаём временную категорию "raw" для отображения
        raw_items = []
        for news_id, info in list(news_db.items())[:30]:
            # Определим is_russia грубо по наличию слова "Россия" или "росси" в заголовке
            title = info.get("title", "")
            is_russia = "Россия" in title or "росси" in title.lower()
            raw_items.append({
                "id": news_id,
                "summary_ru": title,
                "is_russia": is_russia
            })
        # Помещаем их в категорию "raw" (нестандартную, но мы её обработаем отдельно)
        data["raw"] = raw_items
        total_items = len(raw_items)
    
    if total_items == 0:
        print("⚠️  Модель не вернула ни одной новости (fallback структура)")
    
    # Разбивка по категориям/регионам (только для стандартных категорий)
    category_breakdown = {}
    for cat in expected_categories:
        items = data.get(cat)
        items = items if isinstance(items, list) else []
        world_count = sum(1 for it in items if isinstance(it, dict) and not it.get("is_russia", False))
        russia_count = sum(1 for it in items if isinstance(it, dict) and it.get("is_russia", False))
        category_breakdown[cat] = {"world": world_count, "russia": russia_count}
    # Добавим raw категорию, если есть
    if "raw" in data:
        raw_items = data["raw"]
        world_count = sum(1 for it in raw_items if isinstance(it, dict) and not it.get("is_russia", False))
        russia_count = sum(1 for it in raw_items if isinstance(it, dict) and it.get("is_russia", False))
        category_breakdown["raw"] = {"world": world_count, "russia": russia_count}
    
    digest_stats = {"total_items": total_items, "by_category": category_breakdown}
    
    MAIN_SECTIONS = [
        ("geopolitics", "🌍 ГЕОПОЛИТИКА И МАКРО-РЕШЕНИЯ"),
        ("economics", "📈 ЭКОНОМИКА И ИНСТИТУТЫ"),
        ("business", "💼 БИЗНЕС И M&A"),
        ("technology", "🧬 ТЕХНОЛОГИИ И ИННОВАЦИИ"),
        ("energy", "⚡ ЭНЕРГЕТИКА И РЕСУРСЫ"),
        ("security", "🪖 БЕЗОПАСНОСТЬ И КОНФЛИКТЫ")
    ]
    PR_SECTIONS = [("pr", "📢 PR И КОММУНИКАЦИИ")]
    # Добавим RAW секцию, если есть
    RAW_SECTIONS = [("raw", "📰 СЫРЫЕ НОВОСТИ (Gemini недоступна)")] if "raw" in data else []
    
    seen_urls_in_digest = set()
    
    def build_one(target_is_russia, header, sections_to_render, force_show=False):
        html_output = f"{header}\n\n"
        any_valid_anywhere = False
        section_urls = []
        for key, title in sections_to_render:
            raw_items = data.get(key)
            items = raw_items if isinstance(raw_items, list) else []
            filtered_items = [it for it in items if isinstance(it, dict) and bool(it.get("is_russia")) == target_is_russia]
            if len(sections_to_render) > 1:
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
        show_block = any_valid_anywhere or force_show
        result_html = html_output.strip() if show_block else ""
        return result_html, (section_urls if result_html else [])
    
    world_html, world_urls = build_one(False, "🌍 <b>МИРОВАЯ ПОВЕСТКА</b>", MAIN_SECTIONS)
    russia_html, russia_urls = build_one(True, "🇷🇺 <b>РОССИЯ</b>", MAIN_SECTIONS)
    pr_world_html, pr_world_urls = build_one(False, "📢 <b>PR В МИРЕ</b>", PR_SECTIONS, force_show=True)
    pr_russia_html, pr_russia_urls = build_one(True, "📢 <b>PR В РОССИИ</b>", PR_SECTIONS, force_show=True)
    
    # Если есть raw новости, строим блок для мира и России из них
    raw_world_html = ""
    raw_russia_html = ""
    raw_world_urls = []
    raw_russia_urls = []
    if "raw" in data:
        raw_world_html, raw_world_urls = build_one(False, "📰 <b>СЫРЫЕ НОВОСТИ (МИР)</b>", RAW_SECTIONS, force_show=True)
        raw_russia_html, raw_russia_urls = build_one(True, "📰 <b>СЫРЫЕ НОВОСТИ (РОССИЯ)</b>", RAW_SECTIONS, force_show=True)
    
    return (world_html, russia_html, pr_world_html, pr_russia_html,
            world_urls, russia_urls, pr_world_urls, pr_russia_urls,
            digest_stats, raw_world_html, raw_russia_html, raw_world_urls, raw_russia_urls)

def _send_one_chunk(chat_id, chunk):
    html_for_api = chunk.replace("\n", "<br>")
    url = f"https://api.telegram.org/bot{bot_token}/sendRichMessage"
    payload = {
        "chat_id": chat_id,
        "rich_message": {"html": html_for_api},
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=30)
            try:
                data = response.json()
            except ValueError:
                wait_time = 5 * (attempt + 1)
                print(f"⚠️  Telegram вернул нераспознаваемый ответ (HTTP {response.status_code}), попытка {attempt + 1}/{max_retries}, ждём {wait_time}с...")
                time.sleep(wait_time)
                continue
            if response.status_code == 200 and data.get("ok"):
                return True
            if response.status_code == 429:
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                wait_time = min(retry_after, 60)
                print(f"⏸️  Telegram rate limit (429). Ждём {wait_time}с (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            if response.status_code >= 500:
                wait_time = min(5 * (2 ** attempt), 30)
                print(f"⚠️  Telegram сервер вернул {response.status_code}. Ждём {wait_time}с (попытка {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
                continue
            print(f"❌ Telegram отклонил sendRichMessage (HTTP {response.status_code}): {data.get('description', data)}")
            return False
        except requests.exceptions.Timeout:
            wait_time = 5 * (attempt + 1)
            print(f"⏸️  Timeout при отправке в Telegram. Ждём {wait_time}с (попытка {attempt + 1}/{max_retries})...")
            time.sleep(wait_time)
            continue
        except requests.exceptions.RequestException as e:
            wait_time = 3 * (attempt + 1)
            print(f"❌ Сетевая ошибка при отправке sendRichMessage (попытка {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(wait_time)
            continue
        except Exception as e:
            print(f"❌ Непредвиденная ошибка отправки в Telegram: {e}")
            return False
    print(f"❌ Не удалось отправить чанк в Telegram после {max_retries} попыток")
    return False

def _pack_lines_into_chunks(text, limit=32000):
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
    if not text.strip():
        return False, 0
    if len(text) <= 32000:
        return _send_one_chunk(chat_id, text), 1
    all_chunks = _pack_lines_into_chunks(text, limit=32000)
    all_ok = True
    for i, chunk in enumerate(all_chunks):
        if i > 0:
            time.sleep(1)
        all_ok = _send_one_chunk(chat_id, chunk) and all_ok
    return all_ok, len(all_chunks)

ALERT_ON_STATUSES = set(CONFIG["alerts"]["on_statuses"])

def _count_recent_consecutive_failures():
    if not os.path.exists(METRICS_FILE):
        return 0
    try:
        with open(METRICS_FILE, encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
    except Exception:
        return 0
    count = 0
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("status") == "sent":
            break
        count += 1
    return count

def send_alert(run_metrics):
    status = run_metrics.get("status", "unknown")
    schedule = run_metrics.get("schedule", "?")
    errors = run_metrics.get("errors", [])
    icon = "🔴" if status in ("crashed", "send_failed") else "🟡"
    status_text = {
        "crashed": "необработанный сбой (crash)",
        "send_failed": "не удалось отправить в Telegram",
        "empty_no_candidates": "ни один источник не дал новостей за окно сбора",
        "empty_no_digest_items": "Gemini не отобрала ни одной новости",
    }.get(status, status)
    consecutive = _count_recent_consecutive_failures() + 1
    streak_note = f"\n⚠️ {consecutive}-й проблемный прогон подряд" if consecutive > 1 else ""
    lines = [f"{icon} <b>Дайджест {schedule}</b>: {status_text}{streak_note}"]
    if errors:
        lines.append("Детали: " + "; ".join(errors)[:400])
    text = "\n".join(lines)
    sent_via_telegram = False
    alert_chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID") or CHAT_ID
    try:
        sent_via_telegram = _send_one_chunk(alert_chat_id, text)
    except Exception as e:
        print(f"⚠️  Не удалось отправить алерт в Telegram: {e}")
    sent_via_webhook = False
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook_url:
        try:
            resp = requests.post(webhook_url, json={"text": text}, timeout=10)
            sent_via_webhook = resp.status_code < 300
            if not sent_via_webhook:
                print(f"⚠️  Webhook алертов вернул HTTP {resp.status_code}")
        except Exception as e:
            print(f"⚠️  Не удалось отправить алерт на webhook: {e}")
    if sent_via_telegram or sent_via_webhook:
        via = " + ".join(filter(None, ["Telegram" if sent_via_telegram else None, "webhook" if sent_via_webhook else None]))
        print(f"📨 Алерт отправлен ({via})")
    else:
        print("⚠️  Алерт НЕ удалось отправить ни одним способом")
    return sent_via_telegram or sent_via_webhook

def _finalize_run_metrics(run_metrics, run_start_time):
    run_metrics["duration_sec"] = round(time.time() - run_start_time, 1)
    if run_metrics.get("status") in ALERT_ON_STATUSES:
        run_metrics["alert_sent"] = send_alert(run_metrics)
    save_run_metrics(run_metrics)

def save_run_metrics(metrics):
    try:
        with open(METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(metrics, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Ошибка сохранения метрик: {e}")

def get_last_run_time(schedule_name):
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
    data = {}
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Ошибка чтения last_run при сохранении: {e}")
    boundary = get_schedule_boundary(schedule_name)
    data[schedule_name] = boundary
    try:
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"💾 Сохранено время последнего запуска {schedule_name}: {fmt_msk(boundary)}")
    except Exception as e:
        print(f"Ошибка записи last_run: {e}")

async def main_async(args, schedule_name):
    run_start_time = time.time()
    run_metrics = {
        "run_started_at": now_moscow().isoformat(),
        "schedule": schedule_name,
        "status": "unknown",
        "errors": [],
    }
    
    try:
        sent_urls_history = load_sent_urls()
        recent_titles_history = load_recent_titles()
        
        news_db = {}
        raw_data_prompt = ""
        recently_published_titles = []
        collection_stats = {}
        raw_json = None
        
        if args.skip_fetch:
            try:
                with open("last_news_db.json", "r", encoding="utf-8") as f:
                    news_db = json.load(f)
                with open("last_raw_data.txt", "r", encoding="utf-8") as f:
                    raw_data_prompt = f.read()
                with open("last_recent_titles.json", "r", encoding="utf-8") as f:
                    recently_published_titles = json.load(f)
                with open("last_collection_stats.json", "r", encoding="utf-8") as f:
                    collection_stats = json.load(f)
                print("📂 Данные загружены из кеша (--skip-fetch).")
            except Exception as e:
                print(f"⚠️  Не удалось загрузить кеш: {e}. Выполняем полный сбор.")
                args.skip_fetch = False
        
        if not args.skip_fetch:
            fetch_start = time.time()
            news_db, raw_data_prompt, recently_published_titles, collection_stats = await collect_all_news(
                sent_urls_history, recent_titles_history, schedule_name
            )
            run_metrics["fetch_duration_sec"] = round(time.time() - fetch_start, 1)
            try:
                with open("last_news_db.json", "w", encoding="utf-8") as f:
                    json.dump(news_db, f, ensure_ascii=False, indent=2)
                with open("last_raw_data.txt", "w", encoding="utf-8") as f:
                    f.write(raw_data_prompt)
                with open("last_recent_titles.json", "w", encoding="utf-8") as f:
                    json.dump(recently_published_titles, f)
                with open("last_collection_stats.json", "w", encoding="utf-8") as f:
                    json.dump(collection_stats, f)
            except Exception as e:
                print(f"⚠️  Не удалось сохранить кеш: {e}")
        
        run_metrics["collection"] = collection_stats
        
        if args.skip_analyze:
            try:
                with open("last_raw_json.json", "r", encoding="utf-8") as f:
                    raw_json = f.read()
                print("📂 Анализ загружен из кеша (--skip-analyze).")
                gemini_meta = {"latency_sec": 0, "attempts_used": 0, "fallback_used": False}
                run_metrics["gemini"] = gemini_meta
                run_metrics["gemini_total_duration_sec"] = 0
            except Exception as e:
                print(f"⚠️  Не удалось загрузить кеш анализа: {e}. Выполняем анализ.")
                args.skip_analyze = False
        
        if raw_data_prompt.strip() and not args.skip_analyze:
            print("📊 Запрашиваем анализ из Gemini API...")
            gemini_start = time.time()
            raw_json, gemini_meta = generate_analytical_json(raw_data_prompt, recently_published_titles)
            run_metrics["gemini_total_duration_sec"] = round(time.time() - gemini_start, 1)
            run_metrics["gemini"] = gemini_meta
            if gemini_meta.get("fallback_used"):
                run_metrics["errors"].append("Gemini недоступна после всех попыток — использован пустой fallback")
            if gemini_meta.get("error_type"):
                run_metrics["errors"].append(f"Gemini error: {gemini_meta['error_type']} - {gemini_meta.get('error_detail', '')}")
            try:
                with open("last_raw_json.json", "w", encoding="utf-8") as f:
                    f.write(raw_json)
            except Exception as e:
                print(f"⚠️  Не удалось сохранить кеш анализа: {e}")
        
        if raw_json and raw_data_prompt.strip():
            postprocess_start = time.time()
            (world_html, russia_html, pr_world_html, pr_russia_html,
             world_urls, russia_urls, pr_world_urls, pr_russia_urls,
             digest_stats, raw_world_html, raw_russia_html, raw_world_urls, raw_russia_urls) = build_html_digest(raw_json, news_db)
            run_metrics["postprocess_duration_sec"] = round(time.time() - postprocess_start, 1)
            run_metrics["digest"] = digest_stats
            
            wait_until_publish_time(schedule_name)
            
            sent_anything = False
            confirmed_urls = []
            combined_parts = [html for html in (world_html, russia_html, pr_world_html, pr_russia_html, raw_world_html, raw_russia_html) if html.strip()]
            combined_html = "\n\n".join(combined_parts)
            combined_urls = world_urls + russia_urls + pr_world_urls + pr_russia_urls + raw_world_urls + raw_russia_urls
            
            if combined_html.strip():
                print("📤 Отправляем дайджест (мир + Россия + PR)...")
                send_start = time.time()
                send_ok, chunks_sent = send_telegram_message(CHAT_ID, combined_html)
                run_metrics["send_duration_sec"] = round(time.time() - send_start, 1)
                run_metrics["telegram"] = {"success": send_ok, "chunks_sent": chunks_sent}
                if send_ok:
                    sent_anything = True
                    confirmed_urls.extend(combined_urls)
                    clear_pending_digest()
                else:
                    print("❌ Не удалось отправить дайджест — сохраняем для повторной попытки.")
                    save_pending_digest(combined_html)
                    run_metrics["errors"].append("Отправка в Telegram не удалась")
            else:
                run_metrics["status"] = "empty_no_digest_items"
            
            if sent_anything:
                now = time.time()
                for url in confirmed_urls:
                    sent_urls_history[url] = now
                save_sent_urls(sent_urls_history)
                
                url_meta = {v["url"]: {"title": v["title"], "source_name": v["source_name"]} for v in news_db.values()}
                for url in confirmed_urls:
                    meta = url_meta.get(url)
                    if meta:
                        recent_titles_history.append({"title": meta["title"], "added_at": now})
                save_recent_titles(recent_titles_history)
                run_metrics["sources_used"] = sorted({
                    url_meta[u]["source_name"] for u in confirmed_urls if u in url_meta
                })
                
                save_run_time(schedule_name)
                run_metrics["status"] = "sent"
                print("✅ Диджест отправлен успешно!")
            else:
                if run_metrics["status"] == "unknown":
                    run_metrics["status"] = "send_failed"
                print("ℹ️  Новостей для публикации не найдено, либо отправка не удалась.")
        else:
            if not raw_data_prompt.strip():
                run_metrics["status"] = "empty_no_candidates"
                print("ℹ️  Новых материалов за прошедшие часы не обнаружено.")
    
    except Exception as e:
        run_metrics["status"] = "crashed"
        run_metrics["errors"].append(f"{type(e).__name__}: {str(e)[:300]}")
        _finalize_run_metrics(run_metrics, run_start_time)
        raise
    
    _finalize_run_metrics(run_metrics, run_start_time)

if __name__ == "__main__":
    _require_runtime_env_vars()
    parser = argparse.ArgumentParser()
    parser.add_argument("schedule", nargs="?", default="09:00")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-analyze", action="store_true")
    args = parser.parse_args()
    schedule_name = args.schedule if args.schedule in SCHEDULES else "09:00"
    print(f"🕐 Запущен дайджест: {schedule_name}")
    
    pending_html = load_pending_digest()
    if pending_html:
        print("📤 Обнаружен неотправленный дайджест. Пытаемся отправить повторно...")
        send_ok, chunks = send_telegram_message(CHAT_ID, pending_html)
        if send_ok:
            print("✅ Повторная отправка успешна.")
            clear_pending_digest()
            sys.exit(0)
        else:
            print("❌ Повторная отправка не удалась. Оставляем для следующей попытки.")
    
    asyncio.run(main_async(args, schedule_name))