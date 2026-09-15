using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace MADSearcher.Desktop;

/// <summary>Cutout inputs and result handoff. Segmentation and export remain worker operations.</summary>
public partial class MainWindow
{
    private string _cutResultDirectory = "";
    private string _aeScript = "";
    private string _aeExportMode = "";
    private long _cutSelectionVersion;
    private bool _syncingCutRange;
    private readonly DispatcherTimer _cutRangeDelay = new() { Interval = TimeSpan.FromMilliseconds(300) };
    private CancellationTokenSource? _cutRangeCancellation;
    private string _cutSourceIdentity = "";
    private sealed record CutSelection(string Path, string StartText, string EndText, long Version);
    private sealed record ResolvedCutSelection(CutSelection Input, CutRangeInfo Range);

    private CutSelection ReadCutSelection()
    {
        var path = RequireFile(CutPathBox.Text, "抠像视频");
        return new(path, CutStartBox.Text.Trim(), CutEndBox.Text.Trim(), _cutSelectionVersion);
    }

    private async Task<ResolvedCutSelection> ResolveCutSelection(CutSelection selection, CancellationToken token)
    {
        var data = await Call("cutout.range", new
        {
            path = selection.Path,
            start = selection.StartText,
            end = selection.EndText
        }, token);
        var range = JsonSerializer.Deserialize<CutRangeInfo>(data.GetRawText())
            ?? throw new InvalidOperationException("未能读取抠像帧范围。");
        if (selection.Version == _cutSelectionVersion)
        {
            _syncingCutRange = true;
            try { CutStartBox.Text = range.StartTimecode; CutEndBox.Text = range.EndTimecode; }
            finally { _syncingCutRange = false; }
            // Normalizing our own inputs can raise TextChanged; retain that new version.
            selection = selection with { StartText = range.StartTimecode, EndText = range.EndTimecode, Version = _cutSelectionVersion };
            if (SourcePreview.Media?.Path == selection.Path) SourcePreview.SetSelection(range.StartFrame, range.EndFrame);
        }
        return new(selection, range);
    }

    private void CutSelectionChanged(object sender, TextChangedEventArgs e)
    {
        _cutSelectionVersion++;
        if (SourcePreview == null || CutFrameLabel == null)
            return;
        _cutRangeCancellation?.Cancel();
        if (ReferenceEquals(sender, CutPathBox))
        {
            SourcePreview.Clear();
            _cutSourceIdentity = "";
        }
        CutFrameLabel.Text = "起止范围已改变；使用首帧蒙版时，请确认蒙版仍对应新的起点。";
        _state.CutInfo = "片段已改变；已有任务继续使用启动时的片段。";
        if (!_syncingCutRange && SourcePreview.Media != null)
        {
            SourcePreview.SetSelectionError("正在更新抠像范围…");
            _cutRangeDelay.Stop(); _cutRangeDelay.Start();
        }
    }

    private string PromptMode => (PromptModeBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "mask";
    private string AeMode => AeModeBox.SelectedValue?.ToString() ?? "matte";
    private static string AeModeName(string mode) => mode switch
    {
        "rgba" => "透明PNG序列",
        "matte" => "原画＋独立Alpha遮罩（可局部修补）",
        "paths" => "可编辑矢量路径（轮廓近似）",
        _ => "未生成"
    };

    private async void ChooseCutVideo(object sender, RoutedEventArgs e)
    {
        if (_state.Busy)
            return;
        if (!ChooseFile(CutPathBox, "视频文件|*.mp4;*.mkv;*.mov;*.avi;*.webm|所有文件|*.*", "选择抠像视频"))
            return;
        await Execute("正在读取视频…", async token =>
        {
            var version = _cutSelectionVersion;
            var path = CutPathBox.Text;
            var data = await Call("video.probe", new
            {
                path
            }, token);
            if (version != _cutSelectionVersion)
                return;
            CutStartBox.Text = "0";
            CutEndBox.Text = Number(Math.Min(5, data.GetProperty("duration").GetDouble()));
            await LoadCutFrame(token);
        });
    }
    private async void ReadCutFrame(object sender, RoutedEventArgs e) => await Execute("正在载入预览信息…", LoadCutFrame);
    private async Task LoadCutFrame(CancellationToken token)
    {
        var selection = await ResolveCutSelection(ReadCutSelection(), token);
        await LoadResolvedCutFrame(selection, token);
    }

    private Task LoadResolvedCutFrame(ResolvedCutSelection selection, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        if (selection.Input.Version != _cutSelectionVersion)
            return Task.CompletedTask;
        var range = selection.Range;
        var video = range.Video;
        var file = new FileInfo(selection.Input.Path);
        var identity = $"{file.FullName}|{file.Length}|{file.LastWriteTimeUtc.Ticks}|{OperationSettings.FfmpegPath}";
        SourceExpander.IsExpanded = true;
        if (_cutSourceIdentity != identity || SourcePreview.Media == null)
        {
            SourcePreview.Configure(new PreviewMedia(file.FullName, video.Width, video.Height, video.Fps,
                range.NominalFps, range.TotalFrames, EstimatedCount: range.TotalFramesEstimated), OperationSettings.FfmpegPath,
                range.StartFrame, canMark: true);
            _cutSourceIdentity = identity;
        }
        else SourcePreview.Seek(range.StartFrame);
        SourcePreview.SetSelection(range.StartFrame, range.EndFrame);
        _state.CutInfo = $"{video.Width} × {video.Height} · {video.Fps:0.###} fps · 时间码按 {range.NominalFps} 帧编号（0–{range.NominalFps - 1}）\n"
            + $"已选 {range.FrameCount} 帧 · 全片{(range.TotalFramesEstimated ? "估计" : "共")} {range.TotalFrames} 帧";
        CutFrameLabel.Text = $"抠像首帧：{range.StartTimecode} · 源第{range.StartFrame}帧 · 原图{video.Width}×{video.Height}；预览缩放不改变蒙版坐标。";
        return Task.CompletedTask;
    }
    private void PromptModeChanged(object sender, SelectionChangedEventArgs e)
    {
        if (MaskInputs == null)
            return;
        MaskInputs.Visibility = PromptMode == "mask" ? Visibility.Visible : Visibility.Collapsed;
        BoxInputs.Visibility = PromptMode == "box" ? Visibility.Visible : Visibility.Collapsed;
        TextInputs.Visibility = PromptMode == "text" ? Visibility.Visible : Visibility.Collapsed;
        ReferenceInputs.Visibility = PromptMode == "reference" ? Visibility.Visible : Visibility.Collapsed;
    }
    private void ChooseMask(object sender, RoutedEventArgs e) => ChooseFile(MaskPathBox, "蒙版图片|*.png;*.bmp;*.tif;*.tiff", "选择对应首帧的蒙版 / 透明 PNG");
    private void ChooseReference(object sender, RoutedEventArgs e) => ChooseFile(ReferencePathBox, "图片|*.png;*.jpg;*.jpeg;*.webp", "选择人物参考图");
    private void ChooseCutOutput(object sender, RoutedEventArgs e)
    {
        var value = ChooseDirectory("选择抠像输出目录", CutOutputBox.Text);
        if (value != null)
            CutOutputBox.Text = value;
    }
    private async void RunCutout(object sender, RoutedEventArgs e) => await Execute("正在准备 SAM 2 抠像…", async token =>
    {
        var selection = ReadCutSelection();
        if (string.IsNullOrWhiteSpace(OperationSettings.SamCheckpoint) || !File.Exists(OperationSettings.SamCheckpoint))
            throw new InvalidOperationException("尚未配置 SAM 2 权重。请运行 scripts/setup.ps1 -WithSam，再到设置中选择 models 下的 .pt 文件。");
        var output = CutOutputBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(output))
            throw new InvalidOperationException("请选择抠像输出目录。");
        var mode = PromptMode;
        var prompt = PromptTextBox.Text.Trim();
        var exportVideo = ExportMovCheck.IsChecked == true;
        var exportAe = ExportAeCheck.IsChecked == true;
        var aeMode = AeMode;
        var mask = "";
        var reference = "";
        double[]? box = null;
        if (mode == "mask")
            mask = RequireFile(MaskPathBox.Text, "首帧蒙版");
        if (mode == "reference")
            reference = RequireFile(ReferencePathBox.Text, "人物参考图");
        if (mode is "text" or "reference" && string.IsNullOrWhiteSpace(OperationSettings.ApiKey))
            throw new InvalidOperationException("文字或参考图定位需要 OpenAI API Key，请先在设置中填写；蒙版或像素框可完全本地运行。");
        if (mode == "text" && string.IsNullOrWhiteSpace(prompt))
            throw new InvalidOperationException("请描述首帧中要保留的人物外观和位置。");
        if (mode == "box")
        {
            var parts = BoxCoordinates.Text.Split([',', '，', ' ', ';'], StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length != 4)
                throw new InvalidOperationException("像素框需要四个数字：左, 上, 右, 下。");
            box = parts.Select(p => Parse(p, "框坐标")).ToArray();
        }
        var resolved = await ResolveCutSelection(selection, token);
        var range = resolved.Range;
        var video = range.Video;
        if (box != null && (box[0] < 0 || box[1] < 0 || box[2] <= box[0] || box[3] <= box[1] || box[2] > video.Width || box[3] > video.Height))
            throw new InvalidOperationException("像素框必须位于原图范围内，右坐标大于左坐标，下坐标大于上坐标。");
        // Browsing never changes this captured range, and preview readiness is not a processing prerequisite.
        var data = await Call("cutout.run", new
        {
            path = selection.Path,
            start_frame = range.StartFrame,
            end_frame = range.EndFrame,
            prompt_mode = mode,
            mask_path = mask,
            prompt,
            reference_path = reference,
            box,
            output_dir = output,
            export_video = exportVideo,
            export_ae = exportAe,
            ae_mode = aeMode
        }, token);
        _cutResultDirectory = Text(data, "output_dir");
        SetAeExport(data);
        ResultPreview.Configure(new PreviewMedia(Path.Combine(Text(data, "rgba_dir"), "%06d.png"),
            video.Width, video.Height, video.Fps, range.NominalFps, data.GetProperty("frame_count").GetInt64(),
            range.StartFrame, ImageSequence: true), OperationSettings.FfmpegPath);
        ResultExpander.IsExpanded = true;
        CutResultLabel.Text = $"{Path.GetFileName(selection.Path)} · {range.StartTimecode}—{range.EndTimecode}（终点不含）\n已输出 {data.GetProperty("frame_count")} 帧\n{_cutResultDirectory}\n{Warnings(data)}";
        _state.Status = "抠像完成。请预览轮廓，特别检查遮挡、发丝与切镜位置。";
    });
    private void InitializeCutPreview()
    {
        SourcePreview.Activated += _ => ResultPreview.Suspend();
        ResultPreview.Activated += _ => SourcePreview.Suspend();
        SourcePreview.StartRequested += frame => SetCutBoundary(frame, true);
        SourcePreview.EndRequested += frame => SetCutBoundary(frame, false);
        _cutRangeDelay.Tick += async (_, _) => { _cutRangeDelay.Stop(); await RefreshCutRange(); };
    }

    private void SetCutBoundary(long frame, bool start)
    {
        var media = SourcePreview.Media;
        if (media == null) return;
        if (start) CutStartBox.Text = media.Timecode(frame);
        else CutEndBox.Text = media.Timecode(frame);
        CutFrameLabel.Text = start ? "起点已更新；首帧蒙版必须对应新起点。" : "结束点已更新，包含刚才显示的画面。";
        // End marker may equal start while the user is adjusting both; never silently move the other marker.
        _cutRangeDelay.Stop(); _cutRangeDelay.Start();
    }

    private async Task RefreshCutRange()
    {
        if (SourcePreview.Media == null || _storageMaintenance) return;
        _cutRangeCancellation?.Cancel();
        using var cancellation = new CancellationTokenSource();
        _cutRangeCancellation = cancellation;
        var version = _cutSelectionVersion;
        try
        {
            var selection = ReadCutSelection();
            var data = await _worker.RunAsync("cutout.range", new { path = selection.Path, start = selection.StartText, end = selection.EndText },
                _state.Settings.Snapshot(), new Progress<(double, string)>(_ => { }), cancellation.Token);
            if (version != _cutSelectionVersion || cancellation.IsCancellationRequested) return;
            var range = JsonSerializer.Deserialize<CutRangeInfo>(data.GetRawText()) ?? throw new InvalidOperationException("无法解析帧范围。");
            SourcePreview.SetSelection(range.StartFrame, range.EndFrame);
            _state.CutInfo = $"已选 {range.FrameCount} 帧 · {range.StartTimecode} → {range.EndTimecode}（终点不含）";
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            if (version == _cutSelectionVersion) SourcePreview.SetSelectionError(SafeError(ex.Message));
        }
        finally { if (ReferenceEquals(_cutRangeCancellation, cancellation)) _cutRangeCancellation = null; }
    }

    private void CloseCutPreview()
    {
        _cutRangeDelay.Stop(); _cutRangeCancellation?.Cancel();
        SourcePreview.Dispose(); ResultPreview.Dispose();
    }
    private void OpenCutResult(object sender, RoutedEventArgs e) => OpenPath(_cutResultDirectory);
    private void SetAeExport(JsonElement data)
    {
        _aeScript = Text(data, "ae_script");
        _aeExportMode = Text(data, "ae_actual_mode");
        AeExportLabel.Text = string.IsNullOrWhiteSpace(_aeScript)
            ? "本次没有可发送的AE脚本；可选择已有结果补导出。"
            : $"已准备：{AeModeName(_aeExportMode)}\n来源：{Path.GetFileName(_cutResultDirectory)}\n{_aeScript}";
        if (!string.IsNullOrWhiteSpace(_aeScript) && Text(data, "ae_mode") != _aeExportMode)
            AeExportLabel.Text += "\n矢量路径未完成，发送按钮将使用保留像素的Alpha遮罩版本。";
        if (data.TryGetProperty("ae_report_path", out var report) && !string.IsNullOrWhiteSpace(report.GetString()))
            AeExportLabel.Text += $"\n导出报告：{report.GetString()}";
    }
    private async void ReexportAe(object sender, RoutedEventArgs e)
    {
        if (_state.Busy) return;
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = "选择已有抠像任务的manifest.json",
            Filter = "抠像任务记录|manifest.json|JSON文件|*.json",
            FileName = "manifest.json"
        };
        if (Directory.Exists(_cutResultDirectory)) dialog.InitialDirectory = _cutResultDirectory;
        else if (Directory.Exists(CutOutputBox.Text)) dialog.InitialDirectory = CutOutputBox.Text;
        if (dialog.ShowDialog(this) != true) return;
        var manifest = dialog.FileName;
        var mode = AeMode;
        await Execute("正在从已有抠像结果补导出AE…", async token =>
        {
            var data = await Call("cutout.export_ae", new { manifest_path = manifest, ae_mode = mode }, token);
            _cutResultDirectory = Text(data, "output_dir");
            SetAeExport(data);
            var fps = data.GetProperty("fps").GetDouble();
            ResultPreview.Configure(new PreviewMedia(Path.Combine(Text(data, "rgba_dir"), "%06d.png"),
                data.GetProperty("width").GetInt32(), data.GetProperty("height").GetInt32(), fps, (int)Math.Ceiling(fps),
                data.GetProperty("frame_count").GetInt64(), data.GetProperty("start_frame").GetInt64(), ImageSequence: true),
                OperationSettings.FfmpegPath);
            ResultExpander.IsExpanded = true;
            CutResultLabel.Text = $"已有抠像任务：{Path.GetFileName(_cutResultDirectory)}\n{data.GetProperty("frame_count").GetInt32()}帧 · AE补导出完成\n{_cutResultDirectory}\n{Warnings(data)}";
            _state.Status = $"AE脚本已准备：{AeModeName(_aeExportMode)}。原始抠像及旧脚本均保留。{Warnings(data)}";
        });
    }
    private void SendToAe(object sender, RoutedEventArgs e)
    {
        if (_state.Busy)
            return;
        try
        {
            var script = RequireFile(_aeScript, "AE 脚本");
            var executable = RequireFile(_state.Settings.AfterFxPath, "AfterFX.exe");
            var start = new ProcessStartInfo(executable) { UseShellExecute = false };
            start.ArgumentList.Add("-r");
            start.ArgumentList.Add(script);
            Process.Start(start);
            _state.Status = $"已派发AE脚本：{AeModeName(_aeExportMode)}。请在AE检查导入结果；派发成功不代表执行完成。";
        }
        catch (Exception ex)
        {
            _state.Status = "无法发送到 AE：" + SafeError(ex.Message) + " 可在 AE 的 文件 → 脚本 → 运行脚本文件 中打开输出的 JSX。";
        }
    }
}
