import json
import socket
import tempfile
import unittest
from pathlib import Path

from ai_translator.backend import recv_frame, send_frame
from ai_translator.config import bundled_config_path, config_from_mapping, read_api_key, resolve_api_key_file
from ai_translator.deepseek import request_body
from ai_translator.llm import parse_model_json, system_prompt
from ai_translator.policy import (
    EMPTY,
    POLISH,
    SILENT,
    TOO_LONG,
    TRANSLATE,
    TRANSLATE_READONLY,
    candidate_count,
    classify_surface,
    decide,
    mainly_chinese,
    style_slice,
)
from ai_translator.shortcuts import gvariant_strv, merge_paths, parse_strv
from ai_translator.tomlutil import loads as toml_loads


EDITABLE = ["code", "gedit"]
READONLY = ["evince", "xterm"]


class FrameTests(unittest.TestCase):
    def test_roundtrip_keeps_newlines(self):
        left, right = socket.socketpair()
        try:
            send_frame(left, {"message": "第一行\n第二行"})
            self.assertEqual(recv_frame(right)["message"], "第一行\n第二行")
        finally:
            left.close()
            right.close()


class TomlTests(unittest.TestCase):
    def test_default_document(self):
        text = bundled_config_path().read_text(encoding="utf-8")
        self.assertIn("api_key_file", text)
        self.assertNotIn("crsr_", text)
        self.assertNotIn("sk-", text)
        parsed = toml_loads(text)
        self.assertEqual(parsed["api"]["provider"], "deepseek")
        self.assertEqual(parsed["cursor"]["model"], "composer-2.5")
        self.assertEqual(parsed["cursor"]["api_key_file"], "~/.config/ai-translator/api_key")
        self.assertTrue(parsed["cursor"]["fast"])
        self.assertEqual(parsed["deepseek"]["model"], "deepseek-flash")
        self.assertFalse(parsed["deepseek"]["thinking"])
        self.assertEqual(parsed["limits"]["short_text_max_chars"], 200)
        self.assertEqual(parsed["limits"]["chinese_char_ratio"], 0.6)
        self.assertIn("code", parsed["windows"]["editable_wm_class"])
        self.assertIn("google-chrome", parsed["windows"]["editable_wm_class"])
        self.assertIn("{count}", parsed["prompts"]["translate_system"])
        self.assertIn("{styles}", parsed["prompts"]["polish_system"])
        cfg = config_from_mapping(parsed, path=bundled_config_path())
        self.assertEqual(cfg.provider, "deepseek")
        self.assertIsInstance(cfg.api_key, str)
        self.assertIsInstance(cfg.deepseek_api_key, str)
        self.assertTrue(cfg.fast)
        self.assertEqual(cfg.model, "composer-2.5")
        self.assertEqual(cfg.deepseek_model, "deepseek-flash")
        self.assertEqual(cfg.styles[1], "自然")
        rendered, names = system_prompt(cfg, "translate", 3)
        self.assertEqual(names, ["礼貌", "自然", "精炼"])
        self.assertIn("1. 礼貌", rendered)
        self.assertIn("more courteous wording", rendered)
        rendered, names = system_prompt(cfg, "translate_readonly", 1)
        self.assertEqual(names, ["自然"])
        self.assertIn("en→zh", rendered)

    def test_comments_and_trailing_comma(self):
        parsed = toml_loads(
            '[demo]\n'
            'name = "a\\"b"\n'
            'items = [\n'
            '  "one",\n'
            '  # skip\n'
            '  "two",\n'
            ']\n'
        )
        self.assertEqual(parsed["demo"]["name"], 'a"b')
        self.assertEqual(parsed["demo"]["items"], ["one", "two"])


class DeepSeekTests(unittest.TestCase):
    def test_request_disables_thinking(self):
        body = request_body("deepseek-flash", False, "Return JSON.", "你好")
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["messages"][1]["content"], "Source text:\n你好")


class KeyFileTests(unittest.TestCase):
    def test_one_line_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_key"
            path.write_text("secret-token\n", encoding="utf-8")
            self.assertEqual(read_api_key(path), "secret-token")
            self.assertEqual(read_api_key(path.with_name("missing")), "")
            resolved = resolve_api_key_file("~/ai-translator-test-key", Path(directory) / "config.toml")
            self.assertTrue(resolved.is_absolute())


class PolicyTests(unittest.TestCase):
    def test_terminal_stays_readonly_even_if_listed_editable(self):
        self.assertEqual(classify_surface("gnome-terminal", "Gnome-terminal", ["gnome-terminal"], []), "readonly")
        self.assertEqual(classify_surface("code", "Code", EDITABLE, READONLY), "editable")
        self.assertEqual(classify_surface("google-chrome", "Google-chrome", EDITABLE, READONLY), "readonly")
        self.assertEqual(
            classify_surface("google-chrome", "Google-chrome", ["google-chrome"], READONLY),
            "editable",
        )
        self.assertEqual(classify_surface("evince", "Evince", EDITABLE, READONLY), "readonly")
        self.assertEqual(classify_surface("", "", EDITABLE, READONLY), "readonly")

    def test_language_and_actions(self):
        self.assertTrue(mainly_chinese("这是一段中文说明 OK", 0.6))
        self.assertFalse(mainly_chinese("This is English 的", 0.6))
        self.assertFalse(mainly_chinese("12345", 0.6))
        self.assertEqual(decide("polish", "readonly", "Hello", 4000, 0.6), SILENT)
        self.assertEqual(decide("translate", "readonly", "这是一段中文", 4000, 0.6), SILENT)
        self.assertEqual(decide("translate", "readonly", "Hello world", 4000, 0.6), TRANSLATE_READONLY)
        self.assertEqual(decide("translate", "readonly", "123", 4000, 0.6), SILENT)
        self.assertEqual(decide("translate", "readonly", None, 4000, 0.6), SILENT)
        self.assertEqual(decide("translate", "editable", None, 4000, 0.6), EMPTY)
        self.assertEqual(decide("translate", "editable", "你好", 4000, 0.6), TRANSLATE)
        self.assertEqual(decide("polish", "editable", "你好", 4000, 0.6), POLISH)
        self.assertEqual(decide("translate", "readonly", "e" * 4001, 4000, 0.6), TOO_LONG)

    def test_candidate_count_and_styles(self):
        self.assertEqual(candidate_count("a" * 100, 100, 3, 1), 3)
        self.assertEqual(candidate_count("a" * 101, 100, 3, 1), 1)
        names, hints = style_slice(["忠实", "自然", "精炼"], ["a", "b", "c"], 1)
        self.assertEqual(names, ["自然"])
        self.assertEqual(hints, ["b"])


class ModelJsonTests(unittest.TestCase):
    def test_fenced_and_objects(self):
        raw = '```json\n{"direction":"en→zh","candidates":["甲","乙"]}\n```'
        direction, items = parse_model_json(raw)
        self.assertEqual(direction, "en→zh")
        self.assertEqual(items, ["甲", "乙"])
        direction, items = parse_model_json(json.dumps({
            "direction": "zh",
            "candidates": [{"text": " one "}, "two"],
        }))
        self.assertEqual(items, ["one", "two"])


class ClipboardTests(unittest.TestCase):
    def test_keep_blocks_restore(self):
        from unittest.mock import patch

        from ai_translator.x11util import ClipboardBackup

        backup = ClipboardBackup()
        backup.had_text = True
        backup.original = "old"
        backup.dirty = True
        backup.keep()
        with patch("ai_translator.x11util.write_clipboard") as write:
            backup.restore()
        write.assert_not_called()

    def test_failed_focus_leaves_clipboard_and_does_not_paste(self):
        from unittest.mock import patch

        from ai_translator.x11util import ClipboardBackup, paste_text

        backup = ClipboardBackup()
        with patch("ai_translator.x11util.write_clipboard") as write, \
                patch("ai_translator.x11util.focus_window", return_value=False), \
                patch("ai_translator.x11util.send_key") as key:
            paste_text("译文", backup, 0, 42)
        write.assert_called_once_with("译文")
        key.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def test_accepts_x11_on_supported_python(self):
        import os
        from unittest.mock import patch

        from ai_translator.__main__ import _runtime_error

        with patch("ai_translator.__main__.sys.version_info", (3, 12, 3)), \
                patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}):
            self.assertIsNone(_runtime_error())

    def test_rejects_wayland_and_old_python(self):
        import os
        from unittest.mock import patch

        from ai_translator.__main__ import _runtime_error

        with patch("ai_translator.__main__.sys.version_info", (3, 12, 0)), \
                patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}):
            self.assertIn("X11", _runtime_error())
        with patch("ai_translator.__main__.sys.version_info", (3, 9, 0)), \
                patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}):
            self.assertIn("3.10", _runtime_error())


class ShortcutTests(unittest.TestCase):
    def test_merge_keeps_existing(self):
        existing = parse_strv("['/org/custom0/', '/org/custom1/']")
        self.assertEqual(existing, ["/org/custom0/", "/org/custom1/"])
        merged = merge_paths(existing, ["/org/custom1/", "/org/ai-translator-translate/"])
        self.assertEqual(merged[0], "/org/custom0/")
        self.assertEqual(merged[-1], "/org/ai-translator-translate/")
        self.assertEqual(merged.count("/org/custom1/"), 1)
        again = parse_strv(gvariant_strv(merged))
        self.assertEqual(again, merged)


if __name__ == "__main__":
    unittest.main()
