"""OCR 解析的离线回归测试。

这里的数据是 2026-10-21「嵌入式系统」真实随堂题（图片题）识别出来的词块，
用来固定「按坐标分行 → 分块 → 最后 N 块当选项」这套切分逻辑。
"""
import unittest

from ocr_win import (
    _decode_ps_output, fix_ocr_text, group_blocks, merge_line_boxes, parse_ocr_question,
)

# 真实题目 1：以下8位图立即数是否合法？（词块来自 ocr_boxes）
Q1 = [
    {"text": "单", "x": 49, "y": 22, "w": 20, "h": 45},
    {"text": "选", "x": 70, "y": 22, "w": 20, "h": 45},
    {"text": "题", "x": 91, "y": 22, "w": 20, "h": 45},
    {"text": "以", "x": 146, "y": 220, "w": 60, "h": 40},
    {"text": "下", "x": 210, "y": 220, "w": 60, "h": 40},
    {"text": "8", "x": 274, "y": 220, "w": 30, "h": 40},
    {"text": "位", "x": 306, "y": 220, "w": 60, "h": 40},
    {"text": "图", "x": 370, "y": 220, "w": 60, "h": 40},
    {"text": "立", "x": 434, "y": 220, "w": 60, "h": 40},
    {"text": "即", "x": 496, "y": 220, "w": 60, "h": 40},
    {"text": "数", "x": 560, "y": 220, "w": 60, "h": 40},
    {"text": "是", "x": 624, "y": 220, "w": 60, "h": 40},
    {"text": "否", "x": 686, "y": 220, "w": 60, "h": 40},
    {"text": "合", "x": 750, "y": 220, "w": 60, "h": 40},
    {"text": "法", "x": 814, "y": 220, "w": 60, "h": 40},
    {"text": "？", "x": 876, "y": 220, "w": 40, "h": 40},
    {"text": "0x0103c000", "x": 271, "y": 416, "w": 332, "h": 37},
    {"text": "0x12800000", "x": 271, "y": 536, "w": 326, "h": 37},
    {"text": "0x902", "x": 271, "y": 656, "w": 161, "h": 37},
    {"text": "0x135", "x": 271, "y": 776, "w": 161, "h": 37},
]

# 真实题目 2：Arm指令关于Load/Store...（注意被拆开的 "L oa d"）
Q2 = [
    {"text": "单", "x": 49, "y": 22, "w": 20, "h": 45},
    {"text": "选", "x": 70, "y": 22, "w": 20, "h": 45},
    {"text": "题", "x": 91, "y": 22, "w": 20, "h": 45},
    {"text": "Arm", "x": 141, "y": 217, "w": 60, "h": 47},
    {"text": "指", "x": 210, "y": 217, "w": 45, "h": 47},
    {"text": "令", "x": 258, "y": 217, "w": 45, "h": 47},
    {"text": "关", "x": 306, "y": 217, "w": 45, "h": 47},
    {"text": "于", "x": 354, "y": 217, "w": 45, "h": 47},
    {"text": "Load/Store", "x": 402, "y": 217, "w": 200, "h": 47},
    {"text": "指", "x": 610, "y": 217, "w": 45, "h": 47},
    {"text": "令", "x": 658, "y": 217, "w": 45, "h": 47},
    {"text": "说", "x": 706, "y": 217, "w": 45, "h": 47},
    {"text": "法", "x": 754, "y": 217, "w": 45, "h": 47},
    {"text": "正", "x": 802, "y": 217, "w": 45, "h": 47},
    {"text": "确", "x": 850, "y": 217, "w": 45, "h": 47},
    {"text": "的", "x": 898, "y": 217, "w": 45, "h": 47},
    {"text": "是", "x": 946, "y": 217, "w": 45, "h": 47},
    {"text": "0", "x": 1042, "y": 217, "w": 44, "h": 47},
    {"text": "把", "x": 272, "y": 416, "w": 44, "h": 46},
    {"text": "数", "x": 318, "y": 416, "w": 44, "h": 46},
    {"text": "据", "x": 362, "y": 416, "w": 44, "h": 46},
    {"text": "从", "x": 406, "y": 416, "w": 44, "h": 46},
    {"text": "寄", "x": 450, "y": 416, "w": 44, "h": 46},
    {"text": "存", "x": 494, "y": 416, "w": 44, "h": 46},
    {"text": "器", "x": 538, "y": 416, "w": 44, "h": 46},
    {"text": "传", "x": 582, "y": 416, "w": 44, "h": 46},
    {"text": "送", "x": 626, "y": 416, "w": 44, "h": 46},
    {"text": "到", "x": 670, "y": 416, "w": 44, "h": 46},
    {"text": "存", "x": 714, "y": 416, "w": 44, "h": 46},
    {"text": "储", "x": 758, "y": 416, "w": 44, "h": 46},
    {"text": "器", "x": 802, "y": 416, "w": 44, "h": 46},
    {"text": "叫", "x": 846, "y": 416, "w": 44, "h": 46},
    {"text": "L", "x": 890, "y": 416, "w": 20, "h": 46},
    {"text": "oa", "x": 912, "y": 416, "w": 40, "h": 46},
    {"text": "d", "x": 956, "y": 416, "w": 20, "h": 46},
    {"text": "把", "x": 269, "y": 533, "w": 44, "h": 45},
    {"text": "数", "x": 315, "y": 533, "w": 44, "h": 45},
    {"text": "据", "x": 359, "y": 533, "w": 44, "h": 45},
    {"text": "从", "x": 403, "y": 533, "w": 44, "h": 45},
    {"text": "存", "x": 447, "y": 533, "w": 44, "h": 45},
    {"text": "储", "x": 491, "y": 533, "w": 44, "h": 45},
    {"text": "器", "x": 535, "y": 533, "w": 44, "h": 45},
    {"text": "传", "x": 579, "y": 533, "w": 44, "h": 45},
    {"text": "送", "x": 623, "y": 533, "w": 44, "h": 45},
    {"text": "到", "x": 667, "y": 533, "w": 44, "h": 45},
    {"text": "寄", "x": 711, "y": 533, "w": 44, "h": 45},
    {"text": "存", "x": 755, "y": 533, "w": 44, "h": 45},
    {"text": "器", "x": 799, "y": 533, "w": 44, "h": 45},
    {"text": "叫", "x": 843, "y": 533, "w": 44, "h": 45},
    {"text": "Store", "x": 887, "y": 533, "w": 138, "h": 45},
    {"text": "加", "x": 269, "y": 625, "w": 44, "h": 45},
    {"text": "载", "x": 313, "y": 625, "w": 44, "h": 45},
    {"text": "存", "x": 357, "y": 625, "w": 44, "h": 45},
    {"text": "储", "x": 401, "y": 625, "w": 44, "h": 45},
    {"text": "指", "x": 445, "y": 625, "w": 44, "h": 45},
    {"text": "令", "x": 489, "y": 625, "w": 44, "h": 45},
    {"text": "可", "x": 533, "y": 625, "w": 44, "h": 45},
    {"text": "分", "x": 577, "y": 625, "w": 44, "h": 45},
    {"text": "为", "x": 621, "y": 625, "w": 44, "h": 45},
    {"text": "3", "x": 665, "y": 625, "w": 24, "h": 45},
    {"text": "类", "x": 693, "y": 625, "w": 44, "h": 45},
    {"text": "，", "x": 737, "y": 625, "w": 24, "h": 45},
    {"text": "单", "x": 765, "y": 625, "w": 44, "h": 45},
    {"text": "寄", "x": 809, "y": 625, "w": 44, "h": 45},
    {"text": "存", "x": 853, "y": 625, "w": 44, "h": 45},
    {"text": "器", "x": 897, "y": 625, "w": 44, "h": 45},
    {"text": "/", "x": 941, "y": 625, "w": 24, "h": 45},
    {"text": "多", "x": 969, "y": 625, "w": 44, "h": 45},
    {"text": "寄", "x": 1013, "y": 625, "w": 44, "h": 45},
    {"text": "存", "x": 269, "y": 681, "w": 44, "h": 45},
    {"text": "器", "x": 313, "y": 681, "w": 44, "h": 45},
    {"text": "的", "x": 357, "y": 681, "w": 44, "h": 45},
    {"text": "加", "x": 401, "y": 681, "w": 44, "h": 45},
    {"text": "载", "x": 445, "y": 681, "w": 44, "h": 45},
    {"text": "存", "x": 489, "y": 681, "w": 44, "h": 45},
    {"text": "储", "x": 533, "y": 681, "w": 44, "h": 45},
    {"text": "指", "x": 577, "y": 681, "w": 44, "h": 45},
    {"text": "令", "x": 621, "y": 681, "w": 44, "h": 45},
    {"text": "及", "x": 665, "y": 681, "w": 44, "h": 45},
    {"text": "数", "x": 709, "y": 681, "w": 44, "h": 45},
    {"text": "据", "x": 753, "y": 681, "w": 44, "h": 45},
    {"text": "交", "x": 797, "y": 681, "w": 44, "h": 45},
    {"text": "换", "x": 841, "y": 681, "w": 44, "h": 45},
    {"text": "指", "x": 885, "y": 681, "w": 44, "h": 45},
    {"text": "令", "x": 929, "y": 681, "w": 44, "h": 45},
    {"text": "LD", "x": 273, "y": 773, "w": 44, "h": 45},
    {"text": "R", "x": 320, "y": 773, "w": 30, "h": 45},
    {"text": "指", "x": 380, "y": 773, "w": 44, "h": 45},
    {"text": "令", "x": 424, "y": 773, "w": 44, "h": 45},
    {"text": "为", "x": 468, "y": 773, "w": 44, "h": 45},
    {"text": "多", "x": 512, "y": 773, "w": 44, "h": 45},
    {"text": "寄", "x": 556, "y": 773, "w": 44, "h": 45},
    {"text": "存", "x": 600, "y": 773, "w": 44, "h": 45},
    {"text": "器", "x": 644, "y": 773, "w": 44, "h": 45},
    {"text": "加", "x": 688, "y": 773, "w": 44, "h": 45},
    {"text": "载", "x": 732, "y": 773, "w": 44, "h": 45},
    {"text": "扌", "x": 776, "y": 773, "w": 22, "h": 45},
    {"text": "旨", "x": 800, "y": 773, "w": 44, "h": 45},
    {"text": "令", "x": 844, "y": 773, "w": 44, "h": 45},
    {"text": "字", "x": 888, "y": 773, "w": 44, "h": 45},
    {"text": "数", "x": 932, "y": 773, "w": 44, "h": 45},
    {"text": "据", "x": 976, "y": 773, "w": 44, "h": 45},
]


class OcrParseTests(unittest.TestCase):
    def test_large_circle_noise_does_not_merge_adjacent_answers(self):
        # 2026-10-28 现场：B/C 圆钮被读成 0/O，66px 高的框曾把两项正文粘在一起。
        boxes = [
            {"text": "单选题", "x": 42, "y": 19, "w": 117, "h": 38},
            {"text": "1分", "x": 197, "y": 29, "w": 45, "h": 29},
            {"text": "已完成", "x": 919, "y": 26, "w": 62, "h": 21},
            {"text": "关于Arm伪指令说法正确的是0", "x": 121, "y": 185, "w": 565, "h": 40},
            {"text": "0", "x": 132, "y": 441, "w": 65, "h": 66},
            {"text": "O", "x": 132, "y": 544, "w": 65, "h": 66},
            {"text": "ADR伪指令用于中范围地址读取", "x": 230, "y": 353, "w": 554, "h": 38},
            {"text": "ADRL伪指令用于大范围地址读取", "x": 230, "y": 455, "w": 574, "h": 39},
            {"text": "LDR伪指令用于小范围地址读取", "x": 233, "y": 558, "w": 544, "h": 38},
            {"text": "ARM伪指令主要有4条", "x": 230, "y": 660, "w": 389, "h": 39},
        ]
        got = parse_ocr_question(boxes, 4)
        self.assertEqual(got["kind"], "单选")
        self.assertIn("关于Arm伪指令", got["stem"])
        self.assertEqual([o["label"] for o in got["options"]], list("ABCD"))
        self.assertTrue(got["options"][0]["text"].startswith("ADR伪指令"))
        self.assertTrue(got["options"][1]["text"].startswith("ADRL伪指令"))
        self.assertTrue(got["options"][2]["text"].startswith("LDR伪指令"))

    def test_real_hex_question(self):
        got = parse_ocr_question(Q1, 4)
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "单选")
        self.assertIn("立即数", got["stem"])
        self.assertEqual([o["label"] for o in got["options"]], ["A", "B", "C", "D"])
        self.assertEqual(got["options"][0]["text"], "0x0103c000")
        self.assertEqual(got["options"][3]["text"], "0x135")

    def test_real_arm_question_split_words(self):
        got = parse_ocr_question(Q2, 4)
        self.assertIsNotNone(got)
        # 题干：被拆开的英文单词要粘回去，末尾的 0 要还原成（）
        self.assertIn("Load/Store", got["stem"])
        self.assertTrue(got["stem"].endswith("（）"), got["stem"])
        self.assertEqual(got["options"][0]["text"], "把数据从寄存器传送到存储器叫Load")
        self.assertEqual(got["options"][1]["text"], "把数据从存储器传送到寄存器叫Store")
        # 折行要并回同一个选项
        self.assertIn("多寄存器", got["options"][2]["text"])
        self.assertEqual(got["options"][3]["text"], "LDR指令为多寄存器加载指令字数据")

    def test_grouping_splits_options(self):
        blocks = group_blocks(merge_line_boxes(Q1))
        # 题型行 + 题干 + 4 个选项
        self.assertEqual(len(blocks), 6)

    def test_without_option_count_falls_back(self):
        got = parse_ocr_question(Q1)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(len(got["options"]), 2)

    def test_flat_circle_on_option_row_does_not_prefix_text(self):
        # 2026-10-29 现场：圆钮 52x52、正文行高 41，旧的 1.3 倍阈值（53.3）卡不住，
        # 「0」被并进同行选项，得到「0不可靠的面向连接的」。
        boxes = [
            {"text": "单选题", "x": 42, "y": 19, "w": 117, "h": 38},
            {"text": "10分", "x": 197, "y": 29, "w": 60, "h": 30},
            {"text": "倒计时 00：47", "x": 887, "y": 26, "w": 130, "h": 22},
            {"text": "基于TCP/丨P的互联网服务中，|P协议提供主机", "x": 121, "y": 140, "w": 900, "h": 41},
            {"text": "之间的（ ）分组传输服务。", "x": 121, "y": 185, "w": 700, "h": 41},
            {"text": "可靠的面向连接的", "x": 230, "y": 270, "w": 400, "h": 41},
            {"text": "0", "x": 148, "y": 352, "w": 52, "h": 52},
            {"text": "不可靠的面向连接的", "x": 230, "y": 358, "w": 400, "h": 41},
            {"text": "可靠的无连接的", "x": 230, "y": 440, "w": 400, "h": 41},
            {"text": "0", "x": 148, "y": 515, "w": 52, "h": 52},
            {"text": "不可靠的无连接的", "x": 230, "y": 521, "w": 400, "h": 41},
            {"text": "提交答案", "x": 880, "y": 611, "w": 150, "h": 45},
        ]
        got = parse_ocr_question(boxes, 4)
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "单选")
        self.assertEqual(got["stem"], "基于TCP/IP的互联网服务中，IP协议提供主机之间的（）分组传输服务。")
        self.assertEqual([o["text"] for o in got["options"]],
                         ["可靠的面向连接的", "不可靠的面向连接的",
                          "可靠的无连接的", "不可靠的无连接的"])
        self.assertEqual([o["label"] for o in got["options"]], list("ABCD"))

    def test_footer_submit_button_never_becomes_an_option(self):
        # 没有走 _is_ocr_noise 时，页脚「提交答案」会顶掉最后一个选项。
        boxes = [
            {"text": "单选题", "x": 42, "y": 19, "w": 117, "h": 38},
            {"text": "IP层的功能不包括（ ）。", "x": 121, "y": 140, "w": 600, "h": 41},
            {"text": "差错处理", "x": 230, "y": 270, "w": 300, "h": 41},
            {"text": "数据报路由选择", "x": 230, "y": 352, "w": 300, "h": 41},
            {"text": "无连接的数据报传输", "x": 230, "y": 434, "w": 300, "h": 41},
            {"text": "提供可靠连接", "x": 230, "y": 516, "w": 300, "h": 41},
            {"text": "提交答案", "x": 880, "y": 611, "w": 150, "h": 45},
        ]
        got = parse_ocr_question(boxes, 4)
        self.assertEqual([o["text"] for o in got["options"]],
                         ["差错处理", "数据报路由选择", "无连接的数据报传输", "提供可靠连接"])
        self.assertNotIn("提交答案", [o["text"] for o in got["options"]])


class OcrEncodingTests(unittest.TestCase):
    """2026-10-29 线上事故：PowerShell 按控制台代码页（CP936）写 stdout，
    旧代码固定按 UTF-8 解码并 errors="ignore"，中文整段被吞，只剩 ASCII。"""

    def test_utf8_bytes_still_decode(self):
        raw = "单选题\r\n题干\r\n".encode("utf-8")
        self.assertEqual(_decode_ps_output(raw), "单选题\r\n题干\r\n")

    def test_gbk_bytes_are_not_swallowed(self):
        raw = "单选题\r\nA 可靠的面向连接的\r\n".encode("gbk")
        got = _decode_ps_output(raw)
        self.assertIn("单选题", got)
        self.assertIn("可靠的面向连接的", got)

    def test_utf16_bytes_decode(self):
        raw = "单选题\r\n".encode("utf-16-le")
        self.assertEqual(_decode_ps_output(raw), "单选题\r\n")

    def test_pipe_l_ocr_confusion_is_repaired(self):
        self.assertEqual(fix_ocr_text("基于TCP/丨P的互联网服务中，|P协议"), "基于TCP/IP的互联网服务中，IP协议")


if __name__ == "__main__":
    unittest.main()
