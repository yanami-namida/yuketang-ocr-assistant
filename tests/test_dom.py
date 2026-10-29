"""用本地 Edge 页面验证 DOM 识别、图片题选项绑定和真实点击事件；不访问雨课堂。"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright

import ykt_watcher as watcher


IMAGE_QUIZ = """<!doctype html><meta charset="utf-8">
<style>
body { margin:0 }
.page-exercise { position:relative; width:1000px; height:760px }
.slide { width:950px; height:700px }
.count-down { height:40px }
.letters { position:absolute; top:180px; left:100px }
.answer { display:block; width:56px; height:56px; margin:18px; background:#aaa;
          border-radius:50%; text-align:center; line-height:56px; cursor:pointer }
.answer.selected { background:#286 }
.submit-btn { position:absolute; top:680px; left:650px; width:180px; height:45px }
</style>
<section class="page-exercise">
<div class="count-down">剩余时间 00:29</div>
<img class="slide" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='950' height='700'%3E%3Crect width='950' height='700' fill='white'/%3E%3C/svg%3E">
<div class="letters"></div>
<button class="submit-btn" disabled>提交答案</button>
</section>
<script>
window.selected = [];
window.submissions = [];
window.multi = false;
function renderLetters() {
  const group = document.querySelector('.letters');
  group.replaceChildren();
  for (const label of 'ABCD') {
    const button = document.createElement('span');
    button.className = 'answer' + (selected.includes(label) ? ' selected' : '');
    button.textContent = label;
    button.onclick = () => {
      if (window.ignoreClicks) return;
      if (!multi) selected = [label];
      else if (selected.includes(label)) selected = selected.filter(x => x !== label);
      else selected.push(label);
      renderLetters(); // Vue 常在选中后替换节点，旧 data-ykt-* 不再存在。
      const submit = document.querySelector('.submit-btn');
      if (submit) submit.disabled = !selected.length;
    };
    group.append(button);
  }
}
document.querySelector('.submit-btn').onclick = () => submissions.push([...selected]);
renderLetters();
</script>
"""

# OCR 坐标：题型、题干、四个正文块；正文来自图片，DOM 中只有字母。
OCR_BOXES = [
    {"text": text, "x": 50, "y": y, "w": 400, "h": 30}
    for text, y in [
        ("单选题", 10), ("以下哪项说法正确？", 110),
        ("第一项正文", 220), ("第二项正文", 320),
        ("第三项正文", 420), ("第四项正文", 520),
    ]
]

# 14:41 漏抓现场的 OCR 输出：原始尺寸漏掉 minix，还混入按钮和日期。
LINUX_OCR = [
    {"text": text, "x": x, "y": y, "w": width, "h": height}
    for text, x, y, width, height in [
        ("单选题", 42, 19, 117, 38), ("1分", 197, 29, 45, 29),
        ("哪个不属于|inu×系统成功的五大因素？", 122, 185, 656, 39),
        ("倒计时00：18", 885, 26, 128, 21),
        ("Uni)(", 233, 354, 80, 32), ("GPL标准", 232, 455, 150, 39),
        ("GNU计划", 232, 660, 164, 39),
        ("2026丿1m28", 87, 794, 84, 13), ("提交答案", 782, 762, 97, 24),
    ]
]
LINUX_LARGE = [
    {**box, "x": box["x"] * 2, "y": box["y"] * 2,
     "w": box["w"] * 2, "h": box["h"] * 2}
    for box in LINUX_OCR
] + [{"text": "m|n以", "x": 466, "y": 1137, "w": 195, "h": 45}]


class DomClickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel="msedge", headless=True)
        self.page = await self.browser.new_page(viewport={"width": 1440, "height": 900})
        await self.page.route("http**://**/*", lambda route: route.abort())
        self.cfg = copy.deepcopy(watcher.DEFAULT_CONFIG)
        self.cfg.update(auto_fill=True, auto_submit=True, fill_gap=0, submit_delay=0)
        self.temp = tempfile.TemporaryDirectory()
        self.shots_patch = patch.object(watcher, "SHOT_DIR", Path(self.temp.name))
        self.shots_patch.start()
        self.log_patch = patch.object(watcher, "log")
        self.log_patch.start()
        watcher._OCR_CACHE.clear()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.log_patch.stop()
        self.shots_patch.stop()
        self.temp.cleanup()

    async def raw_probe(self, frame=None):
        return await (frame or self.page).evaluate(
            watcher.PROBE_JS, {"selectors": self.cfg["selectors"]})

    async def image_question(self, frame=None):
        await (frame or self.page).set_content(IMAGE_QUIZ)
        with patch.object(watcher, "ocr_boxes", return_value=OCR_BOXES):
            return await watcher.probe_page(self.page, self.cfg)

    async def test_small_bare_letter_buttons_are_anchored(self):
        await self.page.set_content(IMAGE_QUIZ)
        got = await self.raw_probe()
        self.assertEqual([o["label"] for o in got["options"]], list("ABCD"))
        self.assertTrue(all(o["box"] for o in got["options"]))
        self.assertTrue(all(o["text"] == "" for o in got["options"]))
        self.assertEqual(await self.page.locator('[data-ykt-opt]').count(), 4)
        self.assertTrue(got["hasSubmit"])

    async def test_section_letter_controls_are_anchored(self):
        await self.page.set_content(IMAGE_QUIZ.replace("createElement('span')", "createElement('section')"))
        got = await self.raw_probe()
        self.assertEqual([o["label"] for o in got["options"]], list("ABCD"))
        self.assertTrue(all(o["box"] for o in got["options"]))

    async def test_image_sibling_of_control_panel_is_not_excluded(self):
        await self.page.set_content(IMAGE_QUIZ)
        await self.page.evaluate("""() => {
          const panel = document.createElement('div');
          panel.className = 'question-panel';
          panel.style.cssText = 'position:absolute;inset:0';
          for (const selector of ['.count-down','.letters','.submit-btn'])
            panel.append(document.querySelector(selector));
          document.querySelector('.page-exercise').append(panel);
        }""")
        with patch.object(watcher, "ocr_boxes", return_value=OCR_BOXES):
            q = await watcher.probe_page(self.page, self.cfg)
        self.assertIsNotNone(q)
        self.assertEqual(len(q["options"]), 4)

    async def test_real_missed_question_retries_ocr_without_footer_options(self):
        await self.page.set_content(IMAGE_QUIZ)
        with patch.object(watcher, "ocr_boxes", side_effect=[LINUX_OCR, LINUX_LARGE]) as read:
            q = await watcher.probe_page(self.page, self.cfg)
        self.assertIsNotNone(q)
        self.assertEqual(read.call_count, 2)
        self.assertIn("五大因素", q["stem"])
        self.assertEqual([o["label"] for o in q["options"]], list("ABCD"))
        self.assertEqual(q["options"][1]["text"], "GPL标准")
        self.assertEqual(q["options"][3]["text"], "GNU计划")
        self.assertNotIn("提交答案", str(q["options"]))
        self.assertNotIn("2026", str(q["options"]))
        self.assertTrue(watcher._PROBE_DETAILS[id(self.page)]["ocr"]["enlarged_retry"])

    async def test_unrecoverable_missing_option_records_rejection(self):
        await self.page.set_content(IMAGE_QUIZ)
        with patch.object(watcher, "ocr_boxes", return_value=LINUX_OCR):
            q = await watcher.probe_page(self.page, self.cfg)
        self.assertIsNone(q)
        detail = watcher._PROBE_DETAILS[id(self.page)]
        self.assertEqual(detail["dom"]["labels"], list("ABCD"))
        self.assertEqual(detail["ocr"]["labels"], list("ABC"))
        self.assertIn("不一致", detail["rejected"])

    async def test_ocr_text_is_bound_to_dom_buttons_and_submitted(self):
        q = await self.image_question()
        self.assertIsNotNone(q)
        self.assertTrue(q["ocr"])
        self.assertEqual(q["stem"], "以下哪项说法正确？")
        self.assertEqual(q["options"][1]["text"], "第二项正文")
        self.assertTrue(all(o["box"] for o in q["options"]))
        result = await watcher.auto_answer(self.page, q, {"selected": ["B"]}, self.cfg)
        self.assertIn("并点击提交", result)
        self.assertEqual(await self.page.evaluate("submissions"), [["B"]])

    async def test_multiple_answers_survive_option_rerender(self):
        q = await self.image_question()
        await self.page.evaluate("multi = true")
        result = await watcher.auto_answer(self.page, q, {"selected": ["A", "C"]}, self.cfg)
        self.assertIn("并点击提交", result)
        self.assertEqual(await self.page.evaluate("submissions"), [["A", "C"]])

    async def test_button_appearing_only_after_selection_is_found(self):
        q = await self.image_question()
        await self.page.evaluate("""() => {
          const button = document.querySelector('.submit-btn');
          window.savedSubmit = button;
          button.remove();
          document.querySelector('.letters').addEventListener('click', () => {
            document.querySelector('.page-exercise').append(savedSubmit);
            savedSubmit.disabled = false;
          });
        }""")
        q["has_submit"] = False
        result = await watcher.auto_answer(self.page, q, {"selected": ["C"]}, self.cfg)
        self.assertIn("并点击提交", result)
        self.assertEqual(await self.page.evaluate("submissions"), [["C"]])

    async def test_submit_outside_question_root_falls_back_to_text(self):
        # 2026-10-29 真实课堂页：选项圆钮在 .page-exercise 里，「提交答案」在容器外。
        q = await self.image_question()
        await self.page.evaluate("""() => {
          const button = document.querySelector('.submit-btn');
          document.body.append(button);
          button.disabled = false;
        }""")
        q["has_submit"] = False
        result = await watcher.auto_answer(self.page, q, {"selected": ["C"]}, self.cfg)
        self.assertIn("并点击提交", result)
        self.assertEqual(await self.page.evaluate("submissions"), [["C"]])

    async def test_ineffective_click_does_not_submit(self):
        q = await self.image_question()
        await self.page.evaluate("ignoreClicks = true")
        result = await watcher.auto_answer(self.page, q, {"selected": ["B"]}, self.cfg)
        self.assertIn("未提交", result)
        self.assertEqual(await self.page.evaluate("submissions"), [])

    async def test_disabled_submit_is_not_force_clicked(self):
        q = await self.image_question()
        await self.page.evaluate("""() => document.querySelector('.letters').addEventListener(
          'click', () => { document.querySelector('.submit-btn').disabled = true; })""")
        result = await watcher.auto_answer(self.page, q, {"selected": ["B"]}, self.cfg)
        self.assertNotIn("并点击提交", result)
        self.assertEqual(await self.page.evaluate("selected"), ["B"])
        self.assertEqual(await self.page.evaluate("submissions"), [])

    async def test_disabled_option_is_not_force_clicked(self):
        q = await self.image_question()
        await self.page.locator('[data-ykt-opt="B"]').evaluate(
            "el => el.setAttribute('aria-disabled', 'true')")
        result = await watcher.auto_answer(self.page, q, {"selected": ["B"]}, self.cfg)
        self.assertIn("未提交", result)
        self.assertEqual(await self.page.evaluate("selected"), [])
        self.assertEqual(await self.page.evaluate("submissions"), [])

    async def test_fill_only_does_not_submit(self):
        q = await self.image_question()
        self.cfg["auto_submit"] = False
        result = await watcher.auto_answer(self.page, q, {"selected": ["D"]}, self.cfg)
        self.assertIn("未提交", result)
        self.assertEqual(await self.page.evaluate("selected"), ["D"])
        self.assertEqual(await self.page.evaluate("submissions"), [])

    async def test_existing_selection_is_preserved(self):
        q = await self.image_question()
        q["options"][0]["checked"] = True
        await self.page.locator('[data-ykt-opt="A"]').click()
        result = await watcher.auto_answer(self.page, q, {"selected": ["B"]}, self.cfg)
        self.assertIn("页面已有选择", result)
        self.assertEqual(await self.page.evaluate("selected"), ["A"])
        self.assertEqual(await self.page.evaluate("submissions"), [])

    async def test_missing_dom_target_does_not_click_or_submit(self):
        q = await self.image_question()
        q["options"][1]["box"] = None
        result = await watcher.auto_answer(self.page, q, {"selected": ["B"]}, self.cfg)
        self.assertIn("未点击、未提交", result)
        self.assertEqual(await self.page.evaluate("selected"), [])

    async def test_image_without_ocr_is_not_a_fake_d_submit_option(self):
        await self.page.set_content(IMAGE_QUIZ)
        self.cfg["ocr"] = False
        self.assertIsNone(await watcher.probe_page(self.page, self.cfg))

    async def test_original_text_quiz_still_fills_and_submits(self):
        await self.page.goto((watcher.BASE_DIR / "mock_quiz.html").as_uri())
        q = await watcher.probe_page(self.page, self.cfg)
        self.assertEqual(len(q["options"]), 4)
        result = await watcher.auto_answer(self.page, q, {"selected": ["B"]}, self.cfg)
        self.assertIn("并点击提交", result)
        self.assertEqual(await self.page.evaluate("__mockSubmit.selected"), ["B"])

    async def test_stale_hidden_anchors_are_removed(self):
        await self.page.set_content(IMAGE_QUIZ)
        await self.raw_probe()
        await self.page.evaluate("""() => {
          const old = document.querySelector('.letters').cloneNode(true);
          old.style.display = 'none';
          document.body.prepend(old);
        }""")
        await self.raw_probe()
        self.assertEqual(await self.page.locator('[data-ykt-opt="B"]').count(), 1)
        self.assertTrue(await self.page.locator('[data-ykt-opt="B"]').is_visible())

    async def test_image_controls_in_iframe_keep_correct_frame(self):
        await self.page.set_content('<iframe style="width:1100px;height:800px"></iframe>')
        q = await self.image_question(self.page.frames[1])
        self.assertEqual(q["frame_index"], 1)
        result = await watcher.auto_answer(self.page, q, {"selected": ["C"]}, self.cfg)
        self.assertIn("并点击提交", result)
        self.assertEqual(await self.page.frames[1].evaluate("submissions"), [["C"]])


if __name__ == "__main__":
    unittest.main()
