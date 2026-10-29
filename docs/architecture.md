# 架构与实现说明

这份文档写给想改代码的人：各模块职责、一次「抓题 → 出答案 → 提交」的完整链路，以及几个踩过坑的地方。

## 1. 模块职责

| 文件 | 职责 |
| --- | --- |
| `ykt_watcher.py` | 浏览器版全部逻辑：启动 Edge、进课堂、抓题、调模型、浮层、自动填/提交；单文件里也内嵌了三段注入页面的 JS（`PROBE_JS` / `OVERLAY_JS` / `SUBMIT_FALLBACK_JS` / `SLIDE_JS`） |
| `ykt_llm.py` | 模型侧：解析 API Key（参数 → 环境变量 → DSH 凭据）、调用 OpenAI 兼容接口、把模型返回规整成 `selected` 字母列表 |
| `ocr_win.py` | 图片题 OCR：调用 Windows 自带 `Windows.Media.Ocr`，把逐词坐标拼成行、行分块，再解析成「题型 / 题干 / 选项」 |
| `win_ocr.ps1` | 上述 PowerShell 脚本的落盘副本（`ocr_win.py` 会按内嵌内容自动重写，可单独命令行调用调试） |
| `ykt_http.py` | HTTP 版：不开浏览器，用 Cookie 走 HTTP + WebSocket 收题、上传答案；`ykt_api.py` 是它的兼容入口，`ykt_http_menu.py` 是双击 .bat 用的中文菜单 |
| `mock_quiz.html` | 文字题模拟页，`--mock` 用（倒计时归零自动切第二题多选） |
| `mock_image.html` | 图片题模拟页（只有一张图 + 倒计时），用来单独验证 OCR 链路 |

## 2. 浏览器版运行流程

```
main() → watch()
  ├─ open_browser()            # 持久化 Edge，profile 在 edge_profile\
  ├─ 每次轮询：
  │   ├─ 遍历 page.frames → classify_page()   # 首页 / 登录页 / 课堂页 / 答题页 / 陌生页
  │   ├─ auto_enter 时 current_live_lesson()  # GET /api/v3/classroom/on-lesson 拿 lessonId 并跳转
  │   ├─ probe_page()                         # 见 §3，返回题目 dict 或 None
  │   ├─ 新题（signature 变化）：
  │   │   ├─ 截图到 shots\、写 ykt_watch.log
  │   │   ├─ asyncio.create_task(ask_llm())   # 后台跑，不阻塞轮询
  │   │   ├─ 结果落到浮层 + answers.jsonl
  │   │   └─ auto_fill 时 auto_answer()
  │   └─ 题消失超过 question_gone_reset 秒 → 回到等待
  └─ 到达 --duration / --window 终点后退出
```

模型调用是异步任务：题目先显示，答案后到；同一道题（`signature` 相同）在 `answer_cache_ttl` 内直接复用缓存。

## 3. 抓题：DOM 优先，图片兜底

### 3.1 DOM 路（`PROBE_JS`）

每个 frame 都跑一遍，流程是：收集候选容器 → 打分取最高 → 解析选项与题干。

- 候选容器来源：`config.selectors` 命中、「提交」按钮向上 4 层、文本锚点（页面里出现 ≥2 个 `A.`/`B.` 行）、`body` 兜底；
- 打分：选项数 ≥2 加 5，出现题型词加 2，有提交按钮加 2，有倒计时加 2，文本越长加 1，`body` 减 4；
- `score < 6` 时再用文本锚点重试，`score < 3` 直接判无题；
- 命中后给元素打锚点：`data-ykt-question`（题目根）、`data-ykt-opt="A"`（选项按钮）、`data-ykt-submit="1"`（提交按钮），下一次探测先清旧锚点。

选项解析有两条路：DOM 结构（radio / 小圆钮 / `[class*=option]`）和纯文本（`A. xxx` 行）。DOM 结果少于 2 个时才退回文本解析。

### 3.2 何时判定「DOM 不够」

`probe_page` 认为 DOM 结果「可信」的条件是：

- 有题干，且 ≥2 个选项的文本都非空；或
- 没有选项但题型是判断/填空/简答/投票。

不满足就降级到图片路。真实课堂里大量题目是「整页 PPT 截图 + 只有 A/B/C/D 圆钮在 DOM 里」，正好走这条。

### 3.3 图片路（`SLIDE_JS` + `ocr_win`）

1. `SLIDE_JS` 在 frame 里找 `img, canvas`，要求宽 ≥240、高 ≥150、面积 ≥ 视口 15%，按 URL 关键词（`qn-` / `slide` / `ppt` / `cover` / `problem`）、标签、面积排序，取前 4 个候选；
2. 截图前先把浮层 `display:none`（`_set_overlay_visible`），避免把自己的答案 OCR 进去；
3. `ocr_boxes()` 调 PowerShell + WinRT OCR，逐词输出坐标；`parse_ocr_question(n_options)` 拼行、分块、去噪、映射 A/B/C/D；
4. DOM 里已有圆钮时，`n_options` 用 DOM 的选项个数，并要求 OCR 解析出的标签与 DOM 标签**完全一致**才放行；
5. 解析不完整时用 Pillow 放大 2 倍重识别一次；
6. 结果按 `(frame.url, 图片 src, 宽, 高, n_options)` 缓存 `ocr_cache` 秒（默认 8）。

OCR 解析不出来时保持沉默，不做猜测——`plausible` 守卫要求「认出题型 + 至少两个选项」。

## 4. 自动作答与提交

- `auto_answer()` 只点 `advice["selected"]` 里的字母；页面已有选择则跳过，绝不覆盖手选；
- 每个字母都要求在 DOM 里找到对应的、带 `box` 的可点按钮，缺一个就只填不交；
- 点完重读页面确认选中态（Vue 重绘会换节点，所以每次都重新定位锚点）；
- `click_submit()` 可能找不到按钮——真实页面的「提交答案」经常在题目容器**外面**，此时跑 `SUBMIT_FALLBACK_JS` 全页找可见、未禁用的提交按钮并补打锚点；
- 「已点击提交」只代表点击事件已发出，是否成功以页面反馈为准。

## 5. 踩过的坑（改代码前先读）

### 5.1 PowerShell 输出的编码

`ocr_boxes()` 早期用 `encoding="utf-8", errors="ignore"` 读子进程 stdout。但 `powershell.exe` 默认按**父进程控制台代码页**输出，中文 Windows 的 cmd 是 CP936(GBK)：GBK 字节按 UTF-8 解码全被丢弃，只剩 `A`/`B` 这类 ASCII，于是题目退化成「两个选项、没有题干」。

现在两边都做了处理：

- `win_ocr.ps1` 开头强制 `[Console]::OutputEncoding = UTF8` 和 `$OutputEncoding = UTF8`；
- Python 侧拿原始字节走 `_decode_ps_output()`：有 `\x00` 先试 UTF-16，否则 UTF-8-BOM → GBK → UTF-8(`errors=ignore`) 兜底。

### 5.2 OCR 圆钮与正文粘连

PPT 上的 A/B/C/D 圆钮常被识别成字符 `0`。判断圆钮不能只看「比正文高 1.3 倍」：实测圆钮 52px、正文行高 41px，阈值卡不住，圆钮会被并进同行文本（`0不可靠的面向连接的`）。现在用 `_is_circle()`：单字符 `0/O/o` + 高于行高 1.1 倍 + 宽高比 0.7–1.45，并且分块后还会用 `_strip_circle_prefix()` 兜底去掉 `0` 前缀。

### 5.3 页脚按钮混进选项

「提交答案」这类按钮如果落在截图里，会被当成第 N 个选项，导致整题错位一位。`parse_ocr_question` 里对整行等于 提交/确定/交卷/下一题/已完成 之类的块直接剔除。

### 5.4 同一份 `edge_profile` 只能开一个浏览器

`--login`、`--mock`、`--once`、`--dump` 和正式监控都用同一个持久化 profile。已经有一个实例在跑时再启动会失败，用 `--kill-stale` 清残留进程。

### 5.5 窗口最小化会冻结渲染

Chromium 对最小化页面暂停渲染，纯 DOM 题不受影响，但图片题可能截到旧画面。需要后台跑就用 `--headless`。

## 6. 数据文件

`answers.jsonl` 每行一道题：

```json
{"time": "...", "kind": "单选", "stem": "...", "options": [{"label": "A", "text": "...", "checked": false, "box": {"x": 0, "y": 0, "w": 0, "h": 0}}],
 "countdown_at_detect": 58, "answer": "A", "answer_text": "...", "confidence": 0.85, "reason": "...", "signature": "f0f8c7081f1c6b43"}
```

`dump\census_*.json` 是现场快照：页面 URL、每个 frame 的可点元素统计、`probe_details`（DOM 与 OCR 两路各自的中间结果、OCR 引擎语言、原始 OCR 文本前 300 字）。排查「为什么没认出来」先看这里。

## 7. 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_dom.py` | 选项锚点、图片题 OCR 绑定、提交按钮在容器外、重绘后重定位、禁用/无效点击、只填不交等 18 项 |
| `tests/test_ocr.py` | 编码解码（UTF-8/GBK/UTF-16）、圆钮污染、页脚按钮误判、`丨P → IP` 等 12 项 |
| `tests/test_llm.py` | Key 优先级（参数 → config → 环境变量 → DSH 凭据）、`mask_key`、配置读写回环 9 项 |
| `tests/test_http.py` | HTTP 版的解析与上传（需要 `requirements-http.txt`） |

DOM 用例用本地 HTML 夹具 + mock 掉 OCR，不联网、不需要登录；`tests/test_dom.py` 里也保留了真实漏抓样本的 OCR 坐标，作为回归数据。

## 8. 扩展点

- 页面改版抓不到：先用 `--dump` 存 HTML，再往 `config.selectors` 里补选择器；
- 换模型：改 `config.json` 的 `llm.base_url` / `model`，任何 OpenAI 兼容接口都行；
- 换 OCR：`_read_question_image()` 只依赖 `ocr_boxes()` 的「行 + 坐标」输出，替换成别家 OCR 时保持这个返回结构即可。
