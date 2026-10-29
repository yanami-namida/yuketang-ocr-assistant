# 配置模型 API Key（给别人的最短上手说明）

这个项目**不附带任何 API Key**，源码和 `config.json` 里都没有。每个人用自己的 Key，只存在自己电脑上。

Key 的用途：把抓到的题目发给大模型，换回一个参考答案。**没有 Key 也能跑** —— 只抓题、显示题干和倒计时，不生成答案。

---

## 1. 最快：双击 `设置APIKey.bat`

1. 双击项目根目录的 **`设置APIKey.bat`**（需要电脑装有 Python 3.10+）。
2. 选服务商（直接回车 = DeepSeek）。
3. 粘贴 Key，回车（输入时不回显，粘贴后看不到是正常的）。
4. 脚本自动写入 `config.json` 并调用一次接口校验，看到 `校验通过` 就成了。

之后启动 `启动雨课堂助手.bat` 就会用这个 Key。

---

## 2. 命令行版（等价于双击）

在项目目录里打开 PowerShell：

```powershell
# 交互式：选服务商 + 粘贴 Key + 自动校验
python tools\set_api_key.py

# 一步到位（适合写进教程/脚本）
python tools\set_api_key.py --key sk-你的key --base-url https://api.deepseek.com/v1 --model deepseek-chat

# 只看当前用的是哪个 Key（打码显示来源）
python tools\set_api_key.py --show

# 只校验，不改文件
python tools\set_api_key.py --check

# 删掉 config.json 里的 Key
python tools\set_api_key.py --unset
```

其它参数：

| 参数 | 作用 |
| --- | --- |
| `--env` | 不写 `config.json`，改成写用户环境变量（`setx`，新开的窗口才生效） |
| `--session` | 只打印当前窗口临时生效的那行命令，不动文件 |
| `--config PATH` | 指定别的配置文件（测试用） |
| `--yes` | 跳过服务商选择，直接输入 Key |

---

## 3. 三种存 Key 的位置，优先级从高到低

| 优先级 | 位置 | 生效范围 | 适合谁 |
| --- | --- | --- | --- |
| 1 | `--api-key sk-xxx` 启动参数 | 本次运行 | 临时试一下 |
| 2 | `config.json` → `llm.api_key` | 这台机器这个项目 | **推荐**，双击脚本就写这里 |
| 3 | 环境变量 `DEEPSEEK_API_KEY`（或 `llm.api_key_env` 指定的名字） | 当前用户/整台机器 | 一台机器多个项目共用一把 Key |
| 4 | DSH 凭据 `%USERPROFILE%\.dsh\.credentials.yaml` | 本机 | 已经装了 DSH 的人，自动复用 |
| — | 都没有 | — | 只抓题不答题 |

> 想确认现在到底用的哪一把：运行 `python tools\set_api_key.py --show`，会显示「来源」。
> 换了 Key 不生效，先用 `--show` 看来源：如果显示的是环境变量或 DSH 凭据，说明你改的位置不是当前生效的那个（`config.json` 的 `llm.api_key` 优先级更高）。

手动改 `config.json` 的话，加在 `llm` 段里：

```json
"llm": {
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key": "sk-你的key",
  "api_key_env": "DEEPSEEK_API_KEY"
}
```

环境变量两种写法（都可以）：

```powershell
# 永久（写进用户环境变量，重开终端生效）
setx DEEPSEEK_API_KEY "sk-你的key"

# 只对当前窗口生效
$env:DEEPSEEK_API_KEY = "sk-你的key"
```

---

## 4. 换别的模型服务商

任何 OpenAI 兼容接口都能用，改 `base_url` + `model` 即可（脚本里选 1–5 会自动填好）：

| 服务商 | `base_url` | 示例 `model` | Key 从哪申请 |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | https://platform.deepseek.com |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | https://open.bigmodel.cn |
| 阿里通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | https://bailian.console.aliyun.com |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` | https://cloud.siliconflow.cn |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` | 不需要，Key 随便填 `ollama` |

```powershell
python tools\set_api_key.py --key sk-xxx --base-url https://open.bigmodel.cn/api/paas/v4 --model glm-4-flash
```

换服务商后建议跑一次 `python tools\set_api_key.py --check`，能看到 `404`（路径不对）、`401`（Key 不对）之类的明确原因。

---

## 5. 校验与常见错误

```powershell
python tools\set_api_key.py --check
```

| 现象 | 原因 / 解决 |
| --- | --- |
| `未找到模型 API Key：只抓题、不生成答案` | 三种来源都没配；跑一次 `设置APIKey.bat` |
| `401` | Key 打错、过期或被吊销；重新复制 |
| `402` | 账户余额不足，去控制台充值 |
| `403` | Key 没开通这个模型（比如没实名/没申请权限） |
| `404` | `base_url` 写错（注意结尾是 `/v1` 还是 `/v4`，别自己加 `/chat/completions`） |
| `连不上 ...: ConnectTimeout` | 网络/代理问题；公司网或校园网可能挡了 API 域名 |
| 日志里 `AI 建议` 迟迟不出 | 模型名不对（例如 `deepseek-chat` 写成 `deepseek-reasoner` 的旧名）或超时，`config.json` 可调 `llm.timeout` |
| 换 Key 后还是旧的 | 环境变量里有旧 Key；`--show` 看来源 |

---

## 6. 安全提醒

- `config.json` 在 `.gitignore` 里，**不会被 `git status` 看到、也不会被提交**；分享项目时不用删，但别手动把它发群里。
- 不要把 Key 写在命令行里截图、别提交到 GitHub、别放进 Issue。真泄露了去服务商控制台**吊销重建**。
- 项目只把「题型 + 题干 + 选项」发给模型，不发送 Cookie、学号、姓名、页面截图。
- 公用电脑上建议用 `--env`（用户环境变量）而不是 `config.json`，用完 `setx DEEPSEEK_API_KEY ""` 清掉。

---

## 7. 没有 Key 时还能做什么

```powershell
python ykt_watcher.py --window 08:00-09:40
```

会正常抓题、在浮层里显示题干/选项/倒计时，只是「AI 建议」那栏是空的。适合只想看题、不想要答案的人。
