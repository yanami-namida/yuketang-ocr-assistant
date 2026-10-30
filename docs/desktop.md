# Windows 桌面版

无需安装 Python，无需输入命令。需要 Windows 10/11 x64 和 Microsoft Edge；图片题
使用系统自带 OCR。双击 EXE 即可，首次启动解包运行环境可能需要数秒。

## 开始使用

1. 下载 Release 中带 `windows-x64.zip` 的压缩包并解压。
2. 双击 `YuketangAssistant.exe`。
3. 在“模型配置”中选择提供方，填写自己的密钥，点击“保存配置”。
   可以选择“自定义”，手动填写 API 地址和模型名称；完整的 `/chat/completions` 地址也可以。
4. 如需确认模型权限，点击“测试连接”。这会发起一次小型请求，可能产生少量费用。
5. 点击“扫码登录”，在打开的 Edge 窗口扫码；登录完成后浏览器自动关闭。
6. 选择时长，点击“开始监控”。状态和运行日志显示在主窗口；点击“停止任务”结束。

默认只显示参考答案。自动填选和自动提交是单独的开关；启用自动提交会向平台真实提交答案。
HTTP 模式同样需要先扫码登录；课堂 ID 可留空，只有一个活动课堂时自动选择。
定时时间窗用于浏览器模式，例如 `08:00-09:40`；HTTP 模式使用监控时长。

## 华为云 MaaS

选择“华为云 ModelArts MaaS”会自动填入：

- API 基础地址：`https://api.modelarts-maas.com/v2`
- 模型名称：`deepseek-v4.1-flash-1017`
- 推理强度：`none`

地址末尾不要带 `svg`。切换提供方后请重新输入新提供方的密钥，避免误用旧密钥。

## 本地演示

点击“离线演示”会打开合成的本地题目页面，不登录、不调用模型，约一分钟后自动结束。
演示使用单独的数据目录，不影响真实登录态。

## 配置与升级

EXE 版的数据目录为 `%LOCALAPPDATA%\YuketangOCRAssistant`，界面中的“打开数据目录”
按钮可直接打开。密钥保存在其中的 `config.json`，登录态、截图和日志也在此目录。
这些文件属于本机私有数据，不要分享整个数据目录。

更新时关闭旧程序，下载并运行新 EXE 即可，用户目录中的配置和登录态会保留。
源码版仍使用源码所在目录；旧源码版用户可打开桌面版的数据目录，将旧的 `config.json`、
`edge_profile` 和 `ykt_state.json` 复制过去，也可在界面中重新配置和扫码。

外部模型会收到识别后的题目文字，请遵守课程规则。第三方组件许可证随 Windows ZIP
的 `licenses/` 一并提供。本项目使用 MIT 许可证。

## 从源码构建（仅开发者）

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv-build\Scripts\python.exe tools\build_windows.py
```

构建结果位于 `dist/windows/`，打包采用白名单资源，不包含本机配置和浏览器资料。
