# -*- coding: utf-8 -*-
"""Key 优先级 / 配置读写：不联网、不碰真实 config.json。"""
from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

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
    def test_first_run_preserves_example_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            example = Path(tmp) / "example.json"
            example.write_text(json.dumps({"poll_interval": 7, "llm": {"model": "sample"}}),
                               encoding="utf-8")
            with patch.object(set_api_key, "EXAMPLE_FILE", example):
                cfg = set_api_key.load_raw_config(path)
            self.assertEqual(cfg["poll_interval"], 7)
            self.assertEqual(cfg["llm"]["model"], "sample")
            self.assertFalse(path.exists())

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


class CustomProviderTests(unittest.TestCase):
    BASE = "https://api.modelarts-maas.com/v2"
    MODEL = "deepseek-v4.1-flash-1017"

    def test_base_and_full_endpoint_are_equivalent(self):
        for url in (self.BASE, self.BASE + "/", self.BASE + "/chat/completions",
                    " " + self.BASE + "/chat/completions/ "):
            with self.subTest(url=url):
                self.assertEqual(ykt_llm.chat_completions_url(url), self.BASE + "/chat/completions")
        self.assertEqual(ykt_llm.chat_completions_url("http://localhost:11434/v1"),
                         "http://localhost:11434/v1/chat/completions")

    def test_malformed_endpoint_is_rejected(self):
        for url in (self.BASE + "/chat/completionssvg", "file:///tmp/key",
                    "https://example.com/v1?key=private", "https://user:secret@example.com/v1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ykt_llm.chat_completions_url(url)

    def test_real_request_uses_custom_model_and_optional_reasoning(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content":
            '{"answer":"B","confidence":0.9,"reason":"test"}'}}]}
        cfg = cfg_with(base_url=self.BASE + "/chat/completions", model=self.MODEL,
                       reasoning_effort="none")
        question = {"kind": "单选", "stem": "1 + 1 = ?", "options": [
            {"label": "A", "text": "1"}, {"label": "B", "text": "2"}]}
        with patch.object(ykt_llm.requests, "post", return_value=response) as post:
            result = ykt_llm.ask_llm_blocking(cfg, question, "fake-maas-key")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["answer"], "B")
        self.assertEqual(post.call_args.args[0], self.BASE + "/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], self.MODEL)
        self.assertEqual(post.call_args.kwargs["json"]["reasoning_effort"], "none")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_check_validates_selected_model_without_models_endpoint(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
        cfg = cfg_with(base_url=self.BASE + "/chat/completions", model=self.MODEL)
        with patch.object(ykt_llm.requests, "post", return_value=response) as post, \
                patch.object(ykt_llm.requests, "get") as get:
            ok, _ = set_api_key.check_key(cfg, "fake-maas-key")
        self.assertTrue(ok)
        get.assert_not_called()
        self.assertEqual(post.call_args.args[0], self.BASE + "/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], self.MODEL)

    def test_check_rejects_non_chat_200_response(self):
        response = Mock(status_code=200)
        response.json.return_value = {"error": "not a chat response"}
        with patch.object(ykt_llm.requests, "post", return_value=response):
            ok, _ = set_api_key.check_key(cfg_with(), "fake-key")
        self.assertFalse(ok)

    def test_error_response_does_not_echo_credentials(self):
        response = Mock(status_code=400, text="request echoed fake-private-key")
        with patch.object(ykt_llm.requests, "post", return_value=response):
            ok, message = set_api_key.check_key(cfg_with(), "fake-private-key")
        self.assertFalse(ok)
        self.assertNotIn("fake-private-key", message)

    def test_check_applies_cli_overrides_without_saving(self):
        cfg = cfg_with(api_key="fake-key")
        argv = ["set_api_key.py", "--check", "--base-url", self.BASE + "/chat/completions",
                "--model", self.MODEL, "--reasoning-effort", "none"]
        with patch.object(sys, "argv", argv), \
                patch.object(set_api_key, "load_raw_config", return_value=cfg), \
                patch.object(set_api_key, "save_raw_config") as save, \
                patch.object(set_api_key, "check_key", return_value=(True, "OK")) as check, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(set_api_key.main(), 0)
        save.assert_not_called()
        checked = check.call_args.args[0]["llm"]
        self.assertEqual(checked["base_url"], self.BASE)
        self.assertEqual(checked["model"], self.MODEL)
        self.assertEqual(checked["reasoning_effort"], "none")

    def test_env_mode_persists_provider_and_removes_stale_config_key(self):
        cfg = cfg_with(api_key="fake-stale")
        argv = ["set_api_key.py", "--env", "--key", "fake-new", "--base-url", self.BASE,
                "--model", self.MODEL, "--api-key-env", "MAAS_API_KEY"]
        with patch.object(sys, "argv", argv), \
                patch.object(set_api_key, "load_raw_config", return_value=cfg), \
                patch.object(set_api_key, "set_user_env") as env, \
                patch.object(set_api_key, "save_raw_config") as save, \
                patch.object(set_api_key, "check_key", return_value=(True, "OK")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(set_api_key.main(), 0)
        env.assert_called_once_with("MAAS_API_KEY", "fake-new")
        saved = save.call_args.args[0]["llm"]
        self.assertNotIn("api_key", saved)
        self.assertEqual(saved["base_url"], self.BASE)
        self.assertEqual(saved["model"], self.MODEL)


if __name__ == "__main__":
    unittest.main()
