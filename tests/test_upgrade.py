"""Тест обновления config.json на сервере (deploy/upgrade_config.py)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import upgrade_config  # noqa: E402


class Upgrade(unittest.TestCase):
    def test_old_config(self):
        old = {"smtp": {"user": "me@x.ru", "password": "секрет"}, "limits": {"per_day": 15},
               "schedule": {"time": "11:00", "tz": "Asia/Omsk", "niches": ["автосервис"], "cities": ["Омск"],
                            "max_cards": 10, "max_searches": 8}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            sys.argv = ["x", str(path)]
            upgrade_config.main()
            new = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(new["smtp"], old["smtp"])  # почту и пароль не трогаем
            self.assertEqual(new["limits"]["per_day"], 10)  # 10 писем в день на все ящики
            self.assertEqual((new["smtp2"]["user"], new["smtp2"]["password"]), ("вторая-почта@ваш-домен.ru", "пароль приложения"))
            self.assertEqual(new["search"]["cards"], 12)
            s = new["schedule"]
            self.assertEqual((s["cities"], s["niches"], s["max_cards"], s["max_searches"]), ("все", "все", 12, 40))
            self.assertEqual((s["time"], s["tz"]), ("11:00", "Asia/Omsk"))
            self.assertEqual(len(list(Path(tmp).glob("config.json.bak-*"))), 1)
            upgrade_config.main()  # второй запуск ничего не меняет и не плодит копии
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), new)
            self.assertEqual(len(list(Path(tmp).glob("config.json.bak-*"))), 1)

    def test_version2_once(self):
        """Версия 2 применяется один раз: свои значения потом не перетираются."""
        cfg = {"smtp": {"host": "smtp.mail.ru", "user": "a@b.ru", "password": "x"}, "limits": {"per_day": 20}}
        upgrade_config.upgrade(cfg)
        self.assertEqual(cfg["smtp2"]["host"], "smtp.mail.ru")
        cfg["limits"]["per_day"], cfg["schedule"]["max_cards"] = 14, 20
        self.assertEqual(upgrade_config.upgrade(cfg), [])
        self.assertEqual((cfg["limits"]["per_day"], cfg["schedule"]["max_cards"]), (14, 20))

    def test_example_config_is_current(self):
        cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(upgrade_config.upgrade(json.loads(json.dumps(cfg))), [])


if __name__ == "__main__":
    unittest.main()
