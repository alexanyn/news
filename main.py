import os
import re
import json
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

# 2. Таблица жесткого соответствия URL-фидов и каноничных названий
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

def collect_all_news(sent_urls):
    news_db = {}
    items_for_prompt = []
    item_counter = 1

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            canonical_source = resolve_canonical_name(feed_url)

            for entry in feed.entries[:3]:
                link = getattr(entry, 'link', feed_url).strip()
                if link in sent_urls:
                    continue

                title = entry.title
                summary = getattr(entry, 'summary', '')
                if summary:
                    summary = BeautifulSoup(summary, 'html.parser').get_text(strip=True)

                news_id = item_counter
                item_counter += 1

                news_db[news_id] = {
                    "source_name": canonical_source,
                    "url": link
                }

                items_for_prompt.append(f"ID: {news_id}\nЗаголовок: {title}\nКонтекст: {summary[:400]}\n---")
                sent_urls.add(link)
        except Exception as e:
            print(f"Ошибка парсинга RSS {feed_url}: {e}")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    for channel in TG_CHANNELS:
        try:
            url = f"https://t.me/s/{channel}"
            res = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(res.text, 'html.parser')
            posts = soup.find_all('div', class_='tgme_widget_message_text', limit=3)

            canonical_source = resolve_canonical_name(channel)

            for post in posts:
                post_text = post.get_text(strip=True)
                post_hash = f"tg_{channel}_{hash(post_text[:100])}"

                if post_hash in sent_urls:
                    continue

                news_id = item_counter
                item_counter += 1

                news_db[news_id] = {
                    "source_name": canonical_source,
                    "url": url
                }

                items_for_prompt.append(f"ID: {news_id}\nКонтекст: {post_text[:400]}\n---")
                sent_urls.add(post_hash)
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")

    return news_db, "\n".join(items_for_prompt)

def generate_analytical_json(raw_data_prompt):
    prompt = f"""
    Ты — макроэкономический аналитик. Проанализируй новости и сгруппируй их по 4 категориям.

    КРИТИЧЕСКИЕ ПРАВИЛА:
    1. Ответ верни СТРОГО в формате JSON.
    2. Поле "summary_ru" должно содержать ТОЛЬКО смысловой тезис новости СТРОГО НА РУССКОМ ЯЗЫКЕ.
    3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать названия источников или вставлять скобки в "summary_ru"!
    4. Поле "id" должно содержать ТОЛЬКО ЦЕЛОЕ ЧИСЛО (ID из входящих данных).

    СТРУКТУРА JSON:
    {{
      "macro": [{"id": 1, "summary_ru": "Тезис на русском"}],
      "geopolitics": [{"id": 2, "summary_ru": "Тезис на русском"}],
      "industry": [],
      "risks": []
    }}

    Входящие новости:
    {raw_data_prompt}
    """

    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=90
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

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
    """Принудительное удаление мусорных хвостов со скобками из ответа нейросети"""
    # Удаляем скобки вроде (FA RSS), (CNews.ru), (Новое на сайте), (Коммерсантъ. Лента новостей)
    text = re.sub(r'\s*\([^)]*(FA RSS|CNews|Новое на сайте|Лента новостей|Коммерсантъ)[^)]*\)', '', text, flags=re.IGNORECASE)
    return text.strip()

def build_html_digest(raw_response, news_db):
    json_clean = clean_json_str(raw_response)
    try:
        data = json.loads(json_clean)
    except Exception as e:
        print(f"Ошибка парсинга JSON: {e}")
        return ""

    sections = [
        ("macro", "📊 МАКРОЭКОНОМИКА И ФИНАНСЫ"),
        ("geopolitics", "🌍 ГЕОПОЛИТИКА И БЕЗОПАСНОСТЬ"),
        ("industry", "💼 ОТРАСЛЕВЫЕ ТРЕНДЫ И B2B"),
        ("risks", "⚠️ СКРЫТЫЕ РИСКИ")
    ]

    html_output = ""
    for key, title in sections:
        items = data.get(key, [])
        if items:
            html_output += f"<b>{title}</b>\n"
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
            html_output += "\n"

    return html_output.strip()

def send_telegram_message(chat_id, text):
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except ApiTelegramException as e:
        print(f"Ошибка отправки HTML ({e}). Отправка обычным текстом.")
        bot.send_message(chat_id, text)

if __name__ == "__main__":
    sent_urls_history = load_sent_urls()

    news_db, raw_data_prompt = collect_all_news(sent_urls_history)

    if raw_data_prompt.strip():
        raw_json = generate_analytical_json(raw_data_prompt)
        formatted_html = build_html_digest(raw_json, news_db)

        if formatted_html.strip():
            for i in range(0, len(formatted_html), 4000):
                send_telegram_message(CHAT_ID, formatted_html[i:i+4000])

            save_sent_urls(sent_urls_history)
    else:
        print("Новых материалов за прошедшие часы не обнаружено.")
