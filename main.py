import os
import requests
import feedparser
from bs4 import BeautifulSoup
import telebot
from telebot.apihelper import ApiTelegramException

# 1. Считывание переменных окружения
groq_api_key = os.environ.get("GROQ_API_KEY")
bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")

if not groq_api_key or not bot_token or not chat_id:
    raise ValueError("Ошибка: Проверьте GROQ_API_KEY, TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Secrets!")

bot = telebot.TeleBot(bot_token)
CHAT_ID = chat_id

# 2. Источники данных
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
            res = requests.get(url, headers=headers, timeout=15)
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

    Требования к анализу и форматированию:
    1. Игнорируй воду, эмоциональные заявления и развлекательный шум.
    2. Фокусируйся исключительно на: цифрах, решениях регуляторов, изменении законов, макроэкономических сдвигах и институциональных рисках.
    3. Разбей отчет строго по блокам:
       - 📊 МАКРОЭКОНОМИКА И ФИНАНСЫ
       - 🌍 ГЕОПОЛИТИКА И БЕЗОПАСНОСТЬ
       - 💼 ОТРАСЛЕВЫЕ ТРЕНДЫ И B2B
       - ⚠️ СКРЫТЫЕ РИСКИ (Что упускают массовые СМИ)
    4. Для каждого ключевого тезиса дай предельно конкретный вывод.
       СТРОГО ЗАПРЕЩЕНО использовать вводные слова и формулировки-паразиты: "может", "потенциально", "оказать влияние", "улучшить ситуацию", "привести к эскалации".
       Пиши строго сухие факты, финансовую математику и прямые последствия для рынка.

    Вот массив данных:
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

def send_telegram_message(chat_id, text):
    # Попытка отправки с Markdown, при ошибке синтаксиса — отсылает чистым текстом
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown")
    except ApiTelegramException as e:
        print(f"Предупреждение: ошибка разметки Markdown ({e}), отправка обычным текстом.")
        bot.send_message(chat_id, text)

if __name__ == "__main__":
    combined_data = fetch_rss() + "\n" + fetch_telegram_public()
    
    if combined_data.strip():
        digest = generate_analytical_digest(combined_data)
        
        for i in range(0, len(digest), 4000):
            send_telegram_message(CHAT_ID, digest[i:i+4000])
