print("=== ЗАПУСК СКРИПТА ВЕРСИИ 6.11 (FIXED_MODEL_HISTORY_AND_TELEGRAM_SAFETY) ===")

import os
import re
import json
import time
import requests
import feedparser
from html import escape as html_escape
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
    "ourworldindata.org": "Our World in Data"
}

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en",
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.ft.com/world?format=rss",
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
    "https://expert.ru/rss/all/",
    "https://www.csis.org/analysis/rss.xml",
    "https://www.chathamhouse.org/rss/all",
    "https://www.cfr.org/publications/rss.xml",
    "https://www.rand.org/pubs/recent.xml",
    "https://www.iiss.org/rss/",
    "https://carnegieendowment.org/rss/publications/",
    "https://www.atlanticcouncil.org/feed/",
    "https://www.brookings.edu/feed/",
    "https://www.crisisgroup.org/rss.xml",
    "https://russiancouncil.ru/rss/",
    "https://news.google.com/rss/search?q=site:imemo.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:veb.ru+институт&hl=ru&gl=RU&ceid=RU:ru",
    "https://www.csr.ru/rss/",
    "https://news.google.com/rss/search?q=site:forecast.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://www.imf.org/en/News/rss?language=eng",
    "https://www.bis.org/doclist/all_rss.xml",
    "https://www.worldbank.org/en/news/all.rss",
    "https://www.oecd.org/newsroom/rss.xml",
    "https://www.wto.org/english/news_e/news_e.rss",
    "https://news.google.com/rss/search?q=site:fred.stlouisfed.org&hl=en-US&gl=US&ceid=US:en",
    "https://www.project-syndicate.org/rss",
    "https://cepr.org/rss/all-columns",
    "https://news.google.com/rss/search?q=%22Capital+Economics%22&hl=en-US&gl=US&ceid=US:en",
    "https://www.mckinsey.com/insights/rss",
    "https://news.google.com/rss/search?q=site:bcg.com&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:bain.com+insights&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=site:deloitte.com+M%26A+trends&hl=en-US&gl=US&ceid=US:en",
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
    "https://www.nato.int/rss/news.xml",
    "https://www.whitehouse.gov/feed/",
    "https://www.state.gov/feed/",
    "https://home.treasury.gov/rss/press-releases",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://news.google.com/rss/search?q=site:congress.gov&hl=en-US&gl=US&ceid=US:en",
    "https://ec.europa.eu/commission/presscorner/api/rss",
    "https://www.ecb.europa.eu/rss/press.xml",
    "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
]

LOCAL_POLITICS_KEYWORDS = [
    "муниципал", "выбор", "депутат", "областной", "районный", "край",
    "администрац", "мэр", "губернатор", "чиновник", "снять", "уволен",
    "назначен", "отставка", "главный", "голосов"
]

def fetch_feed(url, timeout=15):
    try:
        response = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        response.raise_for_status()
        return feedparser.parse(response.content)
    except requests.exceptions.Timeout:
        print(f"Timeout при получении {url}")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"Ошибка парсинга RSS {url}: {e}")
        return None
    except Exception as e:
        print(f"Пустая/битая лента {url}: {e}")
        return None

def get_source_name(url):
    for domain, name in FEED_CANONICAL_NAMES.items():
        if domain.lower() in url.lower():
            return name
    return url.split("//")[1].split("/")[0] if "//" in url else url

def collect_all_news(sent_urls_history):
    news_db = {}
    raw_data_list = []
    
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
            
            title = entry.get("title", "").strip()
            if not title:
                continue
            
            news_id = str(len(news_db))
            news_db[news_id] = {
                "url": url,
                "title": title,
                "source_name": source_name,
                "published": entry.get("published", "")
            }
            
            raw_data_list.append(f"ID:{news_id}|Title:{title}|Source:{source_name}")
            # ИСПРАВЛЕНО: URL больше НЕ добавляется в историю здесь.
            # Раньше новость считалась "отправленной" уже на этапе сбора из RSS,
            # то есть до того, как она реально прошла через Gemini и ушла в Telegram.
            # Если Gemini или Telegram падали, новость терялась на 7 дней, хотя
            # фактически никуда не отправлялась. Теперь запись в историю происходит
            # только после подтверждённой отправки в Telegram (см. build_html_digest
            # и блок __main__).
    
    print(f"✅ Собрано {len(news_db)} новостей из {len(RSS_FEEDS)} источников")
    
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
    sent_urls_history = load_sent_urls()

    news_db, raw_data_prompt = collect_all_news(sent_urls_history)

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
            print("✅ Диджест отправлен успешно!")
        else:
            print("ℹ️  Новостей для публикации не найдено, либо отправка не удалась — история не обновлена.")
    else:
        print("ℹ️  Новых материалов за прошедшие часы не обнаружено.")
