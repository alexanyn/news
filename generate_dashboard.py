"""
generate_dashboard.py — читает METRICS_FILE (metrics.jsonl) и, если есть,
rss_health.json, генерирует ОДИН самодостаточный dashboard.html для
визуального контроля состояния пайплайна дайджеста.

ДОБАВЛЕНО 2026-08-27. Запускается как ДОПОЛНИТЕЛЬНЫЙ, НЕ блокирующий шаг
после основной отправки дайджеста (см. шаг "Generate dashboard" в daily.yml,
continue-on-error) — если генерация упадёт по любой причине, это не должно
мешать уже отправленному дайджесту или коммиту истории. Скрипт только
ЧИТАЕТ существующие файлы и пишет dashboard.html — можно безопасно запускать
и вручную/локально в любой момент.

Дизайн: тёмная приборная панель в духе терминала для мониторинга новостной
ленты (в духе Bloomberg Terminal / телетайпной wire-комнаты) — тематически
соответствует тому, что дашборд буквально мониторит: автоматический
новостной wire. Янтарный — основной сигнал/мир, циан — второй сигнал/Россия,
красный — только неисправности. IBM Plex Mono для чисел (табличное
начертание цифр важно для рядов метрик), IBM Plex Sans для текста.
"""
import json
import os
from datetime import datetime, timezone, timedelta

METRICS_FILE = "metrics.jsonl"
HEALTH_FILE = "rss_health.json"
OUTPUT_FILE = "dashboard.html"
MOSCOW_TZ = timezone(timedelta(hours=3))
MAX_CHART_RUNS = 40
MAX_TABLE_ROWS = 25

CATEGORY_LABELS = {
    "geopolitics": "Геополитика",
    "economics": "Экономика",
    "business": "Бизнес",
    "technology": "Технологии",
    "energy": "Энергетика",
    "security": "Безопасность",
    "pr": "PR",
}

STATUS_META = {
    "sent": ("Отправлен", "ok"),
    "empty_no_candidates": ("Пусто (нет кандидатов)", "warn"),
    "empty_no_digest_items": ("Пусто (Gemini)", "warn"),
    "send_failed": ("Сбой отправки", "bad"),
    "crashed": ("Крэш", "bad"),
    "unknown": ("Неизвестно", "warn"),
}


def load_metrics():
    """Построчно читает METRICS_FILE. Битые строки (неполный JSON, обрыв
    записи и т.п.) пропускаются молча — одна повреждённая строка не должна
    ронять генерацию всего дашборда."""
    if not os.path.exists(METRICS_FILE):
        return []
    entries = []
    with open(METRICS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def load_health():
    if not os.path.exists(HEALTH_FILE):
        return None
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _parse_ts(entry):
    raw = entry.get("run_started_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _total_items(entry):
    return entry.get("digest", {}).get("total_items", 0) if entry.get("digest") else 0


def build_summary(entries):
    now = datetime.now(MOSCOW_TZ)
    week_ago = now - timedelta(days=7)
    recent = [e for e in entries if (_parse_ts(e) or now) >= week_ago]

    total = len(recent)
    sent = [e for e in recent if e.get("status") == "sent"]
    sent_count = len(sent)
    success_rate = round(100 * sent_count / total) if total else None

    avg_items = round(sum(_total_items(e) for e in sent) / len(sent), 1) if sent else None
    latencies = [e["gemini"]["latency_sec"] for e in sent if e.get("gemini", {}).get("latency_sec") is not None]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None

    last_entry = entries[-1] if entries else None
    last_status = last_entry.get("status") if last_entry else None

    return {
        "total": total,
        "sent_count": sent_count,
        "success_rate": success_rate,
        "avg_items": avg_items,
        "avg_latency": avg_latency,
        "last_entry": last_entry,
        "last_status": last_status,
    }


def build_chart_data(entries):
    tail = entries[-MAX_CHART_RUNS:]
    labels, items, is_sent = [], [], []
    for e in tail:
        ts = _parse_ts(e)
        label = ts.strftime("%d.%m %H:%M") if ts else "?"
        labels.append(label)
        items.append(_total_items(e) if e.get("status") == "sent" else 0)
        is_sent.append(e.get("status") == "sent")
    return {"labels": labels, "items": items, "is_sent": is_sent}


def build_category_data(entries):
    now = datetime.now(MOSCOW_TZ)
    week_ago = now - timedelta(days=7)
    recent_sent = [
        e for e in entries
        if e.get("status") == "sent" and (_parse_ts(e) or now) >= week_ago and e.get("digest")
    ]
    world = {cat: 0 for cat in CATEGORY_LABELS}
    russia = {cat: 0 for cat in CATEGORY_LABELS}
    for e in recent_sent:
        by_cat = e["digest"].get("by_category", {})
        for cat in CATEGORY_LABELS:
            counts = by_cat.get(cat, {})
            world[cat] += counts.get("world", 0)
            russia[cat] += counts.get("russia", 0)
    return {
        "labels": [CATEGORY_LABELS[c] for c in CATEGORY_LABELS],
        "world": [world[c] for c in CATEGORY_LABELS],
        "russia": [russia[c] for c in CATEGORY_LABELS],
    }


def build_table_rows(entries):
    tail = list(reversed(entries[-MAX_TABLE_ROWS:]))
    rows = []
    for e in tail:
        ts = _parse_ts(e)
        label, css = STATUS_META.get(e.get("status"), STATUS_META["unknown"])
        errors = "; ".join(e.get("errors", [])) or "—"
        rows.append({
            "time": ts.strftime("%d.%m %H:%M") if ts else "?",
            "schedule": e.get("schedule", "?"),
            "status_label": label,
            "status_css": css,
            "items": _total_items(e) if e.get("status") == "sent" else "—",
            "duration": e.get("duration_sec"),
            "errors": errors,
        })
    return rows


def render_html(entries, health):
    summary = build_summary(entries)
    chart_data = build_chart_data(entries)
    category_data = build_category_data(entries)
    table_rows = build_table_rows(entries)
    generated_at = datetime.now(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S МСК")

    dot_css = {"sent": "ok", None: "warn"}.get(summary["last_status"], "warn")
    if summary["last_status"] in ("send_failed", "crashed"):
        dot_css = "bad"
    elif summary["last_status"] == "sent":
        dot_css = "ok"
    else:
        dot_css = "warn"

    def fmt(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "—"

    rows_html = ""
    if table_rows:
        for r in table_rows:
            duration = f"{r['duration']:.0f}с" if isinstance(r["duration"], (int, float)) else "—"
            rows_html += f"""
            <tr>
              <td class="mono">{r['time']}</td>
              <td class="mono">{r['schedule']}</td>
              <td><span class="tag tag-{r['status_css']}">{r['status_label']}</span></td>
              <td class="mono num">{r['items']}</td>
              <td class="mono num">{duration}</td>
              <td class="dim">{r['errors']}</td>
            </tr>"""
    else:
        rows_html = '<tr><td colspan="6" class="dim empty-row">Пока нет ни одной записи в metrics.jsonl — появится после первого прогона.</td></tr>'

    health_html = ""
    if health:
        rss = health.get("rss", {})
        tg = health.get("telegram", {})

        def source_list(details, status, limit=12):
            items = [d for d in details if d.get("status") == status][:limit]
            if not items:
                return '<div class="dim">нет</div>'
            return "".join(
                f'<div class="src-row"><span class="mono">{d.get("source_name", "?")}</span>'
                f'<span class="dim"> — {d.get("error") or (str(d.get("latest_entry_age_days","")) + " дн. назад")}</span></div>'
                for d in items
            )

        health_html = f"""
        <section class="panel">
          <h2><span class="eyebrow">04</span> Здоровье источников <span class="dim mono small">· проверено {health.get('checked_at', '?')[:16].replace('T',' ')}</span></h2>
          <div class="health-grid">
            <div class="health-col">
              <div class="health-stat"><span class="mono big">{rss.get('ok',0)}/{rss.get('total',0)}</span><span class="dim">RSS в норме</span></div>
              <h3 class="mini-h bad">Мертвы ({rss.get('dead',0)})</h3>
              {source_list(rss.get('details', []), 'dead')}
              <h3 class="mini-h warn">Протухли ({rss.get('stale',0)})</h3>
              {source_list(rss.get('details', []), 'stale')}
            </div>
            <div class="health-col">
              <div class="health-stat"><span class="mono big">{tg.get('ok',0)}/{tg.get('total',0)}</span><span class="dim">Telegram в норме</span></div>
              <h3 class="mini-h bad">Мертвы ({tg.get('dead',0)})</h3>
              {source_list(tg.get('details', []), 'dead')}
              <h3 class="mini-h warn">Протухли ({tg.get('stale',0)})</h3>
              {source_list(tg.get('details', []), 'stale')}
            </div>
          </div>
        </section>"""
    else:
        health_html = """
        <section class="panel">
          <h2><span class="eyebrow">04</span> Здоровье источников</h2>
          <div class="dim">rss_health.json ещё не создан — появится после первого запуска еженедельной проверки (см. rss_health.yml).</div>
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Пульт дайджеста — мониторинг</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js"></script>
<style>
  :root {{
    --ink: #0B0E14;
    --panel: #131822;
    --panel-2: #171D29;
    --line: #262D3B;
    --paper: #EDEAE2;
    --paper-dim: #8993A6;
    --amber: #F0A83C;
    --cyan: #52B8C4;
    --red: #E15241;
  }}
  * {{ box-sizing: border-box; }}
  html {{ background: var(--ink); }}
  body {{
    margin: 0;
    background: var(--ink);
    color: var(--paper);
    font-family: 'IBM Plex Sans', sans-serif;
    -webkit-font-smoothing: antialiased;
    padding: 0 0 64px 0;
  }}
  .mono {{ font-family: 'IBM Plex Mono', monospace; }}
  .dim {{ color: var(--paper-dim); }}
  .small {{ font-size: 12px; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}

  header.masthead {{
    border-bottom: 1px solid var(--line);
    padding: 28px 32px 24px 32px;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }}
  header.masthead .title-block {{ display: flex; align-items: center; gap: 14px; }}
  .status-dot {{
    width: 11px; height: 11px; border-radius: 50%;
    display: inline-block;
    box-shadow: 0 0 0 4px rgba(240,168,60,0.12);
  }}
  .status-dot.ok {{ background: var(--amber); box-shadow: 0 0 0 4px rgba(240,168,60,0.16); animation: pulse 2.4s ease-in-out infinite; }}
  .status-dot.warn {{ background: #C9A227; box-shadow: 0 0 0 4px rgba(201,162,39,0.16); }}
  .status-dot.bad {{ background: var(--red); box-shadow: 0 0 0 4px rgba(225,82,65,0.18); }}
  @media (prefers-reduced-motion: reduce) {{ .status-dot.ok {{ animation: none; }} }}
  @keyframes pulse {{
    0%, 100% {{ box-shadow: 0 0 0 4px rgba(240,168,60,0.16); }}
    50% {{ box-shadow: 0 0 0 8px rgba(240,168,60,0.05); }}
  }}
  h1 {{
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
    font-size: 18px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin: 0;
  }}
  header.masthead .meta {{ color: var(--paper-dim); font-size: 13px; text-align: right; }}

  .strip {{
    display: flex;
    border-bottom: 1px solid var(--line);
  }}
  .strip .cell {{
    flex: 1 1 0;
    padding: 22px 28px;
    border-right: 1px solid var(--line);
  }}
  .strip .cell:last-child {{ border-right: none; }}
  .strip .cell .big {{
    display: block;
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 700;
    font-size: 30px;
    color: var(--amber);
    line-height: 1.1;
  }}
  .strip .cell .label {{
    display: block;
    margin-top: 6px;
    font-size: 12px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--paper-dim);
  }}

  main {{ max-width: 1180px; margin: 0 auto; padding: 0 32px; }}
  section.panel {{ padding: 40px 0; border-bottom: 1px solid var(--line); }}
  section.panel:last-child {{ border-bottom: none; }}
  h2 {{
    font-size: 15px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 0 0 20px 0;
    display: flex;
    align-items: baseline;
    gap: 10px;
  }}
  .eyebrow {{
    font-family: 'IBM Plex Mono', monospace;
    color: var(--amber);
    font-size: 13px;
  }}
  .mini-h {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; margin: 16px 0 6px 0; }}
  .mini-h.bad {{ color: var(--red); }}
  .mini-h.warn {{ color: #C9A227; }}

  .chart-wrap {{ background: var(--panel); border: 1px solid var(--line); border-radius: 4px; padding: 20px; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead th {{
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--paper-dim);
    font-weight: 500;
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
  }}
  thead th.num {{ text-align: right; }}
  tbody td {{ padding: 9px 10px; border-bottom: 1px solid var(--line); }}
  tbody tr:hover {{ background: var(--panel); }}
  .empty-row {{ text-align: center; padding: 24px !important; }}

  .tag {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 3px;
    border: 1px solid transparent;
  }}
  .tag-ok {{ color: var(--amber); border-color: rgba(240,168,60,0.35); background: rgba(240,168,60,0.08); }}
  .tag-warn {{ color: #D8B948; border-color: rgba(216,185,72,0.35); background: rgba(216,185,72,0.08); }}
  .tag-bad {{ color: var(--red); border-color: rgba(225,82,65,0.4); background: rgba(225,82,65,0.1); }}

  .health-grid {{ display: flex; gap: 32px; }}
  .health-grid .health-col {{ flex: 1 1 0; min-width: 0; }}
  @media (max-width: 860px) {{ .health-grid {{ flex-direction: column; }} }}
  .health-stat {{ margin-bottom: 4px; }}
  .health-stat .big {{ font-family: 'IBM Plex Mono', monospace; font-size: 22px; color: var(--cyan); margin-right: 8px; }}
  .src-row {{ font-size: 13px; padding: 3px 0; }}

  footer {{ max-width: 1180px; margin: 0 auto; padding: 24px 32px 0 32px; color: var(--paper-dim); font-size: 12px; }}
</style>
</head>
<body>

<header class="masthead">
  <div class="title-block">
    <span class="status-dot {dot_css}"></span>
    <h1>Пульт дайджеста · Мониторинг пайплайна</h1>
  </div>
  <div class="meta">Обновлено {generated_at}</div>
</header>

<div class="strip">
  <div class="cell">
    <span class="big mono">{fmt(summary['total'])}</span>
    <span class="label">Прогонов за 7 дней</span>
  </div>
  <div class="cell">
    <span class="big mono">{fmt(summary['success_rate'], '%')}</span>
    <span class="label">Успешно отправлено</span>
  </div>
  <div class="cell">
    <span class="big mono">{fmt(summary['avg_items'])}</span>
    <span class="label">Ср. новостей / дайджест</span>
  </div>
  <div class="cell">
    <span class="big mono">{fmt(summary['avg_latency'], 'с')}</span>
    <span class="label">Ср. задержка Gemini</span>
  </div>
</div>

<main>

  <section class="panel">
    <h2><span class="eyebrow">01</span> Объём дайджестов во времени</h2>
    <div class="chart-wrap"><canvas id="itemsChart" height="80"></canvas></div>
  </section>

  <section class="panel">
    <h2><span class="eyebrow">02</span> Категории за последние 7 дней <span class="dim mono small">· мир / Россия</span></h2>
    <div class="chart-wrap"><canvas id="categoryChart" height="140"></canvas></div>
  </section>

  <section class="panel">
    <h2><span class="eyebrow">03</span> Последние прогоны</h2>
    <table>
      <thead>
        <tr>
          <th>Время</th><th>Расписание</th><th>Статус</th>
          <th class="num">Новостей</th><th class="num">Длительность</th><th>Ошибки</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>
  </section>

  {health_html}

</main>

<footer>metrics.jsonl · генерируется автоматически после каждого прогона, см. generate_dashboard.py</footer>

<script>
  const inkGrid = '#262D3B';
  const paperDim = '#8993A6';
  const amber = '#F0A83C';
  const cyan = '#52B8C4';
  const monoFont = "'IBM Plex Mono', monospace";

  Chart.defaults.color = paperDim;
  Chart.defaults.font.family = monoFont;
  Chart.defaults.font.size = 11;

  new Chart(document.getElementById('itemsChart'), {{
    type: 'line',
    data: {{
      labels: {json.dumps(chart_data['labels'], ensure_ascii=False)},
      datasets: [{{
        label: 'Новостей в дайджесте',
        data: {json.dumps(chart_data['items'])},
        borderColor: amber,
        backgroundColor: 'rgba(240,168,60,0.08)',
        pointRadius: 2,
        pointBackgroundColor: amber,
        borderWidth: 2,
        fill: true,
        tension: 0.25,
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ grid: {{ color: inkGrid }}, ticks: {{ maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }} }},
        y: {{ grid: {{ color: inkGrid }}, beginAtZero: true }}
      }}
    }}
  }});

  new Chart(document.getElementById('categoryChart'), {{
    type: 'bar',
    data: {{
      labels: {json.dumps(category_data['labels'], ensure_ascii=False)},
      datasets: [
        {{ label: 'Мир', data: {json.dumps(category_data['world'])}, backgroundColor: amber }},
        {{ label: 'Россия', data: {json.dumps(category_data['russia'])}, backgroundColor: cyan }}
      ]
    }},
    options: {{
      responsive: true,
      indexAxis: 'y',
      plugins: {{ legend: {{ position: 'top', align: 'end', labels: {{ boxWidth: 12, usePointStyle: true, pointStyle: 'circle' }} }} }},
      scales: {{
        x: {{ grid: {{ color: inkGrid }}, beginAtZero: true, stacked: true }},
        y: {{ grid: {{ display: false }}, stacked: true }}
      }}
    }}
  }});
</script>

</body>
</html>"""
    return html


def main():
    entries = load_metrics()
    health = load_health()
    html = render_html(entries, health)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Дашборд собран: {OUTPUT_FILE} ({len(entries)} записей метрик"
          f"{', есть данные о здоровье источников' if health else ''})")


if __name__ == "__main__":
    main()
