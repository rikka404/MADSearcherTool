using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace MADSearcher.Desktop;

/// <summary>Cutout inputs and result handoff. Segmentation and export remain worker operations.</summary>
public partial class MainWindow
{
    private string _cutPreviewFile = "";
    private string _cutResultDirectory = "";
    private string _aeScript = "";
    private string _cutFrameSource = "";
    private long _cutFrameIndex = -1;
    private long _cutSelectionVersion;
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
            CutStartBox.Text = range.StartTimecode;
            CutEndBox.Text = range.EndTimecode;
            // Normalizing our own inputs can raise TextChanged; retain that new version.
            selection = selection with { StartText = range.StartTimecode, EndText = range.EndTimecode, Version = _cutSelectionVersion };
        }
        return new(selection, range);
    }

    private void CutSelectionChanged(object sender, TextChangedEventArgs e)
    {
        _cutSelectionVersion++;
        _cutFrameSource = "";
        _cutFrameIndex = -1;
        if (CutFirstFrame == null || CutFrameLabel == null)
            return;
        CutFirstFrame.Source = null;
        CutFrameLabel.Text = "片段已改变，请读取新的首帧。";
        _state.CutInfo = "片段已改变；已有任务继续使用启动时的片段。";
    }

    private string PromptMode => (PromptModeBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "mask";

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
    private async void ReadCutFrame(object sender, RoutedEventArgs e) => await Execute("正在读取片段首帧…", LoadCutFrame);
    private async Task LoadCutFrame(CancellationToken token)
    {
        var selection = await ResolveCutSelection(ReadCutSelection(), token);
        await LoadResolvedCutFrame(selection, token);
    }

    private async Task LoadResolvedCutFrame(ResolvedCutSelection selection, CancellationToken token)
    {
        if (selection.Input.Version != _cutSelectionVersion)
            return;
        var range = selection.Range;
        var video = range.Video;
        var data = await Call("video.frame", new
        {
            path = selection.Input.Path,
            frame_index = range.StartFrame
        }, token);
        // The user can edit the next selection while this frame is being decoded.
        if (selection.Input.Version != _cutSelectionVersion)
            return;
        CutPlayer.Stop();
        CutPlayer.Visibility = Visibility.Collapsed;
        CutFirstFrame.Source = Images.Load(Text(data, "path"));
        _cutFrameSource = selection.Input.Path;
        _cutFrameIndex = range.StartFrame;
        _state.CutInfo = $"{video.Width} × {video.Height} · {video.Fps:0.###} fps · 时间码按 {range.NominalFps} 帧编号（0–{range.NominalFps - 1}）\n"
            + $"已选 {range.FrameCount} 帧 · 全片{(range.TotalFramesEstimated ? "估计" : "共")} {range.TotalFrames} 帧";
        CutFrameLabel.Text = $"当前首帧：{range.StartTimecode} · 第 {range.StartFrame} 帧（从0起） · 原图 {video.Width} × {video.Height} 像素";
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
        if (selection.Path != _cutFrameSource || range.StartFrame != _cutFrameIndex)
            await LoadResolvedCutFrame(resolved, token);
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
            export_ae = exportAe
        }, token);
        _cutResultDirectory = Text(data, "output_dir");
        _cutPreviewFile = Text(data, "preview_path");
        _aeScript = Text(data, "ae_script");
        CutResultLabel.Text = $"{Path.GetFileName(selection.Path)} · {range.StartTimecode}—{range.EndTimecode}（终点不含）\n已输出 {data.GetProperty("frame_count")} 帧\n{_cutResultDirectory}\n{Warnings(data)}";
        _state.Status = "抠像完成。请预览轮廓，特别检查遮挡、发丝与切镜位置。";
    });
    private void ShowCutStill(object sender, RoutedEventArgs e)
    {
        CutPlayer.Stop();
        CutPlayer.Visibility = Visibility.Collapsed;
    }
    private void PlayCutResult(object sender, RoutedEventArgs e)
    {
        if (!File.Exists(_cutPreviewFile))
        {
            _state.Status = "还没有完成的抠像预览。请先运行抠像。";
            return;
        }
        CutPlayer.Visibility = Visibility.Visible;
        CutPlayer.Source = new Uri(_cutPreviewFile);
        CutPlayer.Play();
    }
    private void PauseCutResult(object sender, RoutedEventArgs e) => CutPlayer.Pause();
    private void OpenCutResult(object sender, RoutedEventArgs e) => OpenPath(_cutResultDirectory);
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
            _state.Status = "已发送 JSX 到 After Effects。请在 AE 中检查新建合成与逐帧蒙版。";
        }
        catch (Exception ex)
        {
            _state.Status = "无法发送到 AE：" + SafeError(ex.Message) + " 可在 AE 的 文件 → 脚本 → 运行脚本文件 中打开输出的 JSX。";
        }
    }
}
