print("=== ЗАПУСК СКРИПТА ВЕРСИИ 4.2 (GROQ_8B_INSTANT_STABLE) ===")

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
    urls_list = list(sent_set)[-1000:]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(urls_list, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения истории: {e}")

# 2. Таблица каноничных названий
FEED_CANONICAL_NAMES = {
    "cbr.ru": "ЦБ РФ",
    "kommersant.ru": "Коммерсантъ",
    "foreignaffairs.com": "Foreign Affairs",
    "cnews.ru": "CNews",
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
    "nplus1.ru": "N+1",
    "pharmvestnik.ru": "Фармвестник",
    "vademec.ru": "Vademecum",
    "retail.ru": "Retail.ru",
    "globalaffairs.ru": "Россия в глоб. политике",
    "valdaiclub.com": "Валдай",
    "fom.ru": "ФОМ",
    "wciom.ru": "ВЦИОМ",
    "levada.ru": "Левада-Центр",
    "pewresearch.org": "Pew Research",
    "cfr.org": "CFR",
    "csis.org": "CSIS",
    "bruegel.org": "Bruegel",
    "carnegieendowment.org": "Carnegie",
    "theguardian.com": "The Guardian",
    "aljazeera.com": "Al Jazeera",
    "politico.eu": "Politico",
    "politico.com": "Politico",
    "lemonde.fr": "Le Monde",
    "statnews.com": "STAT News",
    "retaildive.com": "Retail Dive",
    "project-syndicate.org": "Project Syndicate",
    "reuters.com": "Reuters",
    "apnews.com": "AP News",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "wsj.com": "WSJ",
    "nytimes.com": "NYT",
    "economist.com": "The Economist",
    "washingtonpost.com": "Washington Post",
    "theinformation.com": "The Information",
    "spglobal.com": "S&P Global",
    "msci.com": "MSCI",
    "mmi_ru": "MMI",
    "solidfin": "Solid Financial",
    "xtxixty": "Твёрдые цифры",
    "russianmacro": "Russianmacro"
}

RSS_FEEDS = [
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
    "https://cbr.ru/rss/RssNews",
    "https://www.cnews.ru/inc/rss/news.xml",
    "https://nplus1.ru/rss",
    "https://pharmvestnik.ru/rss/news.xml",
    "https://vademec.ru/rss/",
    "https://www.retail.ru/rss/news/",
    "https://globalaffairs.ru/feed/",
    "https://ru.valdaiclub.com/rss/",
    "https://fom.ru/rss.xml",
    "https://wciom.ru/rss.xml",
    "https://www.levada.ru/feed/",
    "https://www.foreignaffairs.com/rss.xml",
    "https://www.pewresearch.org/feed/",
    "https://www.cfr.org/rss.xml",
    "https://www.csis.org/rss/all",
    "https://www.bruegel.org/rss.xml",
    "https://carnegieendowment.org/rss/solr/publications",
    "https://www.theguardian.com/world/rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.politico.eu/feed/",
    "https://rss.politico.com/politics-news.xml",
    "https://www.lemonde.fr/en/international/rss_full.xml",
    "https://www.statnews.com/feed/",
    "https://www.retaildive.com/feeds/news/",
    "https://www.project-syndicate.org/rss",
    "https://news.google.com/rss/search?q=site:reuters.com/world",
    "https://news.google.com/rss/search?q=site:apnews.com/world-news",
    "https://news.google.com/rss/search?q=site:bloomberg.com",
    "https://news.google.com/rss/search?q=site:ft.com",
    "https://news.google.com/rss/search?q=site:wsj.com",
    "https://news.google.com/rss/search?q=site:nytimes.com/section/world",
    "https://news.google.com/rss/search?q=site:economist.com",
    "https://news.google.com/rss/search?q=site:washingtonpost.com",
    "https://news.google.com/rss/search?q=site:theinformation.com",
    "https://news.google.com/rss/search?q=site:spglobal.com",
    "https://news.google.com/rss/search?q=site:msci.com"
]

TG_CHANNELS = ["mmi_ru", "solidfin", "xtxixty", "russianmacro"]

def resolve_canonical_name(url_or_channel):
    low = url_or_channel.lower()
    for domain, canonical in FEED_CANONICAL_NAMES.items():
        if domain in low:
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

            for entry in feed.entries[:2]:
                link = getattr(entry, 'link', feed_url).strip()
                if link in sent_urls:
                    continue

                title = clean_input_text(entry.title)
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

                items_for_prompt.append(f"ID: {news_id}\nЗаголовок: {title}\nКонтекст: {summary[:200]}\n---")
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
            recent_messages = valid_messages[-2:] if len(valid_messages) >= 2 else valid_messages

            canonical_source = resolve_canonical_name(channel)

            for msg in recent_messages:
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

                items_for_prompt.append(f"ID: {news_id}\nКонтекст: {post_text[:200]}\n---")
                sent_urls.add(post_url)
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")

    # Оптимальный объем в 50 материалов
    limited_items = items_for_prompt[:50]
    return news_db, "\n".join(limited_items)

def generate_analytical_json(raw_data_prompt):
    prompt_template = """
    Ты — старший аналитик. Проанализируй входящие данные и отбери все значимые события.

    КАТЕГОРИИ:
    1. "politics": Законодательство, госуправление, геополитика, международные решения, выборы.
    2. "conflicts": Военные действия, оборона, спецслужбы, международная безопасность.
    3. "economy": Макроэкономика, рынки, инфляция, банковские ставки, курсы валют, инвестиции.
    4. "b2b_retail": B2B-тренды, ритейл, торговые сети, логистика, промышленность, коммерция.
    5. "tech_health": IT-сектор, ИИ, фармакология, медицина, научные разработки.
    6. "society": Общественные тренды, социологические опросы (ФОМ, ВЦИОМ, Левада), макро-социальные явления.

    ЖЕСТКИЕ ПРАВИЛА И СТОП-ЛИСТ:
    1. Отбирай до 4-5 главных событий на каждую категорию, если они есть.
    2. КАТЕГОРИЧЕСКИ ИСКЛЮЧАЙ: бытовую недвижимость (аренда квартир), ремонт дорог, эстакады, спорт, шоу-бизнес, бытовые ДТП и бытовые финансовые советы.
    3. Переводи все зарубежные материалы на русский язык.
    4. Поле "summary_ru" должно содержать суть на русском языке без скобок и названий источников!
    5. Поле "id" должно содержать ТОЛЬКО ЦЕЛОЕ ЧИСЛО (ID из входящих данных).

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
        "model": "llama-3.1-8b-instant",  # Лимит 30,000 TPM убирает ошибки 429
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2500,
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
