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

# 2. Источники (RSS)
RSS_FEEDS = [
    # СМИ и Институты РФ
    "https://www.kommersant.ru/RSS/news.xml",
    "https://cbr.ru/rss/RssNews",
    "https://www.cnews.ru/inc/rss/news.xml",
    "https://fom.ru/rss.xml",
    "https://wciom.ru/rss.xml",
    "https://www.levada.ru/feed/",
    # Зарубежные аналитические центры
    "https://www.foreignaffairs.com/rss.xml",
    "https://www.pewresearch.org/feed/",
    "https://www.cfr.org/rss.xml",
    "https://www.csis.org/rss/all",
    "https://www.bruegel.org/rss.xml",
    "https://carnegieendowment.org/rss/solr/publications"
]

# 3. Публичные Telegram-каналы (без символа @)
TG_CHANNELS = [
    "mmi_ru",
    "solidfin",
    "xtxixty",
    "russianmacro"
]

# 4. Карта автозамены имен источников
SOURCE_CLEAN_MAP = {
    "foreign affairs": "Foreign Affairs",
    "fa rss": "Foreign Affairs",
    "cfr": "CFR",
    "csis": "CSIS",
    "pew research": "Pew Research",
    "cnews": "CNews.ru",
    "коммерсант": "Коммерсантъ",
    "банк россии": "Банк России",
    "cbr": "Банк России",
    "xtxixty": "Твёрдые цифры",
    "russianmacro": "Russianmacro",
    "mmi_ru": "MMI",
    "solidfin": "Solid Financial",
    "fom": "ФОМ",
    "фом": "ФОМ",
    "wciom": "ВЦИОМ",
    "вциом": "ВЦИОМ",
    "левада": "Левада-Центр",
    "levada": "Левада-Центр",
    "mediascope": "Mediascope",
    "forecast": "ЦМАКП",
    "прогноз": "ЦМАКП",
    "yakov": "Яков и Партнёры",
    "яков": "Яков и Партнёры",
    "eaeunion": "ЕЭК",
    "еэк": "ЕЭК"
}

def clean_source_name(name):
    """Очистка суффиксов и нормализация имен источников"""
    if not name:
        return "Источник"
    
    name = re.sub(r'\.\s*Лента\s+новостей', '', name, flags=re.IGNORECASE)
    name = re.sub(r'(?i)\b(rss|feed)\b', '', name)
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

            for entry in feed.entries[:3]:
                title = entry.title
                summary = getattr(entry, 'summary', '')
                
                if summary:
                    summary = BeautifulSoup(summary, 'html.parser').get_text(strip=True)
                
                link = getattr(entry, 'link', url)

                text_data += f"\nИсточник_Имя: {source_name}\nURL: {link}\nЗаголовок: {title}\nКонтекст: {summary[:500]}\n---"
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
    Ты — макроэкономический и социологический аналитик. Проанализируй данные и верни результат ИСКЛЮЧИТЕЛЬНО в формате JSON.

    ЖЕСТКИЕ ПРАВИЛА:
    1. ВЕСЬ ТЕКСТ В ПОЛЕ "summary_ru" ДОЛЖЕН БЫТЬ СТРОГО НА РУССКОМ ЯЗЫКЕ.
    2. Переводи смысл зарубежных исследований, отчетов и англоязычных постов.
    3. Фильтруй информационный шум. Включай только значимые социологические тренды, макроэкономику, решения регуляторов и B2B-события.

    СТРУКТУРА JSON:
    {{
      "macro": [
        {{"summary_ru": "Развернутый тезис на русском", "source_name": "Имя Источника", "url": "URL"}}
      ],
      "geopolitics": [
        {{"summary_ru": "Развернутый тезис на русском", "source_name": "Имя Источника", "url": "URL"}}
      ],
      "industry": [],
      "risks": []
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
        timeout=60
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
    
    if combined_
