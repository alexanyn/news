print("=== ЗАПУСК СКРИПТА ВЕРСИИ 4.8 (FIX_FULL_INTERNALS_COLLECTION) ===")

import os
import re
import json
import time
import requests
import feedparser
from bs4 import BeautifulSoup
import telebot
from telebot.apihelper import ApiTelegramException

# 1. Переменные окружения
groq_api_key = os.environ.get("GROQ_API_KEY")
bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")

if not groq_api_key or not bot_token or not chat_id:
    raise ValueError("Ошибка: Проверьте GROQ_API_KEY, TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Secrets!")

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
    # Увеличено до 5000, чтобы не терять память о ссылках при частых запусках
    urls_list = list(sent_set)[-5000:]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(urls_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения истории: {e}")

# 2. Таблица каноничных названий
FEED_CANONICAL_NAMES = {
    # Премиальные и профильные прокси (явные ID + фрагменты URL)
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

    # Государственные структуры, социология и статистика
    "cbr.ru": "ЦБ РФ",
    "4n9gkl2gmfhjdlx2": "ЦБ РФ",
    "xq3dpenk8t6kkzde": "РОМИР",
    "romir": "РОМИР",
    "wdcmvjy7bajgrtcc": "Росстат",
    "rosstat": "Росстат",
    "fom.ru": "ФОМ",
    "wciom.ru": "ВЦИОМ",
    "levada.ru": "Левада-Центр",

    # Маркетинг, IT, реклама и медиабизнес
    "kod.ru": "Код",
    "sostav.ru": "Состав",
    "adindex.ru": "AdIndex",
    "cnews.ru": "CNews",
    "nplus1.ru": "N+1",
    "pharmvestnik.ru": "Фармвестник",
    "vademec.ru": "Vademecum",
    "retail.ru": "Retail.ru",

    # Российские деловые СМИ
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

    # Международные информагентства
    "xn8geg0kjxjnedsc": "Associated Press",
    "apnews": "Associated Press",
    "bloomberg": "Bloomberg",
    "ozplb3ix17vahziy": "Politico",
    "politico": "Politico",
    "9f4zackjgycpaee7": "Reuters",
    "reuters": "Reuters",

    # Независимые и аналитические международные институты
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

    # Независимые русскоязычные медиа
    "istories.media": "Важные истории",
    "zona.media": "Медиазона",
    "currenttime.tv": "Настоящее Время",
    "dw.com": "Deutsche Welle",
    "xz567x8w88wqe8iz": "Инфо-источник",

    # Telegram
    "mmi_ru": "MMI",
    "solidfin": "Solid Financial",
    "xtxixty": "Твёрдые цифры",
    "russianmacro": "Russianmacro"
}

RSS_FEEDS = [
    # 1. Новые премиальные прокси-источники
    "https://rss.app/feeds/vbq995yof2htzK6g.xml",  # Financial Times
    "https://rss.app/feeds/f5bCrXyyEC7MWoqu.xml",  # NYT
    "https://rss.app/feeds/3zHq2kSOmYI842d3.xml",  # The Economist
    "https://rss.app/feeds/CWnxBM8vpMgcKNsL.xml",  # Washington Post
    "https://rss.app/feeds/8sZapkG4DK4u7gSj.xml",  # The Information
    "https://rss.app/feeds/G3Y3Fke9lxj30Mus.xml",  # ВТО / WTO
    "https://rss.app/feeds/hWDoHUUjvtDleceN.xml",  # NielsenIQ
    "https://rss.app/feeds/VTA4mv1lSKM5cONW.xml",  # ОПЕК / OPEC
    "https://rss.app/feeds/gL0Q2MprqQui5vYX.xml",  # BlackRock
    "https://rss.app/feeds/hdKObk5WtxkvPz5L.xml",  # ВЭФ / WEF
    "https://rss.app/feeds/a6QZNawxvjrfDtaU.xml",  # Mediascope

    # 2. Российские госорганы, социология и макростатистика
    "https://rss.app/feeds/4N9GkL2gMfHjdlx2.xml",  # ЦБ РФ
    "https://rss.app/feeds/XQ3dPeNk8t6KKZDe.xml",  # РОМИР
    "https://rss.app/feeds/WDCmvjy7BajGRTCc.xml",  # Росстат
    "https://fom.ru/rss.xml",
    "https://wciom.ru/rss.xml",
    "https://www.levada.ru/feed/",

    # 3. Маркетинг, IT, реклама и медиабизнес
    "https://kod.ru/rss",
    "https://www.sostav.ru/rss",
    "https://adindex.ru/news/news.rss",

    # 4. Международные прокси-медиа
    "https://rss.app/feeds/Xn8gEg0kjXjnedSc.xml",  # Associated Press
    "https://feeds.bloomberg.com/business/news.rss",  # Bloomberg
    "https://rss.app/feeds/OZpLB3ix17VahZIY.xml",  # Politico
    "https://rss.app/feeds/9F4ZacKjgYCPaEE7.xml",  # Reuters

    # 5. Российские деловые СМИ
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

    # 6. Независимые и международные русскоязычные медиа
    "https://istories.media/rss/all.xml",  # Важные истории
    "https://zona.media/rss",  # Медиазона
    "https://www.currenttime.tv/api/z$gqiteyq_gt",  # Настоящее Время
    "https://rss.dw.com/xml/rss-ru-all",  # DW на русском
    "https://rss.app/feeds/Xz567X8w88wqe8IZ.xml",

    # 7. Международная аналитика и профильные институты
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

def resolve_canonical_name(url_or_channel):
    low = url_or_channel.lower()
    for key, canonical in FEED_CANONICAL_NAMES.items():
        if key in low:
            return canonical
    return "Источник"

def clean_input_text(text):
    if not text:
        return ""
    text = re.sub(r'(?i)\(?\b(FA RSS\vert{}CNews\.ru\vert{}CNews\vert{}Новое на сайте\vert{}Лента новостей)\b\)?', '', text)
    text = re.sub(r'\.\s*Лента\s+новостей', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def collect_all_news(sent_urls):
    news_db = {}
    items_for_prompt = []
    item_counter = 1

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            canonical_source = resolve_canonical_name(feed_url)

            # ИСПРАВЛЕНО: Забираем ВСЕ неизученные записи вместо ограничений [:2]
            for entry in feed.entries:
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

                news_id = item_counter
                item_counter += 1

                news_db[news_id] = {
                    "source_name": canonical_source,
                    "url": link
                }

                items_for_prompt.append(f"ID: {news_id}\nЗаголовок: {title}\nКонтекст: {summary[:250]}\n---")
                sent_urls.add(link)
        except Exception as e:
            print(f"Ошибка парсинга RSS {feed_url}: {e}")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    for channel in TG_CHANNELS:
        try:
            url = f"https://t.me/s/{channel}"
            res = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(res.text, 'html.parser')
            
            messages = soup.find_all('div', class_='tgme_widget_message')
            valid_messages = [m for m in messages if 'service_message' not in m.get('class', [])]
            
            # ИСПРАВЛЕНО: Забираем все свежие посты из выборки веб-скрапинга, а не последние 2
            canonical_source = resolve_canonical_name(channel)

            for msg in valid_messages:
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

                news_id = item_counter
                item_counter += 1

                news_db[news_id] = {
                    "source_name": canonical_source,
                    "url": post_url
                }

                items_for_prompt.append(f"ID: {news_id}\nКонтекст: {post_text[:250]}\n---")
                sent_urls.add(post_url)
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")

    # ИСПРАВЛЕНО: Убрана искусственная срезка [:80], отдаём все уникальные новости в модель
    return news_db, "\n".join(items_for_prompt)

def generate_analytical_json(raw_data_prompt):
    prompt_template = """
    Ты — старший международный аналитик. Проанализируй входящие данные и отбери все значимые события.

    КАТЕГОРИИ:
    1. "politics": Законодательство, госуправление, геополитика, международные решения, выборы.
    2. "conflicts": Военные действия, оборона, спецслужбы, международная безопасность.
    3. "economy": Макроэкономика, рынки, инфляция, банковские ставки, курсы валют, данные Росстата, ЦБ, ОПЕК, ВТО, РОМИР.
    4. "b2b_retail": B2B-тренды, ритейл, торговые сети, рекламный рынок (Sostav, AdIndex, Mediascope, NielsenIQ), логистика, промышленность.
    5. "tech_health": IT-сектор, ИИ, фармакология, медицина, научные разработки, медиатехнологии (Код).
    6. "society": Общественные тренды, социологические опросы (ФОМ, ВЦИОМ, Левада, РОМИР), макро-социальные явления.

    ЖЕСТКИЕ ПРАВИЛА:
    1. Обязательно выдерживай международный баланс! Не менее 40% дайджеста должны составлять зарубежные и аналитические источники (Financial Times, NYT, The Economist, Washington Post, Associated Press, Reuters, Bloomberg, ВТО, ОПЕК, BlackRock, ВЭФ и др.).
    2. Отбирай до 4 главнейших событий на каждую категорию.
    3. КАТЕГОРИЧЕСКИ ИСКЛЮЧАЙ: бытовую недвижимость (аренда квартир), ремонт дорог, эстакады, спорт, шоу-бизнес, бытовые ДТП и бытовые советы.
    4. Переводи ВСЕ зарубежные материалы на русский язык.
    5. Поле "summary_ru" должно содержать суть на русском языке без скобок и названий источников!
    6. Поле "id" должно содержать ТОЛЬКО ЦЕЛОЕ ЧИСЛО (ID из входящих данных).

    СТРУКТУРА JSON:
    {
      "politics": [{"id": 1, "summary_ru": "Суть"}],
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

    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 3000,
        "response_format": {"type": "json_object"}
    }

    max_retries = 3
    for attempt in range(max_retries):
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=90
        )
        if response.status_code == 429:
            wait_time = 10 * (attempt + 1)
            print(f"Превышен лимит Groq (429). Ждем {wait_time} секунд...")
            time.sleep(wait_time)
            continue
            
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
        
    raise RuntimeError("Не удалось получить ответ от Groq API.")

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
        return ""

    sections = [
        ("politics", "🏛 ПОЛИТИКА И ГОСУПРАВЛЕНИЕ"),
        ("conflicts", "🪖 КОНФЛИКТЫ И БЕЗОПАСНОСТЬ"),
        ("economy", "📈 ЭКОНОМИКА И ФИНАНСЫ"),
        ("b2b_retail", "💼 ОТРАСЛЕВОЙ B2B И РИТЕЙЛ"),
        ("tech_health", "🧬 ТЕХНОЛОГИИ И ЗДРАВООХРАНЕНИЕ"),
        ("society", "👥 ОБЩЕСТВО И СОЦИОЛОГИЯ")
    ]

    html_output = ""
    for key, title in sections:
        items = data.get(key, [])
        html_output += f"<b>{title}</b>\n"
        
        valid_items_count = 0
        if items:
            for item in items:
                try:
                    news_id = int(item.get("id"))
                except (ValueError, TypeError):
                    continue

                summary = sanitize_summary_text(item.get("summary_ru", ""))

                if news_id in news_db and summary:
                    source_name = news_db[news_id]["source_name"]
                    url = news_db[news_id]["url"]
                    html_output += f"• {summary} (<a href=\"{url}\">{source_name}</a>)\n"
                    valid_items_count += 1

        if valid_items_count == 0:
            html_output += "• <i>Существенных сдвигов за прошедшие часы не зафиксировано</i>\n"

        html_output += "\n"

    return html_output.strip()

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
        formatted_html = build_html_digest(raw_json, news_db)

        if formatted_html.strip():
            send_telegram_message(CHAT_ID, formatted_html)
            save_sent_urls(sent_urls_history)
    else:
        print("Новых материалов за прошедшие часы не обнаружено.")
