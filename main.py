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

# Жесткий маппинг имен источников для чистых ссылок
SOURCE_CLEAN_MAP = {
    "foreign affairs rss": "Foreign Affairs",
    "fa rss": "Foreign Affairs",
    "cfr rss": "CFR",
    "csis rss": "CSIS",
    "pew research center": "Pew Research",
    "cnews.ru": "CNews.ru",
    "коммерсантъ": "Коммерсантъ",
    "банк россии": "Банк России"
}

def clean_source_name(name):
    """Принудительная очистка имен источников на уровне Python"""
    low_name = name.lower().strip()
    for key, val in SOURCE_CLEAN_MAP.items():
        if key in low_name:
            return val
    cleaned = re.sub(r'(?i)\b(rss|feed)\b', '', name).strip()
    return cleaned if cleaned else "Источник"

def fetch_content_via_jina(url):
    """Обходит блокировки и вытаскивает текст статьи через Jina Reader"""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(jina_url, headers=headers, timeout=10)
        if res.status_code == 200:
            clean_text = res.text.strip()
            if "Markdown Content:" in clean_text:
                clean_text = clean_text.split("Markdown Content:")[1]
            return clean_text[:700].replace('\n', ' ')
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
                
                # Достаем контекст статьи через Jina, если summary пустое/короткое
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
    # Принуждаем модель отдавать чистый JSON
    prompt = f"""
    Ты — аналитик. Проанализируй данные и верни результат СТРОГО в формате JSON.

    ТРЕБОВАНИЯ К ДАННЫМ В JSON:
    1. Переводи ВСЕ английские заголовки и контекст на РУССКИЙ ЯЗЫК.
    2. В поле "summary_ru" пиши развернутую суть события на русском языке. Запрещено выводить только сырой заголовок вроде "China's Legal Weapon"!
    3. Структура JSON должна иметь строго 4 ключа: "macro", "geopolitics", "industry", "risks".

    ПРИМЕР ВЫХОДНОГО JSON:
    {{
      "macro": [
        {{
          "summary_ru": "СПБ Биржа планирует запустить торги производными инструментами на заблокированные активы",
          "source_name": "Коммерсантъ",
          "url": "https://..."
        }}
      ],
      "geopolitics": [
        {{
          "summary_ru": "Пекин формирует собственную нормативно-правовую базу для защиты китайских компаний от санкций США",
          "source_name": "Foreign Affairs",
          "url": "https://..."
        }}
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

def build_html_digest(json_str):
    """Сборка HTML-сообщения на стороне Python без участия нейросети"""
    try:
        data = json.loads(json_str)
    except Exception as e:
        print(f"Ошибка парсинга JSON от модели: {e}")
        return json_str

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
                source = clean_source_name(item.get("source_name", "Источник"))
                url = item.get("url", "#").strip()
                html_output += f"• {summary} (<a href=\"{url}\">{source}</a>)\n"
            html_output += "\n"

    return html_output.strip()

def send_telegram_message(chat_id, text):
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except ApiTelegramException as e:
        print(f"Ошибка HTML-парсеру Telegram ({e}). Отправка без разметки.")
        bot.send_message(chat_id, text)

if __name__ == "__main__":
    combined_data = fetch_rss() + "\n" + fetch_telegram_public()
    
    if combined_data.strip():
        raw_json = generate_analytical_json(combined_data)
        formatted_html = build_html_digest(raw_json)
        
        for i in range(0, len(formatted_html), 4000):
            send_telegram_message(CHAT_ID, formatted_html[i:i+4000])
