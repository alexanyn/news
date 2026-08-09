import os
import re
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
    # Базовые источники
    "https://www.kommersant.ru/RSS/news.xml",
    "https://cbr.ru/rss/RssNews",
    "https://www.foreignaffairs.com/rss.xml",
    "https://www.cnews.ru/inc/rss/news.xml",
    # Новые валидные RSS-ленты аналитических центров
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

def fetch_rss():
    text_data = ""
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source_name = feed.feed.get('title', 'Источник')
            for entry in feed.entries[:3]:
                title = entry.title
                summary = getattr(entry, 'summary', '')
                link = getattr(entry, 'link', url)
                text_data += f"\nИсточник: {source_name}\nURL: {link}\nЗаголовок: {title}\nТекст: {summary[:300]}\n---"
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
                text_data += f"\nИсточник: Telegram @{channel}\nURL: {url}\nТекст: {post.get_text(strip=True)[:400]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")
    return text_data

def generate_analytical_digest(raw_data):
    prompt = f"""
    Ты — старший аналитик. Проанализируй данные и сформируй дайджест.

    СТРОГИЙ ФОРМАТ ВЫВОДА (Используй ТОЛЬКО эти HTML-теги: <b> и <a href="...">):

    <b>📊 МАКРОЭКОНОМИКА И ФИНАНСЫ</b>
    • Суть новости (<a href="URL">Имя Источника</a>)

    <b>🌍 ГЕОПОЛИТИКА И БЕЗОПАСНОСТЬ</b>
    • Суть новости (<a href="URL">Имя Источника</a>)

    <b>💼 ОТРАСЛЕВЫЕ ТРЕНДЫ И B2B</b>
    • Суть новости (<a href="URL">Имя Источника</a>)

    <b>⚠️ СКРЫТЫЕ РИСКИ</b>
    • Суть новости (<a href="URL">Имя Источника</a>)

    ПРАВИЛА:
    1. Заголовки блоков ОБЯЗАТЕЛЬНО оборачивай в <b>...</b>.
    2. Пункты начинай СТРОГО с эмодзи-точки `• `.
    3. Ссылку оформляй строго в скобках: (<a href="URL">Источник</a>). Никаких квадратных скобок `[]` или звездочек `**`.

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
        "temperature": 0.1
    }
    
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=60
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

def sanitize_html(text):
    # Автоматическая подчистка: если модель сбилась и выдала Markdown-заголовки
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)
    return text

def send_telegram_message(chat_id, text):
    clean_text = sanitize_html(text)
    try:
        bot.send_message(chat_id, clean_text, parse_mode="HTML", disable_web_page_preview=True)
    except ApiTelegramException as e:
        print(f"Ошибка HTML-парсеру Telegram ({e}). Отправка без разметки.")
        bot.send_message(chat_id, text)

if __name__ == "__main__":
    combined_data = fetch_rss() + "\n" + fetch_telegram_public()
    
    if combined_data.strip():
        digest = generate_analytical_digest(combined_data)
        
        for i in range(0, len(digest), 4000):
            send_telegram_message(CHAT_ID, digest[i:i+4000])
