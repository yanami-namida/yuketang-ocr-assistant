# 架构与实现说明

这份文档写给想改代码的人：各模块职责、一次「抓题 → 出答案」的完整链路，以及几个踩过坑的地方。

## 1. 模块职责

| 文件 | 职责 |
| --- | --- |
| `ykt_watcher.py` | 主程序全部逻辑：启动 Edge、进课堂、抓题、调模型、浮层、日志；单文件里也内嵌了注入页面的 JS（`PROBE_JS` / `OVERLAY_JS` / `SLIDE_JS`） |
| `ykt_llm.py` | 模型侧：解析 API Key（参数 → 本地配置 → 环境变量 → 可选 DSH 凭据）、统一基础/完整接口地址、调用模型、把模型返回规整成 `selected` 字母列表 |
| `ocr_win.py` | 图片题 OCR：调用 Windows 自带 `Windows.Media.Ocr`，把逐词坐标拼成行、行分块，再解析成「题型 / 题干 / 选项」 |
| `win_ocr.ps1` | 上述 PowerShell 脚本的落盘副本（`ocr_win.py` 会按内嵌内容自动重写，可单独命令行调用调试） |
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
  │   │   └─ 结果落到浮层 + answers.jsonl
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

### 3.3 默认图片路（`SLIDE_JS` + 多模态模型）

默认 `image_mode=auto`：`check_capabilities()` 验证文本后，`probe_vision_capability()` 将随机数字仅绘制在 PNG 像素中，请求模型读取并核对。200 状态码本身不算通过；明确拒绝图片和暂时失败分别记录。缓存绑定地址、模型、密钥的 SHA-256 标识及推理设置。桌面连接测试保存结果；启动监控时复用有效缓存，未检测/过期时测试一次，只有 `supported=true` 使用下述视觉路，其余走 §3.4。手动 `vision/ocr` 绕过检测。

1. DOM 先检测当前答题控件，`probe_vision_question()` 选取同一 frame 的大图与实际选项字母区域；没有答题控件的普通 PPT 不触发视觉请求。
2. 隐藏助手浮层后截图；PNG 以 Base64 `image_url` 与文字指令一起发送到现有 Chat Completions API。不需要先完成 OCR。
3. 模型一次返回 `kind/stem/options/complete/answer/confidence/reason`。`apply_vision_reading()` 要求题干、选项正文非空，字母无重复、顺序和 DOM 完全一致，且 `complete=true`；DOM 的坐标与实时选中态保留。
4. 题目签名使用 frame URL、完整图片 URL（canvas 用内容）和按钮标签；倒计时、页脚 OCR 噪声、选中态不参与签名。截图复用，避免每轮重复发图；切图会生成新签名。
5. 模型任务绑定发起时的签名，过期结果不能应用到另一题。当前轮从答案缓存恢复识别正文；切图会生成新签名并重新识别，旧答案不会套到新题上。
6. `llm.vision_max_tokens` 默认 1800，便于完整返回选项；有倒计时时限制模型请求超时，4xx 参数错误直接返回，不盲目重试。

### 3.4 备用 OCR 路（`--no-vision` + `ocr_win`）

1. `SLIDE_JS` 在 frame 里找 `img, canvas`，要求宽 ≥240、高 ≥150、面积 ≥ 视口 15%，按 URL 关键词（`qn-` / `slide` / `ppt` / `cover` / `problem`）、标签、面积排序，取前 4 个候选；
2. 截图前先把浮层 `display:none`（`_set_overlay_visible`），避免把自己的答案 OCR 进去；
3. `ocr_boxes()` 调 PowerShell + WinRT OCR，逐词输出坐标；`parse_ocr_question(n_options)` 拼行、分块、去噪、映射 A/B/C/D；
4. DOM 里已有圆钮时，`n_options` 用 DOM 的选项个数，并要求 OCR 解析出的标签与 DOM 标签**完全一致**才放行；
5. 解析不完整时用 Pillow 放大 2 倍重识别一次；
6. 结果按 `(frame.url, 图片 src, 宽, 高, n_options)` 缓存 `ocr_cache` 秒（默认 8）。

OCR 解析不出来时保持沉默，不做猜测——`plausible` 守卫要求「认出题型 + 至少两个选项」。

## 4. 只读设计：为什么没有自动作答

公开版本没有 `click_option()` / `click_submit()` / `auto_answer()` / `run_auto_answer()`，也没有直接请求答题接口的后台版本。原因不是技术上做不到，而是「替用户点击并提交」会越过本项目的定位：

- 定位是「第二双眼睛」：只读页面、只给参考，最终动作由用户自己完成；
- 平台协议与学校规定通常禁止代答和自动化作答，发布这类代码等于鼓励代刷、代考；
- 只读实现可以完全避免误点、重复提交、把 A 题答案点到 B 题等风险。

代码里保留的只有读操作：

- `current_live_lesson()`：`GET /api/v3/classroom/on-lesson`，只读「哪节课在上」，随后导航到听课页，不签到；
- `PROBE_JS`：给选项/提交按钮打 `data-ykt-*` 锚点，仅用于定位和判断「这确实是一道题」；`probe_page()` 不点击任何元素；
- 浮层、截图、OCR、模型调用：全部是读取与展示。

如果将来要扩展，请保持这条边界：可以改选择器、换模型、换 OCR；不要新增点击选项、点击提交、请求答题接口或自动签到的代码。这类改动不会被合并。

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
| `tests/test_dom.py` | 选项锚点、图片题 OCR 绑定、禁用/无效控件识别、iframe、只读保证（识别结果不产生点击） |
| `tests/test_ocr.py` | 编码解码（UTF-8/GBK/UTF-16）、圆钮污染、页脚按钮误判、`丨P → IP` 等 12 项 |
| `tests/test_llm.py` | Key 优先级（参数 → config → 环境变量 → DSH 凭据）、`mask_key`、配置读写回环 9 项 |
| `tests/test_vision.py` | 视觉请求体、选项完整性与标签校验、图片身份与签名 |
| `tests/test_capabilities.py` | 图片能力探测、缓存绑定与失效 |
| `tests/test_desktop.py` | 桌面版参数、配置写入、确认不存在自动作答键 |

DOM 用例用本地 HTML 夹具 + mock 掉 OCR，不联网、不需要登录；`tests/test_dom.py` 里也保留了真实漏抓样本的 OCR 坐标，作为回归数据。

## 8. 扩展点

- 页面改版抓不到：先用 `--dump` 存 HTML，再往 `config.selectors` 里补选择器；
- 换模型：改 `config.json` 的 `llm.base_url` / `model`，任何 OpenAI 兼容接口都行；
- 换 OCR：`_read_question_image()` 只依赖 `ocr_boxes()` 的「行 + 坐标」输出，替换成别家 OCR 时保持这个返回结构即可。
