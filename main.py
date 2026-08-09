import os
import requests
import feedparser
from bs4 import BeautifulSoup
import telebot
from google import genai

# 1. Считываем переменные окружения и проверяем их наличие
api_key = os.environ.get("GEMINI_API_KEY")
bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")

if not api_key or not bot_token or not chat_id:
    raise ValueError("Ошибка: Одно или несколько обязательных секретов (GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) не найдены в GitHub Secrets!")

# 2. Инициализация клиентов
client = genai.Client(api_key=api_key)
bot = telebot.TeleBot(bot_token)
CHAT_ID = chat_id

# 3. Список RSS-источников
RSS_FEEDS = [
    "https://www.kommersant.ru/RSS/news.xml",
    "https://cbr.ru/rss/RssNews",
    "https://www.foreignaffairs.com/rss.xml",
    "https://www.cnews.ru/inc/rss/news.xml"
]

# 4. Список Telegram-каналов (без знака @)
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
            res = requests.get(url, headers=headers, timeout=20)
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
    
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents=prompt
    )
    return response.text

if __name__ == "__main__":
    combined_data = fetch_rss() + "\n" + fetch_telegram_public()
    
    if combined_data.strip():
        digest = generate_analytical_digest(combined_data)
        
        # Разбивка сообщения по 4000 символов под лимит Telegram
        for i in range(0, len(digest), 4000):
            bot.send_message(CHAT_ID, digest[i:i+4000])
