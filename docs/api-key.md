# 配置模型 API 与密钥

公开源码不附带 API Key。每个人使用自己的密钥，保存在本机配置或环境变量中。

Key 的用途：把抓到的题目发给大模型，换回一个参考答案。**没有 Key 也能跑** —— 只抓题、显示题干和倒计时，不生成答案。

---

## 1. 最快：双击 `设置APIKey.bat`

1. 双击项目根目录的 **`设置APIKey.bat`**（需要电脑装有 Python 3.10+）。
2. 选服务商（直接回车保持当前设置；首次默认为 DeepSeek），也可以选择“自定义”。
3. 粘贴 Key，回车（输入时不回显，粘贴后看不到是正常的）。
4. 脚本写入 `config.json` 并发起一次小型模型请求校验，可能产生少量费用；看到 `校验通过` 就成了。

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
| `--api-key-env NAME` | 设置密钥环境变量名；使用 `--env` 时也使用此名称 |
| `--reasoning-effort VALUE` | 配置提供方支持的推理强度，如 `none` / `low` / `high` |

---

## 3. 三种存 Key 的位置，优先级从高到低

| 优先级 | 位置 | 生效范围 | 适合谁 |
| --- | --- | --- | --- |
| 1 | `--api-key sk-xxx` 启动参数 | 本次运行 | 临时试一下 |
| 2 | `config.json` → `llm.api_key` | 这台机器这个项目 | **推荐**，双击脚本就写这里 |
| 3 | 环境变量 `DEEPSEEK_API_KEY`（或 `llm.api_key_env` 指定的名字） | 当前用户/整台机器 | 一台机器多个项目共用一把 Key |
| 4 | DSH 凭据 `%USERPROFILE%\.dsh\.credentials.yaml` | 本机 | 可选：需显式开启 `llm.allow_dsh_credentials` |
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

使用 Bearer 密钥鉴权、兼容 Chat Completions 请求与响应格式的接口可以接入，
改 `base_url` + `model` 即可（脚本里选 1–6 会自动填好，第 7 项可自定义）：

| 服务商 | `base_url` | 示例 `model` | Key 从哪申请 |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | https://platform.deepseek.com |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | https://open.bigmodel.cn |
| 阿里通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | https://bailian.console.aliyun.com |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` | https://cloud.siliconflow.cn |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` | 不需要，Key 随便填 `ollama` |
| 华为云 MaaS | `https://api.modelarts-maas.com/v2` | `deepseek-v4.1-flash-1017` | [MaaS 控制台](https://console.huaweicloud.com/modelarts/) |

`base_url` 可以是表中的基础地址，也可以直接填完整的 `/chat/completions` 地址。
脚本会统一处理，运行时不会重复追加路径。地址不能包含账号密码、查询参数或 URL 片段。
需要其他鉴权方式或完全不同响应格式的提供方需要单独适配。

```powershell
python tools\set_api_key.py --key sk-xxx --base-url https://open.bigmodel.cn/api/paas/v4 --model glm-4-flash
```

换服务商后建议跑一次 `python tools\set_api_key.py --check`，能看到 `404`（路径不对）、`401`（Key 不对）之类的明确原因。

### 华为云 MaaS 示例

```powershell
# 按提示输入密钥，不把真实密钥放入命令历史
python tools\set_api_key.py --yes --base-url https://api.modelarts-maas.com/v2 --model deepseek-v4.1-flash-1017 --api-key-env MAAS_API_KEY --reasoning-effort none

# 也支持用完整接口地址进行校验（此命令只临时覆盖，不保存新配置）
python tools\set_api_key.py --check --base-url https://api.modelarts-maas.com/v2/chat/completions --model deepseek-v4.1-flash-1017
```

完整请求地址为 `https://api.modelarts-maas.com/v2/chat/completions`，末尾没有 `svg`。
预设使用 `reasoning_effort: "none"` 关闭该模型的思考模式，减少课堂参考答案等待时间；
可按服务商支持情况改成其他值，设置为 JSON `null` 则不发送此字段。
模型名称和支持参数以 [华为云 MaaS V2 文档](https://support.huaweicloud.com/model-call-maas/model-call-019.html)为准。
只有密钥具有对应权限且账户额度可用时，调用才会成功。

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
| `404` | 检查服务商的路径版本、完整接口地址及模型名；不要误带 `svg` 等网页文字 |
| `连不上 ...: ConnectTimeout` | 网络/代理问题；公司网或校园网可能挡了 API 域名 |
| 日志里 `AI 建议` 迟迟不出 | 模型名不对（例如 `deepseek-chat` 写成 `deepseek-reasoner` 的旧名）或超时，`config.json` 可调 `llm.timeout` |
| 换 Key 后还是旧的 | 环境变量里有旧 Key；`--show` 看来源 |

---

## 6. 安全提醒

- `config.json` 在 `.gitignore` 中，正常 `git add` 会跳过；手动打包文件夹仍可能包含它。公开源码建议使用 README 中的 `git archive` 命令。
- 不要把 Key 写在命令行里截图、别提交到 GitHub、别放进 Issue。真泄露了去服务商控制台**吊销重建**。
- 模型请求包含题型、题干、选项及可用的剩余时间，不主动附带 Cookie 或截图；OCR 文字本身仍可能包含个人信息，详见 [安全与隐私](../SECURITY.md)。
- `--env` 会保存提供方设置并移除配置文件内的旧密钥，避免旧配置覆盖新环境变量。环境变量同样是本机明文存储，公用电脑不要持久保存密钥。

---

## 7. 没有 Key 时还能做什么

```powershell
python ykt_watcher.py --window 08:00-09:40
```

会正常抓题、在浮层里显示题干/选项/倒计时，只是「AI 建议」那栏是空的。适合只想看题、不想要答案的人。
