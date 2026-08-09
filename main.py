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

def clean_source_name(name):
    """Жесткая зачистка мусорных суффиксов в именах источников"""
    if not name:
        return "Источник"
    
    # Регулярные выражения для удаления хвостов
    name = re.sub(r'\.\s*Лента\s+новостей', '', name, flags=re.IGNORECASE)
    name = re.sub(r'(?i)\b(rss|feed)\b', '', name)
    name = name.strip(' .-_')
    
    low = name.lower()
    if 'foreign' in low or low == 'fa':
        return 'Foreign Affairs'
    if 'коммерсант' in low:
        return 'Коммерсантъ'
    if 'cnews' in low:
        return 'CNews.ru'
    if 'банк россии' in low or 'cbr' in low:
        return 'Банк России'
    if 'cfr' in low:
        return 'CFR'
    if 'csis' in low:
        return 'CSIS'
    if 'pew' in low:
        return 'Pew Research'
        
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
            for post in posts:
                text_data += f"\nИсточник_Имя: Telegram @{channel}\nURL: {url}\nКонтекст: {post.get_text(strip=True)[:400]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")
    return text_data

def generate_analytical_json(raw_data):
    prompt = f"""
    Ты — профессиональный международный аналитик. Проанализируй данные и верни результат ИСКЛЮЧИТЕЛЬНО в формате JSON.

    ЖЕСТКИЕ ПРАВИЛА ТРАНСЛЯЦИИ:
    1. ВЕСЬ ТЕКСТ В ПОЛЕ "summary_ru" ДОЛЖЕН БЫТЬ СТРОГО НА РУССКОМ ЯЗЫКЕ.
    2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО оставлять английские заголовки как есть (например, "China’s Legal Weapon" или "After Putin")! 
       Если в контексте есть только английский заголовок, переведи его смысл на русский язык и напиши развернутый тезис.
       ПРИМЕР: Вместо "China’s Legal Weapon" пиши "Китай формирует собственную нормативно-правовую базу для противодействия санкциям США".
    3. Игнорируй бытовые и мелкие криминальные происшествия (взрывы коробок, бытовые несчастные случаи). Оставляй только макроэкономику, политику, B2B и технологические риски.

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
    
    if combined_data.strip():
        raw_json = generate_analytical_json(combined_data)
        formatted_html = build_
