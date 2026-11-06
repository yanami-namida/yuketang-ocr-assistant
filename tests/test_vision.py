"""视觉请求与选项校验的离线回归；不访问课堂和模型 API。"""
import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import ykt_llm as llm
import ykt_watcher as watcher
from tests import test_dom as fixtures


def reading(**override):
    return dict({"kind": "单选", "stem": "以下哪项说法正确？", "complete": True,
                 "options": [{"label": label, "text": text} for label, text in zip(
                     "ABCD", ["第一项正文", "第二项正文", "第三项正文", "第四项正文"])],
                 "answer": "C", "confidence": 0.9, "reason": "依据题干"}, **override)


class VisionRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "question.png"
        Image.new("RGB", (32, 32), "white").save(self.path)
        self.q = {"vision": True, "kind": "未知", "stem": "", "image_path": str(self.path),
                  "options": [{"label": c, "text": "", "box": {"x": 1}, "checked": c == "B"}
                              for c in "ABCD"], "countdown": 30}
        self.cfg = copy.deepcopy(watcher.DEFAULT_CONFIG)

    def test_image_is_sent_inline_in_same_chat_request(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(reading())}}]}
        with patch.object(llm.requests, "post", return_value=response) as post:
            result = llm.ask_llm_blocking(self.cfg, self.q, "fake-key")
        self.assertTrue(result["ok"])
        payload = post.call_args.kwargs["json"]
        content = payload["messages"][1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("A,B,C,D", content[0]["text"])
        self.assertEqual(base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]), self.path.read_bytes())
        self.assertEqual(payload["max_tokens"], 1800)
        self.assertIn("页脚日期", payload["messages"][0]["content"])
        post.assert_called_once()

    def test_missing_image_fails_without_sending_text_instead(self):
        self.path.unlink()
        with patch.object(llm.requests, "post") as post:
            self.assertFalse(llm.ask_llm_blocking(self.cfg, self.q, "fake-key")["ok"])
        post.assert_not_called()

    def test_reading_preserves_dom_targets_and_selection(self):
        signature = self.q["signature"] = "fixed-image-id"
        advice = llm.normalize_llm_answer(reading(), self.q)
        self.assertTrue(self.q["vision_complete"])
        self.assertEqual(self.q["kind"], "单选")
        self.assertEqual(advice["selected"], ["C"])
        self.assertEqual(self.q["options"][1]["text"], "第二项正文")
        self.assertTrue(self.q["options"][1]["checked"])
        self.assertEqual(self.q["options"][1]["box"], {"x": 1})
        self.assertEqual(self.q["signature"], signature)

    def test_incomplete_duplicate_missing_or_reordered_labels_do_not_select(self):
        cases = [reading(complete=False), reading(options=reading()["options"][:-1]),
                 reading(options=list(reversed(reading()["options"]))),
                 reading(options=[reading()["options"][0]] * 4), reading(stem=""),
                 reading(options=[dict(o, text="") for o in reading()["options"]])]
        for data in cases:
            with self.subTest(data=data):
                q = copy.deepcopy(self.q)
                advice = llm.normalize_llm_answer(data, q)
                self.assertFalse(q["vision_complete"])
                self.assertFalse(advice["recognition_ok"])
                self.assertEqual(advice["selected"], [])

    def test_invalid_or_multiple_single_choice_answers_do_not_select(self):
        for answer in ("AC", "AZ", "Z", "AA"):
            with self.subTest(answer=answer):
                advice = llm.normalize_llm_answer(reading(answer=answer), copy.deepcopy(self.q))
                self.assertEqual(advice["selected"], [])

    def test_judgment_accepts_the_actual_button_letter(self):
        q = copy.deepcopy(self.q)
        self.assertEqual(llm.normalize_llm_answer(reading(kind="判断", answer="C"), q)["selected"], ["C"])

    def test_nonfinite_confidence_does_not_become_high_confidence(self):
        for value in (float("nan"), float("inf"), "nan"):
            self.assertEqual(llm.normalize_llm_answer(reading(confidence=value), copy.deepcopy(self.q))["confidence"], 0)

    def test_expired_question_does_not_call_api(self):
        self.q["_deadline"] = 0
        with patch.object(llm.requests, "post") as post:
            self.assertFalse(llm.ask_llm_blocking(self.cfg, self.q, "fake-key")["ok"])
        post.assert_not_called()

    def test_invalid_request_is_not_retried_and_key_is_redacted(self):
        with patch.object(llm.requests, "post", return_value=Mock(status_code=400, text="fake-key")) as post:
            result = llm.ask_llm_blocking(self.cfg, self.q, "fake-key")
        post.assert_called_once()
        self.assertNotIn("fake-key", result["error"])


class VisionBrowserTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.DomClickTests.asyncSetUp
    asyncTearDown = fixtures.DomClickTests.asyncTearDown
    raw_probe = fixtures.DomClickTests.raw_probe

    async def question(self):
        self.cfg.update(vision=True)
        await self.page.set_content(fixtures.IMAGE_QUIZ)
        with patch.object(watcher, "ocr_boxes", side_effect=AssertionError("视觉不应调用 OCR")):
            q = await watcher.probe_page(self.page, self.cfg)
        self.assertIsNotNone(q)
        self.assertTrue(q["vision"])
        return q

    async def test_capture_without_ocr_then_read(self):
        q = await self.question()
        self.assertEqual([o["label"] for o in q["options"]], list("ABCD"))
        self.assertTrue(Path(q["image_path"]).is_file())
        advice = llm.normalize_llm_answer(reading(), q)
        self.assertEqual(advice["selected"], ["C"])
        self.assertEqual(await self.page.evaluate("selected"), [])
        self.assertEqual(await self.page.evaluate("submissions"), [])

    async def test_identity_ignores_timer_selection_and_footer(self):
        q = await self.question()
        await self.page.locator('[data-ykt-opt="C"]').click()
        await self.page.locator('.count-down').evaluate("e => e.textContent='剩余时间 00:15'")
        await self.page.evaluate("() => document.querySelector('.page-exercise').insertAdjacentHTML('beforeend','<p>2026/11/4</p>')")
        new = await watcher.probe_page(self.page, self.cfg)
        self.assertEqual(new["signature"], q["signature"])
        self.assertEqual(new["image_path"], q["image_path"])
        self.assertTrue(new["options"][2]["checked"])
        self.assertEqual(new["countdown"], 15)

    async def test_new_image_changes_identity_even_on_same_page(self):
        q = await self.question()
        await self.page.locator('img').evaluate("e => e.src=e.src.replace('white','blue')")
        await self.page.locator('img').evaluate("e => e.decode()")
        new = await watcher.probe_page(self.page, self.cfg)
        self.assertNotEqual(new["signature"], q["signature"])

    async def test_normal_slide_without_answer_controls_is_ignored(self):
        await self.page.set_content(fixtures.IMAGE_QUIZ)
        await self.page.locator('.letters,.submit-btn,.count-down').evaluate_all("els => els.forEach(e => e.remove())")
        self.cfg["vision"] = True
        with patch.object(watcher, "ocr_boxes", side_effect=AssertionError("不应运行 OCR")):
            self.assertIsNone(await watcher.probe_page(self.page, self.cfg))

    async def test_small_logo_does_not_suppress_a_complete_text_question(self):
        await self.page.goto((watcher.BASE_DIR / 'mock_quiz.html').as_uri())
        await self.page.locator('#panel').evaluate("""e => {
          const image = document.createElement('img');
          image.style.cssText='width:20px;height:20px';
          image.src='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
          e.append(image);
        }""")
        self.cfg['vision'] = True
        q = await watcher.probe_page(self.page, self.cfg)
        self.assertIsNotNone(q)
        self.assertFalse(q.get('vision'))
        self.assertIn('关系数据库', q['stem'])

    async def test_incomplete_reading_does_not_produce_a_click(self):
        q = await self.question()
        advice = llm.normalize_llm_answer(reading(complete=False), q)
        self.assertEqual(advice["selected"], [])
        self.assertEqual(await self.page.evaluate("selected"), [])
        self.assertEqual(await self.page.evaluate("submissions"), [])

if __name__ == "__main__":
    unittest.main()
