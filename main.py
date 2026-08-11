print("=== ЗАПУСК СКРИПТА ВЕРСИИ 6.6 (CLEAN_THEMATIC_CATEGORIES) ===")

import os
import re
import json
import time
import requests
import feedparser
import random
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

def load_sent_urls():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Ошибка чтения файла истории: {e}")
    return set()

def save_sent_urls(sent_set):
    urls_list = list(sent_set)[-5000:]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(urls_list, f, ensure_ascii=False, indent=2)
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
    "https://www.cfr.org/rss.xml",
    "https://www.rand.org/pubs/recent.xml",
    "https://www.iiss.org/rss/",
    "https://carnegieendowment.org/rss/solr/?fa=pubs",
    "https://www.atlanticcouncil.org/feed/",
    "https://www.brookings.edu/feed/",
    "https://www.crisisgroup.org/rss.xml",
    "https://russiancouncil.ru/rss/",
    "https://ru.valdaiclub.com/rss/",
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
    "https://www.theinformation.com/feed",
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
    "http://kremlin.ru/events/all/feed",
    "http://government.ru/all/rss/",
    "https://news.google.com/rss/search?q=site:duma.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://www.mid.ru/ru/rss/",
    "http://www.cbr.ru/rss/RssNews",
    "https://news.google.com/rss/search?q=site:minfin.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://news.google.com/rss/search?q=site:rosstat.gov.ru&hl=ru&gl=RU&ceid=RU:ru",
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "https://news.google.com/rss/search?q=%22Money+Stuff%22+Matt+Levine&hl=en-US&gl=US&ceid=US:en",
    "https://api.axios.com/feed/markets.rss",
    "https://news.google.com/rss/search?q=%22FirstFT%22&hl=en-US&gl=US&ceid=US:en",
    "https://www.politico.com/rss/playbook.xml",
    "https://news.google.com/rss/search?q=%22GZERO%22+Eurasia+Group&hl=en-US&gl=US&ceid=US:en",
    "https://econs.online/feed/",
    "https://thebell.io/feed",
    "https://ourworldindata.org/atom.xml",
    "https://news.google.com/rss/search?q=site:data.worldbank.org&hl=en-US&gl=US&ceid=US:en"
]

# 3. Пре-фильтры
JUNK_KEYWORDS_RU = [
    "инопланет", "нло ", "гороскоп", "звёзды шоу-бизнеса", "шоу-бизнес",
    "свадьб", "рецепт", "знаменитост", "поженил", "развелся", "развелась",
    "премьера сериала", "премьера фильма", "какой гороскоп",
    "открытие магазина", "открыл магазин", "новый филиал", "магазина сети",
    "расширяет сеть", "открылся первый", "новая точка",
    "подкаст", "аудиоверсия",
    "бесплатно", "музеи", "выставка", "выставки", "парк горького", "вднх", "фестиваль",
    "зумер", "миллениал", "психолог посоветовал", "психологи рассказали", "лайфхак"
]

EN_LOCAL_REGEX = re.compile(
    r'\b(primary election|city council|local mayor|gubernatorial|state senate|school board|alderman|county commissioner|local precinct)\b',
    re.IGNORECASE
)

MEDIA_JUNK_REGEX = re.compile(
    r'\(podcast\)|\b(podcast|listen to)\b', 
    re.IGNORECASE
)

CRIME_JUNK_REGEX = re.compile(
    r'\b(выпал из окна|выпала из окна|найден труп|поножовщин|дтп|сбили пешехода|задержан|возбуждено уголовное дело|убийств)\b', 
    re.IGNORECASE
)

LOCAL_POLITICS_KEYWORDS = [
    "праймериз", "пелоси", "бланше", "муницип", "мэр ", "мэра ", "мэрии",
    "городского совета", "городской думы", "местного самоуправления",
    "региональн", "губернатор", "законодательного собрания штата",
    "легислатур",
]

def resolve_canonical_name(url_or_channel):
    low = url_or_channel.lower()
    for key, canonical in FEED_CANONICAL_NAMES.items():
        if key in low:
            return canonical
    return "Источник"

def is_junk_topic(text):
    if not text:
        return False
    low = text.lower()
    if any(kw in low for kw in JUNK_KEYWORDS_RU):
        return True
    if EN_LOCAL_REGEX.search(text):
        return True
    if MEDIA_JUNK_REGEX.search(text):
        return True
    if CRIME_JUNK_REGEX.search(text):
        return True
    return False

def clean_input_text(text):
    if not text:
        return ""
    text = re.sub(r'(?i)\(?\b(FA RSS|CNews\.ru|CNews|Новое на сайте|Лента новостей)\b\)?', '', text)
    text = re.sub(r'\.\s*Лента\s+новостей', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

RSS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

def fetch_feed(feed_url):
    try:
        resp = requests.get(feed_url, headers={"User-Agent": RSS_USER_AGENT}, timeout=15)
    except requests.exceptions.SSLError:
        print(f"SSL-ошибка на {feed_url}, повтор без проверки сертификата")
        resp = requests.get(feed_url, headers={"User-Agent": RSS_USER_AGENT}, timeout=15, verify=False)
    resp.raise_for_status()
    return feedparser.parse(resp.content)

def collect_all_news(sent_urls):
    news_db = {}
    items_for_prompt = []
    item_counter = 1

    for feed_url in RSS_FEEDS:
        try:
            feed = fetch_feed(feed_url)
            canonical_source = resolve_canonical_name(feed_url)

            if not feed.entries:
                reason = feed.get("bozo_exception", "лента пуста, причина не определена")
                print(f"Пустая/битая лента [{canonical_source}] {feed_url}: {reason}")
                continue

            for entry in feed.entries[:4]:
                link = getattr(entry, 'link', feed_url).strip()
                if link in sent_urls:
                    continue

                if canonical_source == "Источник":
                    canonical_source = resolve_canonical_name(link)

                title = clean_input_text(getattr(entry, 'title', ''))
                summary = getattr(entry, 'summary', '')
                if summary:
                    summary = BeautifulSoup(summary, 'html.parser').get_text(strip=True)
                summary = clean_input_text(summary)

                if is_junk_topic(title) or is_junk_topic(summary):
                    sent_urls.add(link)
                    continue

                news_id = f"N_{item_counter}"
                item_counter += 1

                news_db[news_id] = {
                    "source_name": canonical_source,
                    "url": link
                }

                items_for_prompt.append(f"ID: {news_id} | Источник: {canonical_source}\nЗаголовок: {title}\nКонтекст: {summary[:140]}\n---")
                sent_urls.add(link)
        except Exception as e:
            print(f"Ошибка парсинга RSS [{resolve_canonical_name(feed_url)}] {feed_url}: {type(e).__name__}: {e}")

    random.shuffle(items_for_prompt)
    limited_items = items_for_prompt[:150]
    
    return news_db, "\n".join(limited_items)

def generate_analytical_json(raw_data_prompt):
    prompt_template = """
    Ты — старший аналитик-международник, готовишь дайджест для PR-специалиста.
    ОСОБЫЙ ФОКУС — на макро-решениях и России.

    КАТЕГОРИИ:
    1. "geopolitics": Геополитика и макро-решения.
    2. "economics": Экономика и институты.
    3. "business": Бизнес и M&A.
    4. "technology": Технологии и инновации.
    5. "energy": Энергетика и ресурсы.
    6. "security": Безопасность и конфликты.

    СТРОЖАЙШИЕ ПРАВИЛА:
    1. РАЗДЕЛЬНЫЕ КВОТЫ ВНУТРИ КАТЕГОРИЙ: В каждую рубрику отбирай СТРОГО НЕ БОЛЕЕ 3 событий про Россию (is_russia=true) И СТРОГО НЕ БОЛЕЕ 3 событий про остальной мир (is_russia=false).
    2. ИЗ-ЗА ЛИМИТА ТЫ ОБЯЗАН ОБЪЕДИНЯТЬ ДУБЛИКАТЫ: если разные источники пишут про одно и то же, выбери ТОЛЬКО ОДИН ID, самый содержательный. Не трать слоты рубрики на дубли!
    3. Поле "id" ДОЛЖНО СТРОГО СОВПАДАТЬ с ID из входящего блока.
    4. Поле "source_name" должно совпадать с источником под этим ID.
    5. Поле "is_russia" (true/false) — ставь true, ЕСЛИ новость напрямую касается России: её государства, экономики, армии, компаний, регионов, решений властей, ИЛИ если это реакция других стран/институтов непосредственно на Россию. Во всех остальных случаях (мировая политика, экономика других стран, глобальные события без прямой привязки к РФ) — false.
    6. ВЗАИМОИСКЛЮЧЕНИЕ КАТЕГОРИЙ: Каждый ID может быть использован строго в ОДНОЙ категории.
    7. КАТЕГОРИЧЕСКИ ИСКЛЮЧАЙ мусор (даже если он про Россию): 
       - Локальную внутреннюю политику (праймериз, назначения мэров).
       - Местечковый корпоративный PR (открытия отдельных магазинов, новые филиалы, мелкие запуски продуктов).
       - Городскую афишу и быт (работа музеев, выставок, парков, бесплатные мероприятия, ЖКХ).
       - Хронику происшествий и криминал (ДТП, выпал из окна, убийства, пожары, аресты обычных граждан).
       - Лайфстайл, спорт.
    8. "summary_ru" — факт + краткий контекст (что это значит. Но не нужно начинать предложение с «что это значит»). До 220 символов. Переводи на русский.

    JSON СТРУКТУРА:
    {
      "geopolitics": [{"id": "N_14", "source_name": "Financial Times", "summary_ru": "Факт. Почему важно: контекст.", "is_russia": false}],
      "economics": [],
      "business": [],
      "technology": [],
      "energy": [],
      "security": []
    }

    Входящие новости:
    __INPUT_DATA__
    """
    
    prompt = prompt_template.replace("__INPUT_DATA__", raw_data_prompt)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={gemini_api_key}"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "low"}
        }
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=90)

            if response.status_code in [429, 500, 502, 503, 504]:
                wait_time = 15 * (attempt + 1)
                print(f"Временная ошибка API ({response.status_code}). Ждем {wait_time} секунд...")
                time.sleep(wait_time)
                continue

            if response.status_code == 404:
                print(f"Модель недоступна (404): {response.text}\nПроверь актуальное имя модели.")
                break

            response.raise_for_status()
            result = response.json()

            finish_reason = result.get("candidates", [{}])[0].get("finishReason", "")
            if finish_reason == "MAX_TOKENS":
                print("ВНИМАНИЕ: ответ модели обрезан по лимиту maxOutputTokens.")

            return result["candidates"][0]["content"]["parts"][0]["text"]

        except requests.exceptions.RequestException as e:
            print(f"Сетевая ошибка при обращении к Gemini: {e}")
            if attempt < max_retries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            else:
                break

    raise RuntimeError("Не удалось получить ответ от Gemini API после всех попыток.")

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
    text = re.sub(r'\s*[\(\[\{][^\)\]\}]*(FA RSS|CNews|Новое на сайте|Лента новостей|Коммерсант|Foreign Affairs|ЦБ РФ)[^\)\]\}]*[\)\]\}]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\([^\)]*\)\s*$', '', text)
    return text.strip(' .-_')

def build_html_digest(raw_response, news_db):
    json_clean = clean_json_str(raw_response)
    try:
        data = json.loads(json_clean)
    except Exception as e:
        print(f"Критическая ошибка парсинга JSON от модели: {e}")
        return "", ""

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

        for key, title in sections:
            items = [it for it in data.get(key, []) if bool(it.get("is_russia")) == target_is_russia]
            html_output += f"<b>{title}</b>\n"

            valid_items_count = 0
            for item in items:
                news_id = str(item.get("id", "")).strip()
                summary = sanitize_summary_text(item.get("summary_ru", ""))

                if news_id not in news_db or not summary:
                    print(f"Отброшена галлюцинация модели с некорректным ID: {news_id}")
                    continue

                low_summary = summary.lower()
                if any(kw in low_summary for kw in LOCAL_POLITICS_KEYWORDS):
                    print(f"Отброшено пост-фильтром локальной политики: {summary[:80]}")
                    continue

                url = news_db[news_id]["url"]
                if url in seen_urls_in_digest:
                    continue
                seen_urls_in_digest.add(url)

                source_name = news_db[news_id]["source_name"]
                html_output += f"• {summary} (<a href=\"{url}\">{source_name}</a>)\n"
                valid_items_count += 1

            if valid_items_count == 0:
                html_output += "• <i>Существенных сдвигов за прошедшие часы не зафиксировано</i>\n"
            else:
                any_valid_anywhere = True

            html_output += "\n"

        return html_output.strip() if any_valid_anywhere else ""

    world_html = build_one(False, "🌍 <b>МИРОВАЯ ПОВЕСТКА</b>")
    russia_html = build_one(True, "🇷🇺 <b>РОССИЯ</b>")

    return world_html, russia_html

def send_telegram_message(chat_id, text):
    if not text.strip():
        return
        
    if len(text) <= 4000:
        try:
            bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
        except ApiTelegramException as e:
            print(f"Ошибка отправки HTML ({e}). Отправка обычным текстом.")
            bot.send_message(chat_id, text)
    else:
        blocks = text.split("\n\n")
        current_chunk = ""
        for block in blocks:
            if len(current_chunk) + len(block) + 2 <= 3900:
                current_chunk += block + "\n\n"
            else:
                if current_chunk.strip():
                    try:
                        bot.send_message(chat_id, current_chunk.strip(), parse_mode="HTML", disable_web_page_preview=True)
                    except ApiTelegramException:
                        bot.send_message(chat_id, current_chunk.strip())
                current_chunk = block + "\n\n"
        
        if current_chunk.strip():
            try:
                bot.send_message(chat_id, current_chunk.strip(), parse_mode="HTML", disable_web_page_preview=True)
            except ApiTelegramException:
                bot.send_message(chat_id, current_chunk.strip())

if __name__ == "__main__":
    sent_urls_history = load_sent_urls()

    news_db, raw_data_prompt = collect_all_news(sent_urls_history)

    if raw_data_prompt.strip():
        raw_json = generate_analytical_json(raw_data_prompt)
        world_html, russia_html = build_html_digest(raw_json, news_db)

        sent_anything = False

        if world_html.strip():
            send_telegram_message(CHAT_ID, world_html)
            sent_anything = True
            time.sleep(2)

        if russia_html.strip():
            send_telegram_message(CHAT_ID, russia_html)
            sent_anything = True

        if sent_anything:
            save_sent_urls(sent_urls_history)
    else:
        print("Новых материалов за прошедшие часы не обнаружено.")
