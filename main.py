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

def fetch_content_via_jina(url):
    """Обходит Cloudflare и пайволы через Jina Reader API, вытаскивая чистый текст"""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(jina_url, headers=headers, timeout=10)
        if res.status_code == 200:
            # Берем первые 600 символов чистого текста статьи
            clean_text = res.text.strip()
            # Убираем служебный заголовок Jina
            if "Markdown Content:" in clean_text:
                clean_text = clean_text.split("Markdown Content:")[1]
            return clean_text[:600].replace('\n', ' ')
    except Exception as e:
        print(f"Ошибка Jina Reader для {url}: {e}")
    return ""

def fetch_rss():
    text_data = ""
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            # Чистим техническое имя источника
            raw_source_name = feed.feed.get('title', 'Источник')
            source_name = raw_source_name.replace(" RSS", "").replace("Feed", "").strip()

            for entry in feed.entries[:3]:
                title = entry.title
                summary = getattr(entry, 'summary', '')
                
                if summary:
                    summary = BeautifulSoup(summary, 'html.parser').get_text(strip=True)
                
                link = getattr(entry, 'link', url)
                
                # Если анонс пустой или короткий (как у Foreign Affairs), вытаскиваем текст через Jina
                if len(summary) < 50 and link:
                    jina_text = fetch_content_via_jina(link)
                    if jina_text:
                        summary = jina_text

                text_data += f"\nИсточник_Имя: {source_name}\nURL: {link}\nЗаголовок: {title}\nТекст: {summary[:500]}\n---"
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
                text_data += f"\nИсточник_Имя: Telegram @{channel}\nURL: {url}\nТекст: {post.get_text(strip=True)[:400]}\n---"
        except Exception as e:
            print(f"Ошибка парсинга TG @{channel}: {e}")
    return text_data

def generate_analytical_digest(raw_data):
    prompt = f"""
    Ты — старший аналитик по международным отношениям и экономике. Проанализируй данные и сформируй дайджест.

    ЖЕСТКИЕ ТРЕБОВАНИЯ К ТЕКСТУ:
    1. ИТОГОВЫЙ ТЕКСТ ДОЛЖЕН БЫТЬ 100% НА РУССКОМ ЯЗЫКЕ.
    2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО просто дублировать английские заголовки! 
       Даже если в источнике был только заголовок "China’s Legal Weapon", переведи его и напиши понятную развернутую суть:
       • Китай формирует собственную правовую систему для борьбы с юридическим давлением США (<a href="...">Foreign Affairs</a>).
    3. Запрещено использовать аббревиатуры вроде "FA RSS". Пиши нормальное имя: "Foreign Affairs", "CSIS", "Pew Research".

    СТРОГИЙ ФОРМАТ ВЫВОДА (Используй ТОЛЬКО HTML-теги: <b> и <a href="...">):

    <b>📊 МАКРОЭКОНОМИКА И ФИНАНСЫ</b>
    • Развернутая аналитическая суть новости на русском (<a href="URL">Имя Источника</a>)

    <b>🌍 ГЕОПОЛИТИКА И БЕЗОПАСНОСТЬ</b>
    • Развернутая аналитическая суть новости на русском (<a href="URL">Имя Источника</a>)

    <b>💼 ОТРАСЛЕВЫЕ ТРЕНДЫ И B2B</b>
    • Развернутая аналитическая суть новости на русском (<a href="URL">Имя Источника</a>)

    <b>⚠️ СКРЫТЫЕ РИСКИ</b>
    • Развернутая аналитическая суть новости на русском (<a href="URL">Имя Источника</a>)

    ПРАВИЛА:
    1. Заголовки блоков ОБЯЗАТЕЛЬНО оборачивай в <b>...</b>.
    2. Каждый пункт начинай СТРОГО с эмодзи-точки `• `.
    3. Ссылку оформляй строго в скобках: (<a href="URL">Источник</a>).

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
