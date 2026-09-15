# MADSearcher

面向 MAD 制作的 Windows / WPF 辅助工具：按动画分组查找镜头，将视频中的目标人物分离为透明素材，再交给 After Effects 继续制作。

## 启动

当前机器已准备项目内 Python 环境和 SAM 2.1 tiny 权重。包含镜头/台词检索、角色功能、抠像双预览、AE三种导出及缓存清理的新版可直接打开 `artifacts/app/MADSearcher.exe`，请保留整个项目的目录结构。也可以双击根目录的 `MADSearcher.cmd`，它会编译并启动桌面端。

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
- [角色索引技术路线与提示词](Docs/角色索引技术路线与提示词.md)：角色资料、512像素图片边界、镜头关联及耗时日志。
- [镜头分段与动态采样](Docs/镜头分段与动态采样.md)：分段参数、采样数量、分层缓存、费用和镜头范围扩展。
- [姓名查询与排序](Docs/姓名查询与排序.md)：句内姓名/别名、多路情节查询、人物加分与本地证据、查询缓存及验收边界。
- [台词检索与补建](Docs/台词检索与补建.md)：独立字幕向量、综合/台词/画面情节模式、旧素材只补台词、原文与同义查询。
- [AE导出与修补](Docs/AE导出与修补.md)：三种交接方式、原画与Alpha局部修补、已有抠像结果补导出和诊断。
- [缓存清理与索引资源](Docs/缓存清理与索引资源.md)：工具菜单清理范围、打开索引目录、旧缩略图保护迁移和正式结果保留。
- [扩展开发说明](Docs/扩展开发说明.md)：目录、服务边界、协议与后续功能扩展。
- [技术方案](Docs/技术方案.md)、[设计文档](Docs/设计文档.md)、[任务 DAG](Docs/tasks.json)。
- [抠像与输出边界](Docs/抠像边界.md)、[交付及历史验收记录](Docs/验收记录.md)。

API key 默认留空。字幕关键词检索、蒙版/框提示抠像可在本地完成；视觉理解和语义索引需显式启用 OpenAI。PSD 序列暂不支持。

本次按用户要求完成实现收尾和文档交付，**新增及回归测试暂缓**，等待后续测试文档。历史测试结果不代表最新版本已完成全面验收。开发与交付使用 `dev` 分支；运行环境、模型和发布文件不进入版本库，请按上述步骤在本机准备。

`v0.1.0`固定为35edf258645c9f6cd091a261e60c59639b929cae；角色列表、参考图关联和索引耗时日志属于其后的dev开发内容。
