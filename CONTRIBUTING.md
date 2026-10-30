# 参与贡献

欢迎提交问题和 Pull Request。描述问题时请附上 Windows、Python、Edge 版本，
复现步骤和预期结果；尽量使用 `mock_quiz.html` 或自制题目复现。

不要上传 API Key、Cookie、登录态、真实课堂截图或含个人信息的日志。
模型配置请使用占位符，调试文件先脱敏。

## 开发与验证

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-http.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

完整测试需要 Windows 和已安装的 Microsoft Edge；DOM 测试使用临时浏览器上下文，
不会使用个人的 `edge_profile/`。模型与 HTTP 请求在测试中均用模拟响应替代。

修改 OCR 时请补充能体现分行、选项顺序或噪声问题的合成词块；修改模型接入时请
覆盖请求地址、模型参数与错误响应。避免在测试里依赖外部账号或付费 API。

源码和 Markdown 使用 UTF-8、LF；`.bat` 和 `.ps1` 按 `.gitattributes` 使用 CRLF。
提交贡献即表示你有权以本项目的 [MIT 许可证](LICENSE) 提供这些内容。
