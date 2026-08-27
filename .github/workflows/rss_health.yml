name: RSS/Telegram Source Health Check

on:
  # ДОБАВЛЕНО 2026-08-27. В отличие от daily.yml (см. подробный комментарий
  # там про отказ от "schedule:" ради cron-job.org) — здесь встроенный
  # GitHub "schedule:" сознательно ОСТАВЛЕН. Причина отличается от daily.yml:
  # там опоздание/пропуск прогона на 86 минут ломает расписание дайджеста,
  # который читатель ждёт в конкретное время (09:00/12:00/...). Здесь же
  # прогон "иногда в течение дня в понедельник, +/- час-два" не имеет
  # никакого практического значения — источники не отваливаются за часы,
  # а сам отчёт rss_health.json никто не ждёт к конкретной минуте. Заводить
  # ради этого второй внешний cron-job.org триггер — не оправданная сложность.
  schedule:
    - cron: '0 6 * * 1'  # каждый понедельник, 06:00 UTC = 09:00 МСК
  workflow_dispatch: {}

jobs:
  health-check:
    runs-on: ubuntu-latest
    # Проверка ~104 RSS-лент + Telegram-каналов, параллельно (см.
    # MAX_FETCH_WORKERS в main.py), но каждая — со своим таймаутом в 20с и
    # возможными сетевыми задержками. 30 минут — щедрый запас; сама проверка
    # обычно займёт 2-5 минут, но лучше упасть по таймауту, чем зависнуть
    # на дефолтные 360 минут GitHub Actions при неожиданной сетевой проблеме.
    timeout-minutes: 30

    steps:
    - name: Checkout repository
      uses: actions/checkout@v4
      with:
        fetch-depth: 0

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.10'

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install requests feedparser beautifulsoup4

    - name: Run health check
      # ВАЖНО: rss_health_check.py импортирует RSS_FEEDS/TELEGRAM_CHANNELS и
      # функции сбора напрямую из main.py (см. комментарий в начале того
      # файла) — но main.py больше НЕ требует реальных GEMINI_API_KEY/
      # TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID на уровне импорта (см.
      # _require_runtime_env_vars в main.py, вызывается только из
      # if __name__ == "__main__", не при импорте) — поэтому здесь
      # намеренно НЕТ env: секции с секретами, они этому workflow не нужны.
      run: python rss_health_check.py

    - name: Commit health report
      run: |
        git config --local user.email "action@github.com"
        git config --local user.name "GitHub Action"
        # ИСПРАВЛЕНО 2026-08-27: см. аналогичный комментарий в daily.yml —
        # git add на несуществующий файл роняет всю команду целиком, поэтому
        # проверяем существование перед добавлением даже для одного файла
        # (rss_health_check.py пишет его безусловно при успешном завершении,
        # но лучше не полагаться на это молча).
        if [ -f rss_health.json ]; then
          git add rss_health.json
        fi
        git commit -m "Update RSS/Telegram health report [skip ci]" || echo "No changes to commit"
        git pull --rebase origin main
        git push
