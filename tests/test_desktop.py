"""桌面配置、进程启动参数和冻结程序路径的回归验证；不访问账号和模型。"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ykt_desktop as desktop
import ykt_paths
from ykt_llm import capability_binding, cached_vision_capability


class DesktopTests(unittest.TestCase):
    def test_custom_config_preserves_other_settings_and_normalizes_url(self):
        cfg = {"poll_interval": 7, "llm": {"timeout": 12, "api_key": "old"}}
        new = desktop.update_settings(cfg, "https://example.com/v2/chat/completions", "model",
                                      "new-key", "none", "45")
        self.assertEqual(new["poll_interval"], 7)
        self.assertEqual(new["llm"]["timeout"], 12)
        self.assertEqual(new["llm"]["base_url"], "https://example.com/v2")
        self.assertEqual(new["llm"]["api_key"], "new-key")
        self.assertEqual(cfg["llm"]["api_key"], "old")
        self.assertNotIn("auto_submit", new)

    def test_default_reasoning_omitted_and_no_auto_answer_keys(self):
        cfg = desktop.update_settings({}, "http://localhost:11434/v1", "model", "ollama",
                                      "默认", "20")
        self.assertIsNone(cfg["llm"]["reasoning_effort"])
        self.assertNotIn("auto_fill", cfg)
        self.assertNotIn("auto_submit", cfg)

    def test_explicit_image_modes_and_legacy_switch_are_saved(self):
        for mode in ('auto','vision','ocr'):
            cfg=desktop.update_settings({},'https://example.test/v1','test','fake','默认','45',
                                        selected_image_mode=mode)
            self.assertEqual(cfg['image_mode'],mode)
        cfg=desktop.update_settings({},'https://example.test/v1','test','fake','默认','45',False)
        self.assertEqual(cfg['image_mode'],'ocr')

    def test_invalid_duration_cannot_start(self):
        for value in ("nan", "inf", "0", "-1", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                desktop.monitor_arguments(value, "")

    def test_browser_time_window(self):
        args = desktop.monitor_arguments("45", "08:00-09:40")
        self.assertIn("08:00-09:40", args)
        self.assertEqual(desktop.monitor_arguments("45", ""), ["--duration", "45.0"])
        with self.assertRaises(ValueError):
            desktop.monitor_arguments("45", "25:00-26:00")

    def test_frozen_worker_calls_exe_without_python_or_source_script(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "app.exe"):
            command = desktop.worker_command("browser", ["--login"])
        self.assertEqual(command, ["app.exe", "--worker", "browser", "--login"])

    def test_frozen_data_is_separate_from_temporary_resources(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"LOCALAPPDATA": tmp, "YKT_DATA_DIR": ""}), \
                patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", str(Path(tmp) / "_MEI"), create=True):
            self.assertEqual(ykt_paths.data_dir(), Path(tmp) / "YuketangOCRAssistant")
            self.assertEqual(ykt_paths.resource_dir(), Path(tmp) / "_MEI")

    def test_owned_stop_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stop"
            with patch.dict(os.environ, {"YKT_STOP_FILE": str(path)}):
                self.assertFalse(ykt_paths.stop_requested())
                path.touch()
                self.assertTrue(ykt_paths.stop_requested())

    def test_gui_opens_with_isolated_config_and_saves_without_credentials(self):
        import tkinter as tk
        with tempfile.TemporaryDirectory() as tmp, patch.object(desktop, "data_dir", return_value=Path(tmp)), \
                patch.object(desktop.set_api_key, "load_raw_config", return_value={"llm": {}}), \
                patch.object(desktop.set_api_key, "CONFIG_FILE", Path(tmp) / "config.json"):
            root = tk.Tk()
            root.withdraw()
            try:
                app = desktop.DesktopApp(root)
                app.provider.set("华为云 ModelArts MaaS")
                app.choose_provider()
                self.assertEqual(app.model.get(), "deepseek-v4.1-flash-1017")
                self.assertEqual(app.reasoning.get(), "none")
                self.assertEqual(app.key_entry.cget("show"), "●")
                self.assertIsNotNone(app.save(False))
                self.assertTrue((Path(tmp) / "config.json").exists())
                # 测试通过后缓存能力；手动选择保持不变。
                app.key.set('fake-test-key')
                app.image_mode.set('OCR 扫描文本')
                cfg=app.current_settings()
                tested={'binding':capability_binding(cfg,'fake-test-key'),'checked_at':time.time(),
                        'status':'supported','supported':True,'message':'图片测试通过'}
                app.apply_connection_result({'ok':True,'message':'OK','vision':tested})
                self.assertIsNotNone(cached_vision_capability(app.cfg,'fake-test-key'))
                self.assertEqual(app.cfg['image_mode'],'ocr')
                # 测试期间用户切换模型时，不把旧测试应用到新模型。
                app.model.set('another-model')
                app.apply_connection_result({'ok':True,'message':'OK','vision':tested})
                self.assertIsNone(cached_vision_capability(app.current_settings(),'fake-test-key'))
                self.assertIn('未应用旧结果',app.logs.get('1.0','end'))
                root.update_idletasks()
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()
