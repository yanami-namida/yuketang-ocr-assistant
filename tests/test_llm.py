# -*- coding: utf-8 -*-
"""Key 优先级 / 配置读写：不联网、不碰真实 config.json。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ykt_llm
from tools import set_api_key


def cfg_with(**llm) -> dict:
    base = {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "allow_dsh_credentials": True,
    }
    base.update(llm)
    return {"llm": base}


class ResolvePriorityTests(unittest.TestCase):
    def test_explicit_wins_over_everything(self):
        cfg = cfg_with(api_key="sk-from-config")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-from-env"}):
            self.assertEqual(ykt_llm.resolve_api_key(cfg, "  sk-explicit  "), "sk-explicit")

    def test_config_beats_env(self):
        cfg = cfg_with(api_key="sk-from-config")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-from-env"}):
            self.assertEqual(ykt_llm.resolve_api_key(cfg, None), "sk-from-config")

    def test_env_used_when_config_empty(self):
        cfg = cfg_with()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-from-env"}):
            self.assertEqual(ykt_llm.resolve_api_key(cfg, None), "sk-from-env")

    def test_dsh_credentials_as_last_resort(self):
        cfg = cfg_with()
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(ykt_llm, "load_dsh_key", return_value="sk-from-dsh"):
            self.assertEqual(ykt_llm.resolve_api_key(cfg, None), "sk-from-dsh")

    def test_nothing_configured_returns_none(self):
        cfg = cfg_with(allow_dsh_credentials=False)
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(ykt_llm.resolve_api_key(cfg, None))

    def test_custom_env_name(self):
        cfg = cfg_with(api_key_env="MY_MODEL_KEY")
        with patch.dict(os.environ, {"MY_MODEL_KEY": "sk-custom"}):
            self.assertEqual(ykt_llm.resolve_api_key(cfg, None), "sk-custom")

    def test_source_strings(self):
        cfg = cfg_with(api_key="sk-x")
        self.assertEqual(ykt_llm.api_key_source(cfg, "sk-cli"), "--api-key 参数")
        self.assertEqual(ykt_llm.api_key_source(cfg, None), "config.json 的 llm.api_key")
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(ykt_llm, "load_dsh_key", return_value=None):
            self.assertEqual(ykt_llm.api_key_source(cfg_with(api_key=""), None), "未配置")


class MaskAndConfigTests(unittest.TestCase):
    def test_mask_hides_middle(self):
        masked = set_api_key.mask_key("sk-1234567890abcdef")  # 19 位：前 6 + 9 个星 + 后 4
        self.assertEqual(masked, "sk-123" + "*" * 9 + "cdef")
        self.assertEqual(set_api_key.mask_key(""), "（空）")
        self.assertNotIn("7890", masked)

    def test_write_read_unset_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"llm": {"model": "m1"}, "poll_interval": 3}), encoding="utf-8")

            cfg = set_api_key.load_raw_config(path)
            cfg["llm"]["api_key"] = "sk-roundtrip-0001"
            set_api_key.save_raw_config(cfg, path)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["llm"]["api_key"], "sk-roundtrip-0001")
            self.assertEqual(saved["llm"]["model"], "m1")
            self.assertEqual(saved["poll_interval"], 3)  # 其它键不能被弄丢

            resolved, source = set_api_key.resolve(saved)
            self.assertEqual(resolved, "sk-roundtrip-0001")
            self.assertEqual(source, "config.json 的 llm.api_key")

            saved["llm"].pop("api_key")
            set_api_key.save_raw_config(saved, path)
            self.assertNotIn("api_key", json.loads(path.read_text(encoding="utf-8"))["llm"])


if __name__ == "__main__":
    unittest.main()
