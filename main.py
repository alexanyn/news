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

# 2. Источники
RSS_FEEDS = [
    "https://www.kommersant.ru/RSS/news.xml",
    "https://cbr.ru/rss/RssNews",
    "https://www.foreignaffairs.com/rss.xml",
    "https://www.cnews.ru/inc/rss/news.xml",
    "https://www.pewresearch.org/feed/",
    "https://www.cfr.org/rss.xml",
    "https://www.csis.org/rss/all",
    "https://www.bruegel.org/rss.xml",
    "https://carnegieendowment.org/rss/solr/publications"
]

TG_CHANNELS = [
    "mmi_ru",
    "solidfin"
]

SOURCE_CLEAN_MAP = {
    "foreign affairs": "Foreign Affairs",
    "fa rss": "Foreign Affairs",
    "cfr": "CFR",
    "csis": "CSIS",
    "pew research": "Pew Research",
    "cnews": "CNews.ru",
    "коммерсантъ": "Коммерсантъ",
    "банк россии": "Банк России"
}

def clean_source_name(name):
    low_name = name.lower().strip()
    for key, val in SOURCE_CLEAN_MAP.items():
        if key in low_name:
            return val
    cleaned = re.sub(r'(?i)\b(rss|feed)\b', '', name).strip()
    return cleaned if cleaned else "Источник"

def fetch_content_via_jina(url):
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(jina_url, headers=headers, timeout=15)
        if res.status_code == 200:
            clean_text = res.text.strip()
            if "Markdown Content:" in clean_text:
                clean_text = clean_text.split("Markdown Content:")[1]
            return clean_text[:800].replace('\n', ' ')
    except Exception as e:
        print(f"Ошибка Jina Reader для {url}: {e}")
    return ""

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
                
                if (not summary or len(summary) < 100 or summary.strip() == title.strip()) and link:
                    jina_text = fetch_content_via_jina(link)
                    if jina_text:
                        summary = jina_text

                text_data += f"\nИсточник_Имя: {source_name}\nURL: {link}\nЗаголовок: {title}\nКонтекст: {summary[:600]}\n---"
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
            for post in posts:
                text_data += f"\nИсточник_Имя: Telegram @{channel}\nURL: {url}\nКонтекст: {post.get_text(strip=True)[:400]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")
    return text_data

def generate_analytical_json(raw_data):
    prompt = f"""
    Ты — профессиональный международный аналитик. Проанализируй данные и верни результат ИСКЛЮЧИТЕЛЬНО в формате JSON.

    ЖЕСТКИЕ ПРАВИЛА:
    1. Весь текст внутри JSON ("summary_ru") ДОЛЖЕН БЫТЬ НА РУССКОМ ЯЗЫКЕ.
    2. Запрещено выводить оригинальные английские заголовки статей! Переводи смысл и пересказывай суть своими словами.
    3. Поле "source_name" должно содержать чистые имена: "Foreign Affairs", "CSIS", "Pew Research", "Коммерсантъ".

    СТРУКТУРА JSON (Строго соблюдай ключи):
    {{
      "macro": [
        {{"summary_ru": "Развернутая аналитическая суть новости на русском", "source_name": "Имя Источника", "url": "URL"}}
      ],
      "geopolitics": [
        {{"summary_ru": "Развернутая аналитическая суть новости на русском", "source_name": "Имя Источника", "url": "URL"}}
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
    """Очищает JSON от Markdown блоков ```json ... ```"""
    clean = raw_str.strip()
    if clean.startswith("```json"):
        clean = clean[7:]
    elif clean.startswith("```"):
        clean = clean[3:]
    if clean.endswith("```"):
        clean = clean[:-3]
    return clean.strip()

def build_html_digest(raw_response):
    """Безопасная сборка HTML из JSON"""
    json_clean = clean_json_str(raw_response)
    try:
        data = json.loads(json_clean)
    except Exception as e:
        print(f"Критическая ошибка парсинга JSON: {e}. Применение резервной очистки...")
        # Резервный регулярочный подчиститель на случай сбоя JSON
        clean_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', raw_response)
        clean_text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', clean_text)
        return clean_text

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
        print(f"Ошибка HTML-парсеру Telegram ({e}). Отправка обычным текстом.")
        bot.send_message(chat_id, text)

if __name__ == "__main__":
    combined_data = fetch_rss() + "\n" + fetch_telegram_public()
    
    if combined_data.strip():
        raw_json = generate_analytical_json(combined_data)
        formatted_html = build_html_digest(raw_json)
        
        for i in range(0, len(formatted_html), 4000):
            send_telegram_message(CHAT_ID, formatted_html[i:i+4000])
