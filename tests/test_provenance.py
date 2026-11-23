import json
import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "test")

import main  # noqa: E402


class ProvenanceValidationTests(unittest.TestCase):
    def setUp(self):
        self.news_db = {
            "0": {
                "url": "https://example.com/articles/icc",
                "title": "Tajikistan moves to leave ICC amid US push against court",
                "summary": "The ICC expressed concern after Tajikistan notified the UN.",
                "source_name": "Al Jazeera",
            },
            "1": {
                "url": "https://example.com/articles/mckinsey",
                "title": "McKinsey study finds vulnerable global imports",
                "summary": "A McKinsey study examines global goods imports and supply risks.",
                "source_name": "McKinsey",
            },
        }

    def validate(self, items):
        data, stats = main.validate_gemini_items({"economics": items}, self.news_db)
        return data["economics"], stats

    def test_rejects_wrong_source_claim(self):
        accepted, stats = self.validate([
            {"id": "0", "summary_ru": "Согласно исследованию McKinsey, около 23% мирового импорта уязвимы."}
        ])
        self.assertEqual(accepted, [])
        self.assertEqual(stats["rejected_provenance"], 1)

    def test_accepts_matching_summary(self):
        accepted, stats = self.validate([
            {"id": "0", "summary_ru": "Таджикистан начал процесс выхода из МУС после уведомления ООН."}
        ])
        self.assertEqual([item["id"] for item in accepted], ["0"])
        self.assertEqual(stats["accepted_items"], 1)

    def test_rejects_unknown_id_and_bad_id(self):
        accepted, stats = self.validate([
            {"id": "999", "summary_ru": "Новость"},
            {"id": "not-an-id", "summary_ru": "Новость"},
        ])
        self.assertEqual(accepted, [])
        self.assertEqual(stats["rejected_unknown_id"], 1)
        self.assertEqual(stats["rejected_bad_id"], 1)

    def test_rejects_duplicate_id_and_url(self):
        self.news_db["2"] = dict(self.news_db["1"])
        accepted, stats = self.validate([
            {"id": "1", "summary_ru": "Исследование McKinsey оценивает уязвимость импорта."},
            {"id": "1", "summary_ru": "Исследование McKinsey оценивает уязвимость импорта."},
            {"id": "2", "summary_ru": "Исследование McKinsey оценивает уязвимость импорта."},
        ])
        self.assertEqual([item["id"] for item in accepted], ["1"])
        self.assertEqual(stats["rejected_duplicate_id"], 1)
        self.assertEqual(stats["rejected_duplicate_url"], 1)

    def test_rejects_invalid_url(self):
        self.news_db["3"] = dict(self.news_db["0"], url="not-a-url")
        accepted, stats = self.validate([
            {"id": "3", "summary_ru": "Таджикистан начал процесс выхода из МУС."}
        ])
        self.assertEqual(accepted, [])
        self.assertEqual(stats["rejected_bad_url"], 1)

    def test_html_builder_does_not_fallback_rejected_items(self):
        raw = json.dumps({
            "economics": [{
                "id": "0",
                "summary_ru": "Согласно исследованию McKinsey, около 23% мирового импорта уязвимы.",
                "is_russia": False,
            }]
        }, ensure_ascii=False)
        result = main.build_html_digest(raw, self.news_db)
        stats = result[8]
        self.assertEqual(stats["total_items"], 0)
        self.assertEqual(stats["validation"]["rejected_provenance"], 1)
        self.assertEqual(result[0], "")
        self.assertEqual(result[1], "")


if __name__ == "__main__":
    unittest.main()
