# MADSearcher

面向 MAD 制作的 Windows / WPF 辅助工具：按动画分组查找镜头，将视频中的目标人物分离为透明素材，再交给 After Effects 继续制作。

## 启动

当前机器已准备项目内 Python 环境和 SAM 2.1 tiny 权重。可直接打开 `artifacts/app/MADSearcher.exe`，请保留整个项目的目录结构。也可以双击根目录的 `MADSearcher.cmd`，它会编译并启动桌面端。

命令行入口：

```powershell
.\MADSearcher-CLI.cmd --help
.\MADSearcher-CLI.cmd group.list
```

首次在其他机器配置：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -WithSam -Cuda -WithWhisper
```

需要 Windows、.NET 9 SDK、Python 3.10+、FFmpeg / FFprobe；非 NVIDIA 设备去掉 `-Cuda`。脚本不自动安装 .NET、Python 或 FFmpeg。完整步骤、CPU配置和网络问题见使用说明。

## 文档

- [使用说明](Docs/使用说明.md)：桌面工作流、命令行操作、安装配置、输入要求及错误处理。
- [扩展开发说明](Docs/扩展开发说明.md)：目录、服务边界、协议与后续功能扩展。
- [技术方案](Docs/技术方案.md)、[设计文档](Docs/设计文档.md)、[任务 DAG](Docs/tasks.json)。
- [抠像与输出边界](Docs/抠像边界.md)、[交付及历史验收记录](Docs/验收记录.md)。

API key 默认留空。字幕关键词检索、蒙版/框提示抠像可在本地完成；视觉理解和语义索引需显式启用 OpenAI。PSD 序列暂不支持。

本次按用户要求完成实现收尾和文档交付，**新增及回归测试暂缓**，等待后续测试文档。历史测试结果不代表最新版本已完成全面验收。开发与交付使用 `dev` 分支；运行环境、模型和发布文件不进入版本库，请按上述步骤在本机准备。
