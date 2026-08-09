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
    """Очищает суффиксы RSS и Feed из названий источников"""
    cleaned = re.sub(r'(?i)\b(rss|feed)\b', '', name)
    return cleaned.strip()

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
            # Возвращаем первые 700 символов
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
                
                # Если текст короче 100 символов или совпадает с заголовком, дотягиваем его через Jina
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

def generate_analytical_digest(raw_data):
    prompt = f"""
    Ты — международный макроэкономический аналитик. Проанализируй входящий массив данных и составь аналитический дайджест.

    КРИТИЧЕСКИЕ ПРАВИЛА (НАРУШЕНИЕ ЗАПРЕЩЕНО):
    1. ИТОГОВЫЙ ТЕКСТ ДОЛЖЕН БЫТЬ 100% НА РУССКОМ ЯЗЫКЕ.
    2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выводить сырые английские заголовки! 
       Каждая новость должна содержать осмысленный аналитический вывод на русском языке, объясняющий сути событий.
       ПРИМЕР ОШИБКИ: • China’s Legal Weapon (<a href="...">Foreign Affairs</a>)
       ПРИМЕР ПРАВИЛЬНОГО ВЫВОДА: • Пекин выстраивает собственную нормативно-правовую базу для защиты компаний от санкций США (<a href="...">Foreign Affairs</a>).
    3. Имя источника очищай от 'RSS' и 'Feed'. Пиши строго: "Foreign Affairs", "CSIS", "Pew Research", "Коммерсантъ".

    СТРОГИЙ HTML-ФОРМАТ ВЫВОДА:

    <b>📊 МАКРОЭКОНОМИКА И ФИНАНСЫ</b>
    • Детальный аналитический тезис на русском (<a href="URL">Имя Источника</a>)

    <b>🌍 ГЕОПОЛИТИКА И БЕЗОПАСНОСТЬ</b>
    • Детальный аналитический тезис на русском (<a href="URL">Имя Источника</a>)

    <b>💼 ОТРАСЛЕВЫЕ ТРЕНДЫ И B2B</b>
    • Детальный аналитический тезис на русском (<a href="URL">Имя Источника</a>)

    <b>⚠️ СКРЫТЫЕ РИСКИ</b>
    • Детальный аналитический тезис на русском (<a href="URL">Имя Источника</a>)

    Правила синтаксиса:
    - Заголовки блоков оборачивай только в <b>...</b>.
    - Пункты списков начинай строго с `• `.
    - Ссылка должна быть внутри скобок: (<a href="URL">Источник</a>).

    Входящие данные:
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
