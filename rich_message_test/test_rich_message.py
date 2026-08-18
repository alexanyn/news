"""
ИЗОЛИРОВАННЫЙ тест метода sendRichMessage (Telegram Bot API 10.1+, добавлен
июнь 2026). Никак не связан с основным main.py — не импортирует из него
ничего и не может повлиять на его работу. Единственная цель: один раз
отправить тестовое сообщение в канал и посмотреть, что ответит Telegram —
подтвердит ли она поддержку метода реальным, а не документационным способом.

Использует requests напрямую (в обход pyTelegramBotAPI, которая метод не
поддерживает) — тот же подход, каким пришлось бы пользоваться и в основном
скрипте, если решим переходить на этот метод.
"""
import os
import json
import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в переменных окружения")

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendRichMessage"

# Простой режим InputRichMessage — готовый HTML вместо структуры блоков.
# Специально беру то же форматирование (<b>), что уже используется в
# основном дайджесте, чтобы тест был максимально показателен.
test_html = (
    "<b>🧪 Тест Rich Message</b>\n\n"
    "Если вы видите это сообщение — метод <b>sendRichMessage</b> работает "
    "с вашим ботом и токеном.\n\n"
    "Это тестовое сообщение специально длиннее обычного лимита в 4096 "
    "символов не проверяет — просто подтверждает сам факт, что метод "
    "принят и сообщение доставлено. " + ("Проверка длины текста. " * 40)
)

payload = {
    "chat_id": CHAT_ID,
    "rich_message": {
        "html": test_html
    }
}

print(f"Длина тестового текста: {len(test_html)} символов")
print("Отправляю запрос к sendRichMessage...")

response = requests.post(url, json=payload, timeout=30)

print(f"\nHTTP-статус: {response.status_code}")
print("Ответ Telegram:")
print(json.dumps(response.json(), ensure_ascii=False, indent=2))

if response.status_code == 200 and response.json().get("ok"):
    print("\n✅ УСПЕХ: sendRichMessage работает, сообщение отправлено.")
else:
    print("\n❌ ОШИБКА: см. поле 'description' в ответе выше — там Telegram "
          "объясняет, что именно не так.")
