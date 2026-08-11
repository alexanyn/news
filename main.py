print("=== ЗАПУСК СКРИПТА ВЕРСИИ 6.2 (ROBUST_RSS_FETCH_DIAGNOSTICS) ===")

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

# 2. Таблица каноничных названий (Код удален)
FEED_CANONICAL_NAMES = {
    "8szapkg4dk4ugsj": "The Information",
    "theinformation": "The Information",
    "hwdohujjvtdlecen": "NielsenIQ",
    "nielseniq": "NielsenIQ",
    "3zhqk2somyi842d3": "The Economist",
    "economist": "The Economist",
    "vbq995yof2htzk6g": "Financial Times",
    "ft.com": "Financial Times",
    "f5bcrxyyec7mwoqu": "The New York Times",
    "nytimes": "The New York Times",
    "cwnxbm8vpmgcknsl": "Washington Post",
    "washingtonpost": "Washington Post",
    "g3y3fke9lxj30mus": "ВТО",
    "wto.org": "ВТО",
    "vta4mv1lskm5conw": "ОПЕК",
    "opec.org": "ОПЕК",
    "gl0q2mprqqui5vyx": "BlackRock",
    "blackrock": "BlackRock",
    "hdkobk5wtxkvpz5l": "ВЭФ",
    "weforum": "ВЭФ",
    "a6qznawxvjrfdtau": "Mediascope",
    "mediascope": "Mediascope",
    "cbr.ru": "ЦБ РФ",
    "4n9gkl2gmfhjdlx2": "ЦБ РФ",
    "xq3dpenk8t6kkzde": "РОМИР",
    "romir": "РОМИР",
    "wdcmvjy7bajgrtcc": "Росстат",
    "rosstat": "Росстат",
    "fom.ru": "ФОМ",
    "wciom.ru": "ВЦИОМ",
    "levada.ru": "Левада-Центр",
    "sostav.ru": "Состав",
    "adindex.ru": "AdIndex",
    "cnews.ru": "CNews",
    "nplus1.ru": "N+1",
    "pharmvestnik.ru": "Фармвестник",
    "vademec.ru": "Vademecum",
    "retail.ru": "Retail.ru",
    "kommersant.ru": "Коммерсантъ",
    "tass.ru": "ТАСС",
    "ria.ru": "РИА Новости",
    "interfax.ru": "Интерфакс",
    "vedomosti.ru": "Ведомости",
    "iz.ru": "Известия",
    "rbc.ru": "РБК",
    "forbes.ru": "Forbes",
    "rg.ru": "Российская Газета",
    "tvzvezda.ru": "ТК Звезда",
    "1prime.ru": "Прайм",
    "frankmedia.ru": "Frank Media",
    "globalaffairs.ru": "Россия в глоб. политике",
    "valdaiclub.com": "Валдай",
    "xn8geg0kjxjnedsc": "Associated Press",
    "apnews": "Associated Press",
    "bloomberg": "Bloomberg",
    "ozplb3ix17vahziy": "Politico",
    "politico": "Politico",
    "9f4zackjgycpaee7": "Reuters",
    "reuters": "Reuters",
    "foreignaffairs.com": "Foreign Affairs",
    "pewresearch.org": "Pew Research",
    "cfr.org": "CFR",
    "csis.org": "CSIS",
    "bruegel.org": "Bruegel",
    "carnegieendowment.org": "Carnegie",
    "theguardian.com": "The Guardian",
    "aljazeera.com": "Al Jazeera",
    "lemonde.fr": "Le Monde",
    "statnews.com": "STAT News",
    "retaildive.com": "Retail Dive",
    "project-syndicate.org": "Project Syndicate",
    "istories.media": "Важные истории",
    "zona.media": "Медиазона",
    "currenttime.tv": "Настоящее Время",
    "dw.com": "Deutsche Welle",
    "xz567x8w88wqe8iz": "Инфо-источник",
    "mmi_ru": "MMI",
    "solidfin": "Solid Financial",
    "xtxixty": "Твёрдые цифры",
    "russianmacro": "Russianmacro"
}

RSS_FEEDS = [
    "https://rss.app/feeds/vbq995yof2htzK6g.xml",
    "https://rss.app/feeds/f5bCrXyyEC7MWoqu.xml",
    "https://rss.app/feeds/3zHq2kSOmYI842d3.xml",
    "https://rss.app/feeds/CWnxBM8vpMgcKNsL.xml",
    "https://rss.app/feeds/8sZapkG4DK4u7gSj.xml",
    "https://rss.app/feeds/G3Y3Fke9lxj30Mus.xml",
    "https://rss.app/feeds/hWDoHUUjvtDleceN.xml",
    "https://rss.app/feeds/VTA4mv1lSKM5cONW.xml",
    "https://rss.app/feeds/gL0Q2MprqQui5vYX.xml",
    "https://rss.app/feeds/hdKObk5WtxkvPz5L.xml",
    "https://rss.app/feeds/a6QZNawxvjrfDtaU.xml",
    "https://rss.app/feeds/4N9GkL2gMfHjdlx2.xml",
    "https://rss.app/feeds/XQ3dPeNk8t6KKZDe.xml",
    "https://rss.app/feeds/WDCmvjy7BajGRTCc.xml",
    "https://fom.ru/rss.xml",
    "https://wciom.ru/rss.xml",
    "https://www.levada.ru/feed/",
    "https://www.sostav.ru/rss",
    "https://adindex.ru/news/news.rss",
    "https://rss.app/feeds/Xn8gEg0kjXjnedSc.xml",
    "https://feeds.bloomberg.com/business/news.rss",
    "https://rss.app/feeds/OZpLB3ix17VahZIY.xml",
    "https://rss.app/feeds/9F4ZacKjgYCPaEE7.xml",
    "https://tass.ru/rss/v2.xml",
    "https://ria.ru/export/rss2/archive/index.xml",
    "https://www.interfax.ru/rss.asp",
    "https://www.kommersant.ru/RSS/news.xml",
    "https://www.vedomosti.ru/rss/news",
    "https://iz.ru/xml/rss/all.xml",
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "https://www.forbes.ru/new-rss.xml",
    "https://rg.ru/xml/index.xml",
    "https://tvzvezda.ru/export/rss.xml",
    "https://1prime.ru/export/rss2/index.xml",
    "https://frankmedia.ru/feed",
    "https://www.cnews.ru/inc/rss/news.xml",
    "https://nplus1.ru/rss",
    "https://pharmvestnik.ru/rss/news.xml",
    "https://vademec.ru/rss/",
    "https://www.retail.ru/rss/news/",
    "https://globalaffairs.ru/feed/",
    "https://ru.valdaiclub.com/rss/",
    "https://istories.media/rss/all.xml",
    "https://zona.media/rss",
    "https://www.currenttime.tv/api/z$gqiteyq_gt",
    "https://rss.dw.com/xml/rss-ru-all",
    "https://rss.app/feeds/Xz567X8w88wqe8IZ.xml",
    "https://www.foreignaffairs.com/rss.xml",
    "https://www.pewresearch.org/feed/",
    "https://www.cfr.org/rss.xml",
    "https://www.csis.org/rss/all",
    "https://www.bruegel.org/rss.xml",
    "https://carnegieendowment.org/rss/solr/publications",
    "https://www.theguardian.com/world/rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.statnews.com/feed/",
    "https://www.retaildive.com/feeds/news/",
    "https://www.project-syndicate.org/rss"
]

TG_CHANNELS = ["mmi_ru", "solidfin", "xtxixty", "russianmacro"]

# 3. Пре-фильтры
JUNK_KEYWORDS_RU = [
    "инопланет", "нло ", "гороскоп", "звёзды шоу-бизнеса", "шоу-бизнес",
    "свадьб", "рецепт", "знаменитост", "поженил", "развелся", "развелась",
    "премьера сериала", "премьера фильма", "какой гороскоп",
    "открытие магазина", "открыл магазин", "новый филиал", "магазина сети",
    "расширяет сеть", "открылся первый", "новая точка",
    "подкаст", "аудиоверсия",
    # Городская афиша и лайфстайл
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

# Фильтр криминала и ЧП
CRIME_JUNK_REGEX = re.compile(
    r'\b(выпал из окна|выпала из окна|найден труп|поножовщин|дтп|сбили пешехода|задержан|возбуждено уголовное дело|убийств)\b', 
    re.IGNORECASE
)

# 4. Пост-фильтр
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
    """Загружает ленту через requests с браузерным User-Agent и таймаутом,
    затем отдаёт байты в feedparser. В отличие от feedparser.parse(url) напрямую,
    здесь видно РЕАЛЬНУЮ причину сбоя (403, таймаут, SSL, редирект) —
    feedparser молча глотает такие ошибки и просто возвращает пустой feed."""
    try:
        resp = requests.get(feed_url, headers={"User-Agent": RSS_USER_AGENT}, timeout=15)
    except requests.exceptions.SSLError:
        # У некоторых сайтов (в т.ч. госорганизаций) кривой/самоподписанный сертификат.
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

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    for channel in TG_CHANNELS:
        try:
            url = f"https://t.me/s/{channel}"
            res = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(res.text, 'html.parser')
            
            messages = soup.find_all('div', class_='tgme_widget_message')
            valid_messages = [m for m in messages if 'service_message' not in m.get('class', [])]
            canonical_source = resolve_canonical_name(channel)

            for msg in valid_messages[-3:]:
                data_post = msg.get('data-post')
                text_div = msg.find('div', class_='tgme_widget_message_text')
                if not text_div:
                    continue

                post_text = clean_input_text(text_div.get_text(strip=True))
                
                if data_post:
                    post_url = f"https://t.me/{data_post}"
                else:
                    post_url = f"https://t.me/{channel}"

                if post_url in sent_urls:
                    continue

                if is_junk_topic(post_text):
                    sent_urls.add(post_url)
                    continue

                news_id = f"N_{item_counter}"
                item_counter += 1

                news_db[news_id] = {
                    "source_name": canonical_source,
                    "url": post_url
                }

                items_for_prompt.append(f"ID: {news_id} | Источник: {canonical_source}\nКонтекст: {post_text[:140]}\n---")
                sent_urls.add(post_url)
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")

    random.shuffle(items_for_prompt)
    limited_items = items_for_prompt[:150]  # Gemini free tier легко тянет больше items, чем Groq
    
    return news_db, "\n".join(limited_items)

def generate_analytical_json(raw_data_prompt):
    prompt_template = """
    Ты — старший аналитик-международник, готовишь дайджест для PR-специалиста.
    ОСОБЫЙ ФОКУС — на макро-решениях и России.

    КАТЕГОРИИ:
    1. "politics": Законодательство, госуправление, геополитика, национальные выборы.
    2. "conflicts": Военные действия, оборона, безопасность.
    3. "economy": Макроэкономика, рынки, ЦБ, ОПЕК.
    4. "b2b_retail": B2B, макро-ритейл, логистика.
    5. "tech_health": IT, ИИ, фармакология.
    6. "society": Общество, социологические опросы.

    СТРОЖАЙШИЕ ПРАВИЛА:
    1. ЖЕСТКИЙ ЛИМИТ: Отбирай СТРОГО НЕ БОЛЕЕ 4 самых важных событий на каждую рубрику. 
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
       - Лайфстайл и поп-психологию (советы зумерам/миллениалам, диеты, отношения).
       - Бытовую недвижимость, шоу-бизнес, спорт.
    8. "summary_ru" — факт + краткий контекст (почему важно). До 220 символов. Переводи на русский.

    JSON СТРУКТУРА:
    {
      "politics": [{"id": "N_14", "source_name": "Financial Times", "summary_ru": "Факт. Почему важно: контекст.", "is_russia": false}],
      "conflicts": [],
      "economy": [],
      "b2b_retail": [],
      "tech_health": [],
      "society": []
    }

    Входящие новости:
    __INPUT_DATA__
    """
    
    prompt = prompt_template.replace("__INPUT_DATA__", raw_data_prompt)

    # Модель: gemini-3.6-flash — актуальная GA-версия на август 2026.
    # Google меняет доступность моделей быстрее, чем документация — если снова
    # вылетит 404 "no longer available", смотри актуальное имя здесь:
    # https://ai.google.dev/gemini-api/docs/models
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
        response = requests.post(url, json=payload, timeout=90)

        if response.status_code == 429:
            wait_time = 15 * (attempt + 1)
            print(f"Превышен лимит Gemini (429). Ждем {wait_time} секунд...")
            time.sleep(wait_time)
            continue

        if response.status_code == 404:
            print(f"Модель недоступна (404): {response.text}\nПроверь актуальное имя модели: https://ai.google.dev/gemini-api/docs/models")

        if response.status_code != 200:
            print(f"Ошибка Gemini API ({response.status_code}): {response.text}")

        response.raise_for_status()
        result = response.json()

        finish_reason = result.get("candidates", [{}])[0].get("finishReason", "")
        if finish_reason == "MAX_TOKENS":
            print("ВНИМАНИЕ: ответ модели обрезан по лимиту maxOutputTokens — увеличь лимит в generate_analytical_json.")

        return result["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError("Не удалось получить ответ от Gemini API.")

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
        ("politics", "🏛 ПОЛИТИКА И ГОСУПРАВЛЕНИЕ"),
        ("conflicts", "🪖 КОНФЛИКТЫ И БЕЗОПАСНОСТЬ"),
        ("economy", "📈 ЭКОНОМИКА И ФИНАНСЫ"),
        ("b2b_retail", "💼 ОТРАСЛЕВОЙ B2B И РИТЕЙЛ"),
        ("tech_health", "🧬 ТЕХНОЛОГИИ И ЗДРАВООХРАНЕНИЕ"),
        ("society", "👥 ОБЩЕСТВО И СОЦИОЛОГИЯ")
    ]

    def build_one(target_is_russia, header):
        seen_urls_in_digest = set()
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
