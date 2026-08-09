import os
import requests
import feedparser
from bs4 import BeautifulSoup
import telebot
from g4f.client import Client

# Инициализация бота
bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")

if not bot_token or not chat_id:
    raise ValueError("Ошибка: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не найдены!")

bot = telebot.TeleBot(bot_token)
CHAT_ID = chat_id
ai_client = Client()

# --- Списки источников ---
RSS_FEEDS = [
    "https://www.kommersant.ru/RSS/news.xml",
    "https://cbr.ru/rss/RssNews",
    "https://www.foreignaffairs.com/rss.xml",
    "https://www.cnews.ru/inc/rss/news.xml"
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
            for entry in feed.entries[:3]:
                title = entry.title
                summary = getattr(entry, 'summary', '')
                text_data += f"\n[RSS: {feed.feed.get('title', 'Источник')}]\nЗаголовок: {title}\nТекст: {summary[:300]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга RSS {url}: {e}")
    return text_data

def fetch_telegram_public():
    text_data = ""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    for channel in TG_CHANNELS:
        try:
            url = f"https://t.me/s/{channel}"
            res = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            posts = soup.find_all('div', class_='tgme_widget_message_text', limit=3)
            for post in posts:
                text_data += f"\n[Telegram: @{channel}]\nТекст: {post.get_text(strip=True)[:400]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")
    return text_data

def generate_analytical_digest(raw_data):
    prompt = f"""
    Ты — старший аналитик по макроэкономике и геополитике. 
    Проанализируй полученный массив сырых данных за 24 часа и составь жесткий, фактологический дайджест.

    Требования к форматированию:
    1. Игнорируй воду, эмоциональные заявления и развлекательный шум.
    2. Фокусируйся исключительно на: цифрах, решениях регуляторов, изменении законов, макроэкономических сдвигах и институциональных рисках.
    3. Разбей отчет строго по блокам:
       - 📊 МАКРОЭКОНОМИКА И ФИНАНСЫ
       - 🌍 ГЕОПОЛИТИКА И БЕЗОПАСНОСТЬ
       - 💼 ОТРАСЛЕВЫЕ ТРЕНДЫ И B2B
       - ⚠️ СКРЫТЫЕ РИСКИ (Что упускают массовые СМИ)
    4. Для каждого ключевого тезиса обязательно укажи источник и дай микро-вывод "Что это значит для рынка".

    Вот массив данных:
    {raw_data}
    """
    
    response = ai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

if __name__ == "__main__":
    combined_data = fetch_rss() + "\n" + fetch_telegram_public()
    
    if combined_data.strip():
        digest = generate_analytical_digest(combined_data)
        for i in range(0, len(digest), 4000):
            bot.send_message(CHAT_ID, digest[i:i+4000])
