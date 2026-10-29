# 雨课堂 HTTP 答案上传版

入口 `ykt_http.py`（原 `ykt_api.py` 转发到同一实现）。不导入 Playwright、不打开浏览器、不读 DOM：
Cookie 登录态 → HTTP 读课堂 → WebSocket 收发题事件 → HTTP 取课件 JSON → 图片题走 OCR → 模型出答案 → 可选 HTTP 上传。

---

## 1. 快速开始（命令）

```powershell
cd D:\_LLMWorkplace\__tool\雨课堂

# 只读：验证登录态 + 列出正在上的课（不签到、不进课堂）
.\.venv-http\Scripts\python.exe -B ykt_http.py --list-lessons

# 读一次已发布题目，不调模型、不上传
.\.venv-http\Scripts\python.exe -B ykt_http.py --lesson-id 123456 --once

# 盯课并预览答案（默认只打印，不上传）
.\.venv-http\Scripts\python.exe -B ykt_http.py --lesson-id 123456 --duration 45
.\.venv-http\Scripts\python.exe -B ykt_http.py --duration 45          # 只有一个活动课堂时可不给 ID

# 盯课并真的上传模型答案（会真实计分，慎用）
.\.venv-http\Scripts\python.exe -B ykt_http.py --lesson-id 123456 --duration 45 --auto-submit

# 调试：看缓存题目的完整 JSON（含 OCR 补全后的题干/选项）
.\.venv-http\Scripts\python.exe -B ykt_http.py --lesson-id 123456 --once --dump-questions
```

把 `123456` 换成真实 `lessonId`。不想敲命令：双击 **`启动雨课堂HTTP版.bat`**。

不指定 ID 时，只有「正在上课的课堂恰好一个」才会自动选择；没有活动课堂会退出并提示。

> 进入课堂监听（包含 `--once`）会调用平台 `checkin`，产生正常的课堂进入/签到行为；`--list-lessons` 和单题答案预览不调用该接口。

---

## 2. 工作流程

现有 Cookie 登录态 → HTTP 读取课堂列表 → HTTP 进入课堂并获取鉴权 → WebSocket 接收发题事件 → HTTP 获取课件 JSON → 模型生成答案 → 再次查询题目状态 → HTTP POST 上传答案并校验成功码。

题目推送用 WebSocket，课件读取和答案上传用 HTTP。指定题号和答案的单题入口只走 HTTP。

## 3. 图片题与 OCR

真实随堂题的正文常常不在 JSON 里：`body` 为空、`options[].value` 为空，题干和选项都画在一张 PPT 图片上（`cover` 字段，`qn-st0.yuketang.cn`）。所以收到新题后会：

1. 下载该题的 `cover` 原图到 `shots\http_<题号>.png`（用 `cover` 而不是 `coverAlt`——后者常被 CDN 转成 webp，Windows OCR 不认）；
2. 调 Windows 自带 OCR（`Windows.Media.Ocr`，经 `ocr_win.py`）逐词识别，按坐标把折行、被拆开的英文单词（`L oa d`）拼回整行；
3. 按纵向间距把行分块，第 1 块是题干，最后 N 块按顺序对应 A/B/C/D（N 来自接口里的选项个数）；
4. 补全后的题干和选项交给模型，再走原来的置信度检查与上传流程。

只有 Windows 有这套 OCR；识别失败时退化为只显示题号。`--no-ocr` 可关闭。识别文字可能有错字（例如 `指令` 被认成 `扌旨令`），模型提示词里已说明「这是 OCR 文字，可能有噪音」。

## 4. 环境与登录态

本目录已创建独立环境 `.venv-http`。迁移到其它机器：

```powershell
python -m venv .venv-http
.\.venv-http\Scripts\python.exe -m pip install -r requirements-http.txt
```

只依赖 `requests` 和 `websocket-client`。

登录态来自浏览器版的 `ykt_state.json`；过期时用**浏览器版的环境**重新扫码（登录仍需 Playwright）：

```powershell
python ykt_watcher.py --login
```

也可用 `--state` 指定另一个 Playwright storage_state JSON。

## 5. 已知答案直接上传

先预览请求 JSON（无需登录、不联网）：

```powershell
.\.venv-http\Scripts\python.exe -B ykt_http.py --problem-id 987654321 --problem-type 1 --answer B
```

真实上传需明确课堂 ID 并加 `--auto-submit`：

```powershell
.\.venv-http\Scripts\python.exe -B ykt_http.py --lesson-id 123456 --problem-id 987654321 --problem-type 1 --answer B --auto-submit
```

单题入口按指定答案直接请求平台，无模型置信度/图片检查；平台负责校验权限和题目状态。

| problemType | 题型 | `--answer` 示例 | HTTP result |
| --- | --- | --- | --- |
| 1 | 单选 | `B` | `["B"]` |
| 2 | 多选 | `ACD` | `["A","C","D"]` |
| 3 | 投票 | `A` | `["A"]` |
| 4 | 填空 | `"第一空；第二空"` | `["第一空","第二空"]` |
| 5 | 简答 | `"文字答案"` | `{"content":"文字答案","pics":[]}` |

payload 包含字符串 `problemId`、整数 `problemType`、毫秒时间戳 `dt` 和 `result`。判断题按实际选项标签提交；模型输出「正确/错误」时会映射到题目里的选项。

## 6. 配置

继承 `config.json` 的模型配置和时长，再叠加可选的 `config.http.json`。需要修改时复制 `config.http.example.json` 为 `config.http.json`，或用 `--config` 指定。

| 配置/参数 | 用途 |
| --- | --- |
| `http.base` / `--base` | 根地址，默认长江雨课堂，仅接受雨课堂 HTTPS 域名 |
| `http.lesson_id` / `--lesson-id` | 课堂 ID |
| `http.state_file` / `--state` | 登录态文件，相对路径从脚本目录解析 |
| `http.timeout` | HTTP 请求与课堂握手超时秒数，默认 15 |
| `http.reconnect_delay` | 断线重连基础等待秒数，默认 3 |
| `http.min_confidence` | 自动上传的最低置信度，默认 0.7 |
| `http.type_map` | 题型编号映射，可按站点差异校准 |
| `http.journal` | 上传记录文件，默认 `http_submissions.jsonl` |
| `http.answers_file` | 模型答案记录，默认 `http_answers.jsonl` |
| `--model` / `--base-url` / `--api-key` | 覆盖模型设置，仍支持环境变量与 DSH 凭据 |
| `--no-ocr` | 关闭题目图片 OCR（只看接口文字，通常为空） |
| `--dump-questions` | 配合 `--once` 打印缓存题目完整 JSON（含 OCR 结果） |

浏览器版的 `auto_fill` / `auto_submit` 配置不会启用 HTTP 上传，由 `--auto-submit` 开启。HTTP 版没有网页浮层和截图。日志是 `ykt_http.log`。

## 7. 上传结果与去重

每次上传前先把 `attempting` 写盘，再发一次 POST，最后追加结果。同一站点/账号/课堂/题号已有上传尝试时不会自动重发；文件锁防止两个上传进程同时写同一记录文件。

| 状态 | 含义 |
| --- | --- |
| `submitted` | HTTP 200 且业务 `code=0`，服务器确认成功 |
| `rejected` | 平台拒绝或返回 HTTP 4xx |
| `unknown` | 网络异常、5xx、重定向或响应不完整，需到网页核对 |
| `attempting` | 上传开始后进程退出，结果未记录，需要核对 |

超时不会自动重试 POST。要重试某题：先在网页核对实际结果，停止脚本并备份记录，再只移除该题对应的记录行——不要清空整个文件重跑。

自动流程会跳过已作答、已过期、缺少选项、无效答案和低置信度答案；图片题会先做 OCR 补全（识别失败才按缺题干处理）。无模型 Key 时只展示题目。停止监听后，正在进行的模型请求仍会等到超时退出，但不会继续上传。

## 8. 验证及限制

```powershell
.\.venv-http\Scripts\python.exe -B -m unittest discover -s tests -v
```

离线测试模拟 HTTP 响应和课堂消息，覆盖「发题 → 模型 → 状态复查 → HTTP 上传」、Cookie/CSRF、题型转换、失败状态、超时不重试、重启去重、进程锁和预览模式；
`tests\test_ocr.py` 用三道真实题目的 OCR 词块做回归（折行合并、被拆英文拼回、把 OCR 出来的 `0` 还原成 `（）`）。

已实测：登录态可调用用户接口、课堂列表与 `on-lesson`；能连上真实课堂、缓存到 3 道真题并 OCR 出题干与选项。**真实发题瞬间的端到端上传**尚未在有活动题时验证。

范围是课堂随堂题，不含独立考试系统、整卷提交、视频学习或作业系统接口。

协议参考：[课堂 HTTP/WebSocket 实现](https://github.com/chlchi/RainClassroomAssitant-standalone/blob/main/Scripts/Classes.py)、[较新前端中的题型与答案格式](https://greasyfork.org/en/scripts/531469-ai%E9%9B%A8%E8%AF%BE%E5%A0%82%E5%8A%A9%E6%89%8B-js%E7%89%88/code)。默认采用后者的 1–5 题型编号，不同版本不可混用映射。
