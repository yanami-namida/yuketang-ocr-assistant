# -*- coding: utf-8 -*-
"""配置模型 API Key：写进本地 config.json 的 llm.api_key，或设为用户环境变量。

用法（在项目目录下）：
    python tools/set_api_key.py                 # 交互式：粘贴 key，自动校验并写 config.json
    python tools/set_api_key.py --check         # 校验当前配置里的 key 是否可用
    python tools/set_api_key.py --show          # 看当前 key（打码）和来源
    python tools/set_api_key.py --unset         # 从 config.json 删除 llm.api_key
    python tools/set_api_key.py --env           # 改为写入用户环境变量而不是 config.json

Windows 上也可以直接双击 设置APIKey.bat。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent.parent
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))
from ykt_paths import data_dir, resource_dir
from ykt_llm import chat_completions_url, normalize_base_url
BASE_DIR = data_dir()
CONFIG_FILE = BASE_DIR / "config.json"
EXAMPLE_FILE = resource_dir() / "config.example.json"

DEFAULT_LLM = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key_env": "DEEPSEEK_API_KEY",
    "allow_dsh_credentials": False,
}

PROVIDERS = {
    "1": ("DeepSeek（深度求索）", "https://api.deepseek.com/v1", "deepseek-chat"),
    "2": ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "3": ("阿里通义千问（DashScope 兼容模式）", "https://dashscope.aliyuncs.com/compatible-mode/v1",
          "qwen-plus"),
    "4": ("硅基流动 SiliconFlow", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
    "5": ("本地 Ollama（无需 key）", "http://127.0.0.1:11434/v1", "qwen2.5:7b"),
    "6": ("华为云 ModelArts MaaS", "https://api.modelarts-maas.com/v2",
          "deepseek-v4.1-flash-1017"),
}


# ----------------------------------------------------------------------------
# 配置文件读写
# ----------------------------------------------------------------------------
def load_raw_config(path: Path | None = None) -> dict:
    """读取配置或样例；只有保存时才创建文件。"""
    path = path or CONFIG_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"config.json 解析失败：{exc}\n请修好它或删掉后重新运行本脚本。")
    if EXAMPLE_FILE.exists():
        return json.loads(EXAMPLE_FILE.read_text(encoding="utf-8-sig"))
    return {"llm": dict(DEFAULT_LLM)}


def save_raw_config(cfg: dict, path: Path | None = None) -> None:
    path = path or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_llm(cfg: dict) -> dict:
    llm = cfg.setdefault("llm", {})
    llm.setdefault("base_url", DEFAULT_LLM["base_url"])
    llm.setdefault("model", DEFAULT_LLM["model"])
    llm.setdefault("api_key_env", DEFAULT_LLM["api_key_env"])
    return llm


# ----------------------------------------------------------------------------
# key 的显示 / 校验
# ----------------------------------------------------------------------------
def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return "（空）"
    if len(key) <= 12:
        return key[:2] + "*" * (len(key) - 2)
    return f"{key[:6]}{'*' * (len(key) - 10)}{key[-4:]}"


def resolve(cfg: dict, explicit: str | None = None) -> tuple[str | None, str]:
    """与 ykt_llm.resolve_api_key 相同的优先级，返回 (key, 来源)。"""
    llm = get_llm(cfg)
    if explicit:
        return explicit.strip(), "--key 参数"
    saved = (llm.get("api_key") or "").strip()
    if saved:
        return saved, "config.json 的 llm.api_key"
    env_name = llm.get("api_key_env") or "DEEPSEEK_API_KEY"
    env = os.environ.get(env_name)
    if env:
        return env.strip(), f"环境变量 {env_name}"
    dsh = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh" / ".credentials.yaml"
    if llm.get("allow_dsh_credentials", False) and dsh.exists():
        import re
        m = re.search(rf"{re.escape(env_name)}\s*:\s*['\"]?([A-Za-z0-9_\-\.]+)",
                      dsh.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return m.group(1), "DSH 凭据 ~/.dsh/.credentials.yaml"
    return None, "未配置"


def check_key(cfg: dict, key: str) -> tuple[bool, str]:
    """用一次小型 chat 请求验证地址、鉴权和模型；可能产生少量费用。"""
    import requests

    llm = get_llm(cfg)
    try:
        url = chat_completions_url(llm.get("base_url") or DEFAULT_LLM["base_url"])
    except ValueError as exc:
        return False, str(exc)
    headers = {"Authorization": f"Bearer {key}"}
    payload = {"model": llm.get("model") or DEFAULT_LLM["model"],
               "messages": [{"role": "user", "content": "Reply OK."}],
               "max_tokens": 8, "stream": False}
    if llm.get("reasoning_effort") is not None:
        payload["reasoning_effort"] = llm["reasoning_effort"]
    try:
        r = requests.post(
            url,
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=llm.get("timeout", 60),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return False, f"连接失败：{type(exc).__name__}"
    if r.status_code == 200:
        try:
            message = r.json()["choices"][0]["message"]
            if not isinstance(message, dict):
                raise ValueError("missing message")
        except (ValueError, KeyError, IndexError, TypeError):
            return False, "返回 200，但响应不是兼容的 Chat Completions JSON"
        return True, "地址、鉴权和模型调用通过（POST /chat/completions）"
    if r.status_code == 401:
        return False, "key 无效或已被吊销（401）"
    if r.status_code == 402:
        return False, "账户余额不足（402）"
    if r.status_code == 403:
        return False, "key 无权限（403），可能未开通该模型"
    # 不回显原始响应，网关可能把请求中的凭据包含在错误信息里。
    return False, f"服务返回 {r.status_code}；请检查接口地址、模型名称、权限和额度"


def set_user_env(name: str, value: str) -> None:
    """写用户级环境变量（setx 只影响新开的窗口）。"""
    if os.name != "nt":
        raise SystemExit("--env 只支持 Windows；其它系统请写 shell 配置。")
    subprocess.run(["setx", name, value], check=True, capture_output=True, text=True)


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def ask_provider(llm: dict) -> None:
    print("\n选择模型服务商（回车保持当前设置）：")
    for num, (name, base, model) in PROVIDERS.items():
        print(f"  {num}) {name}")
    print(f"  {len(PROVIDERS) + 1}) 自定义（自己填 base_url 和模型名）")
    print(f"\n当前：{llm.get('base_url')}  模型 {llm.get('model')}")
    choice = input("编号> ").strip()
    if not choice:
        return
    if choice in PROVIDERS:
        _, base, model = PROVIDERS[choice]
        llm["base_url"], llm["model"] = base, model
        if choice == "6":
            llm["reasoning_effort"] = "none"
        print(f"已切换到 {base} / {model}")
    elif choice == str(len(PROVIDERS) + 1):
        base = input(f"base_url [{llm.get('base_url')}]> ").strip()
        model = input(f"模型名 [{llm.get('model')}]> ").strip()
        if base:
            llm["base_url"] = normalize_base_url(base)
        if model:
            llm["model"] = model


def main() -> int:
    ap = argparse.ArgumentParser(description="配置雨课堂助手的模型 API Key（写入本地 config.json）")
    ap.add_argument("--key", help="直接给 key（不填则交互输入，输入时不回显）")
    ap.add_argument("--base-url", help="覆盖 llm.base_url，例如 https://api.deepseek.com/v1")
    ap.add_argument("--model", help="覆盖 llm.model，例如 deepseek-chat")
    ap.add_argument("--api-key-env", help="密钥环境变量名，例如 MAAS_API_KEY")
    ap.add_argument("--reasoning-effort", help="提供方支持的推理强度，例如 none / low / high")
    ap.add_argument("--env", action="store_true", help="写入用户环境变量，而不是 config.json")
    ap.add_argument("--session", action="store_true", help="只打印当前窗口临时生效的命令，不改文件")
    ap.add_argument("--check", action="store_true", help="只校验当前已配置的 key")
    ap.add_argument("--show", action="store_true", help="显示当前 key（打码）与来源")
    ap.add_argument("--unset", action="store_true", help="从 config.json 删除 llm.api_key")
    ap.add_argument("--config", help="指定配置文件路径（默认项目下的 config.json）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = ap.parse_args()

    if args.config:
        global CONFIG_FILE
        CONFIG_FILE = Path(args.config).expanduser().resolve()

    cfg = load_raw_config()
    llm = get_llm(cfg)
    if args.base_url:
        try:
            llm["base_url"] = normalize_base_url(args.base_url)
        except ValueError as exc:
            ap.error(str(exc))
    if args.model:
        llm["model"] = args.model
    if args.api_key_env:
        import re
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
            ap.error("环境变量名只能包含英文字母、数字和下划线，且不能以数字开头")
        llm["api_key_env"] = args.api_key_env
    if args.reasoning_effort:
        llm["reasoning_effort"] = args.reasoning_effort
    env_name = llm.get("api_key_env") or "DEEPSEEK_API_KEY"

    if args.show:
        key, source = resolve(cfg, args.key)
        print(f"配置文件：{CONFIG_FILE}")
        print(f"服务商  ：{llm.get('base_url')}   模型：{llm.get('model')}")
        print(f"Key     ：{mask_key(key or '')}（{len((key or '').strip())} 位）")
        print(f"来源    ：{source}")
        print(f"优先级  ：--api-key 参数 > config.json llm.api_key > 环境变量 {env_name} > DSH 凭据")
        return 0

    if args.unset:
        if "api_key" not in llm:
            print("config.json 里本来就没有 llm.api_key，无需删除。")
            return 0
        llm.pop("api_key", None)
        save_raw_config(cfg)
        print(f"已从 {CONFIG_FILE} 删除 llm.api_key。")
        return 0

    if args.check:
        key, source = resolve(cfg, args.key)
        if not key:
            print("还没有配置 key。运行 python tools/set_api_key.py 或双击 设置APIKey.bat。")
            return 1
        print(f"正在用 {llm.get('base_url')} 校验（来源：{source}）……")
        ok, msg = check_key(cfg, key)
        print(("通过：" if ok else "失败：") + msg)
        return 0 if ok else 2

    key = (args.key or "").strip()
    if not key and not sys.stdin.isatty():
        print("非交互环境请用 --key 传 key，例如：python tools/set_api_key.py --key sk-xxxx")
        return 1
    if not key:
        print("=" * 62)
        print(" 配置模型 API Key")
        print("=" * 62)
        print("Key 只写进本机的 config.json（已在 .gitignore 里），不会被提交或上传。")
        print("按 Ctrl+C 可随时退出，不会改动任何文件。")
        try:
            if not args.yes:
                ask_provider(llm)
            local = any(t in llm["base_url"] for t in ("127.0.0.1", "localhost", "ollama"))
            if local:
                print("\n本地服务（Ollama 等）通常不校验 key，自动填一个占位值 ollama。")
                key = "ollama"
            while not key:
                typed = getpass.getpass(f"\n粘贴 {llm.get('model')} 的 API Key（输入不回显，回车确认）> ")
                key = typed.strip()
                if not key:
                    print("不能为空，请重新输入。")
        except (EOFError, KeyboardInterrupt, ValueError) as exc:
            print("\n已取消，没有改动任何文件。")
            if isinstance(exc, ValueError):
                print(str(exc))
            return 1

    if args.session:
        shell = "PowerShell" if os.name == "nt" else "shell"
        print(f"\n当前 {shell} 窗口临时生效（关掉窗口就失效）：")
        print(f'  $env:{env_name} = "{key}"' if os.name == "nt" else f'  export {env_name}="{key}"')
        return 0

    if args.env:
        set_user_env(env_name, key)
        llm.pop("api_key", None)
        save_raw_config(cfg)
        print(f"已写入用户环境变量 {env_name}（打码：{mask_key(key)}，{len(key)} 位）。")
        print("注意：只对新开的窗口生效；已经打开的终端和启动器要关掉重开。")
    else:
        llm["api_key"] = key
        save_raw_config(cfg)
        print(f"已写入 {CONFIG_FILE}（打码：{mask_key(key)}，{len(key)} 位）。")

    print(f"服务商 {llm.get('base_url')}   模型 {llm.get('model')}")
    print("\n正在校验（一次小型模型请求，可能产生少量费用）……")
    ok, msg = check_key(cfg, key)
    print(("校验通过：" if ok else "校验失败：") + msg)
    if not ok:
        print("可以先跳过校验继续用，但抓题时模型调用会失败，日志里会看到原因。")
        return 2
    print("\n下一步：双击 启动雨课堂助手.bat，或在项目目录运行")
    print("  python ykt_watcher.py --login      # 第一次：扫码登录")
    print("  python ykt_watcher.py --window 08:00-09:40 --auto-fill")
    return 0


if __name__ == "__main__":
    sys.exit(main())
