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

# 2. Полный массив RSS-источников (Прямые XML + Google News RSS для сайтов с Paywall)
RSS_FEEDS = [
    # Российские СМИ и Финансы
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
    
    # Зарубежная аналитика и Открытые СМИ (Прямой RSS)
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
    
    # Зарубежные СМИ с жестким Paywall / Cloudflare (Через Google News RSS)
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

# 3. Публичные Telegram-каналы
TG_CHANNELS = [
    "mmi_ru",
    "solidfin",
    "xtxixty",
    "russianmacro"
]

# 4. Карта очистки имен источников
SOURCE_CLEAN_MAP = {
    "тасс": "ТАСС",
    "риа новости": "РИА Новости",
    "интерфакс": "Интерфакс",
    "коммерсант": "Коммерсантъ",
    "ведомости": "Ведомости",
    "известия": "Известия",
    "рбк": "РБК",
    "forbes": "Forbes",
    "российская газета": "Российская Газета",
    "звезда": "ТК Звезда",
    "прайм": "Прайм",
    "frank media": "Frank Media",
    "cnews": "CNews",
    "n + 1": "N+1",
    "фармацевтический вестник": "Фармвестник",
    "vademecum": "Vademecum",
    "retail.ru": "Retail.ru",
    "россия в глобальной политике": "Россия в глоб. политике",
    "валдай": "Валдай",
    "банк россии": "Банк России",
    "foreign affairs": "Foreign Affairs",
    "cfr": "CFR",
    "csis": "CSIS",
    "pew research": "Pew Research",
    "reuters": "Reuters",
    "associated press": "AP News",
    "ap news": "AP News",
    "bloomberg": "Bloomberg",
    "financial times": "Financial Times",
    "ft.com": "Financial Times",
    "wall street journal": "WSJ",
    "wsj": "WSJ",
    "new york times": "NYT",
    "nyt": "NYT",
    "guardian": "The Guardian",
    "economist": "The Economist",
    "al jazeera": "Al Jazeera",
    "politico": "Politico",
    "washington post": "Washington Post",
    "le monde": "Le Monde",
    "the information": "The Information",
    "stat news": "STAT News",
    "retail dive": "Retail Dive",
    "project syndicate": "Project Syndicate",
    "s&p global": "S&P Global",
    "msci": "MSCI",
    "xtxixty": "Твёрдые цифры",
    "russianmacro": "Russianmacro",
    "mmi_ru": "MMI",
    "solidfin": "Solid Financial",
    "fom": "ФОМ",
    "wciom": "ВЦИОМ",
    "левада": "Левада-Центр"
}

def clean_source_name(name):
    if not name:
        return "Источник"
    
    name = re.sub(r'\.\s*Лента\s+новостей', '', name, flags=re.IGNORECASE)
    name = re.sub(r'(?i)\b(rss|feed|export|official|- Google News)\b', '', name)
    name = name.strip(' .-_')
    
    low = name.lower()
    for key, val in SOURCE_CLEAN_MAP.items():
        if key in low:
            return val
            
    return name if name else "Источник"

def fetch_rss():
    text_data = ""
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            raw_source_name = feed.feed.get('title', 'Источник')
            source_name = clean_source_name(raw_source_name)

            for entry in feed.entries[:2]:
                title = entry.title
                summary = getattr(entry, 'summary', '')
                
                if summary:
                    summary = BeautifulSoup(summary, 'html.parser').get_text(strip=True)
                
                link = getattr(entry, 'link', url)

                text_data += f"\nИсточник_Имя: {source_name}\nURL: {link}\nЗаголовок: {title}\nКонтекст: {summary[:400]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга RSS {url}: {e}")
    return text_data

def fetch_telegram_public():
    text_data = ""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    for channel in TG_CHANNELS:
        try:
            url = f"https://t.me/s/{channel}"
            res = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(res.text, 'html.parser')
            posts = soup.find_all('div', class_='tgme_widget_message_text', limit=3)
            
            clean_channel_name = clean_source_name(channel)
            
            for post in posts:
                text_data += f"\nИсточник_Имя: {clean_channel_name}\nURL: {url}\nКонтекст: {post.get_text(strip=True)[:400]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")
    return text_data

def generate_analytical_json(raw_data):
    prompt = f"""
    Ты — старший макроэкономический и отраслевой аналитик. Проанализируй входящий массив данных со всех мировых и российских СМИ и сформируй сжатый дайджест.

    ЖЕСТКИЕ ПРАВИЛА:
    1. ИТОГОВЫЙ ТЕКСТ В ПОЛЕ "summary_ru" ДОЛЖЕН БЫТЬ СТРОГО НА РУССКОМ ЯЗЫКЕ. Переводи все зарубежные материалы!
    2. Агрегируй новости: отбирай ТОЛЬКО самые важные макроэкономические сдвиги, решения регуляторов, геополитику, социологию и технологические тренды.
    3. Отсекай мелкий бытовой и криминальный шум.

    СТРУКТУРА JSON:
    {{
      "macro": [
        {{"summary_ru": "Развернутый тезис на русском", "source_name": "Имя Источника", "url": "URL"}}
      ],
      "geopolitics": [
        {{"summary_ru": "Развернутый тезис на русском", "source_name": "Имя Источника", "url": "URL"}}
      ],
      "industry": [
        {{"summary_ru": "Развернутый тезис на русском", "source_name": "Имя Источника", "url": "URL"}}
      ],
      "risks": [
        {{"summary_ru": "Развернутый тезис на русском", "source_name": "Имя Источника", "url": "URL"}}
      ]
    }}

    Массив данных:
    {raw_data}
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

def build_html_digest(raw_response):
    json_clean = clean_json_str(raw_response)
    try:
        data = json.loads(json_clean)
    except Exception as e:
        print(f"Ошибка парсинга JSON: {e}")
        return raw_response

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
                summary = item.get("summary_ru", "").strip()
                raw_src = item.get("source_name", "Источник")
                source = clean_source_name(raw_src)
                url = item.get("url", "#").strip()
                
                if summary:
                    html_output += f"• {summary} (<a href=\"{url}\">{source}</a>)\n"
            html_output += "\n"

    return html_output.strip()

def send_telegram_message(chat_id, text):
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except ApiTelegramException as e:
        print(f"Ошибка отправки HTML ({e}). Отправляем без разметки.")
        bot.send_message(chat_id, text)

if __name__ == "__main__":
    combined_data = fetch_rss() + "\n" + fetch_telegram_public()
    
    if combined_data.strip():
        raw_json = generate_analytical_json(combined_data)
        formatted_html = build_html_digest(raw_json)
        
        for i in range(0, len(formatted_html), 4000):
            send_telegram_message(CHAT_ID, formatted_html[i:i+4000])
