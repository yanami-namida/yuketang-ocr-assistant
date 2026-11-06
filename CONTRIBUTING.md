# 参与贡献

欢迎提交问题和 Pull Request。描述问题时请附上 Windows、Python、Edge 版本，
复现步骤和预期结果；尽量使用 `mock_quiz.html` 或自制题目复现。

不要上传 API Key、Cookie、登录态、真实课堂截图或含个人信息的日志。
模型配置请使用占位符，调试文件先脱敏。

## 不接受的功能改动

本项目只做「只读识别 + 参考答案」。以下改动一律不会被合并：

- 点击网页选项、提交答案、自动签到或任何替用户操作页面的代码；
- 直接调用平台接口收题/交卷的后台模式（包括 WebSocket 监听、Cookie 复用上传）；
- 绕过登录、权限、限流或反自动化的实现；
- 面向批量刷课、代答、替考的功能。

理由见 [使用条款](ACCEPTABLE_USE.md)。这类功能会让项目从学习辅助变成代答工具，
既违反平台协议，也可能违反学校规定。

## 开发与验证

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

完整测试需要 Windows 和已安装的 Microsoft Edge；DOM 测试使用临时浏览器上下文，
不会使用个人的 `edge_profile/`。模型请求在测试中均用模拟响应替代。

修改 OCR 时请补充能体现分行、选项顺序或噪声问题的合成词块；修改模型接入时请
覆盖请求地址、模型参数与错误响应。避免在测试里依赖外部账号或付费 API。

源码和 Markdown 使用 UTF-8、LF；`.bat` 和 `.ps1` 按 `.gitattributes` 使用 CRLF。
提交贡献即表示你有权以本项目的 [MIT 许可证](LICENSE) 提供这些内容。

## Windows 桌面版

源码入口为 `ykt_desktop.py`，打包命令和路径说明见 [桌面版文档](docs/desktop.md)。
请在独立虚拟环境安装 `requirements-build.txt`，不要向构建参数添加真实配置或整个项目目录。
验证 EXE 时可设置 `YKT_DATA_DIR` 为临时测试目录，避免访问开发者的个人登录态。
