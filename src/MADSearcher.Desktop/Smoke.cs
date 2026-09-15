using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace MADSearcher.Desktop;

public partial class MainWindow
{
    private string? _smokeImportPath;
    private string? _smokeExportDirectory;

    /// <summary>Runs the actual WPF handlers and Python boundary on an isolated synthetic fixture.</summary>
    private async Task SmokeAsync()
    {
        var output = Path.Combine(_root, "artifacts", "desktop-smoke"); Directory.CreateDirectory(output);
        var checks = new List<string>();
        try
        {
            await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            Capture(Path.Combine(output, "01-empty-search.png"));
            await SearchAsync();
            Expect(_state.Status.Contains("分组"), "空分组检索校验", checks);
            var source = Path.Combine(output, "测试电车.mp4");
            var subtitle = Path.Combine(output, "测试电车.srt");
            await File.WriteAllTextAsync(subtitle, "1\n00:00:00,200 --> 00:00:01,500\n少女在电车站等待朋友。\n\n2\n00:00:02,000 --> 00:00:03,600\n列车经过，蓝色天空与回忆。\n", new UTF8Encoding(false));
            var ffmpeg = new ProcessStartInfo(_state.Settings.FfmpegPath) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true, RedirectStandardOutput = true };
            foreach (var argument in new[] { "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x1d485e:s=640x360:r=12:d=4", "-vf", "drawbox=x=40:y=90:w=560:h=155:color=0x79baa7:t=fill,drawbox=x=60:y=110:w=120:h=65:color=0x203a51:t=fill,drawbox=x=210:y=110:w=120:h=65:color=0x203a51:t=fill,drawbox=x=390:y=110:w=110:h=65:color=0x203a51:t=fill", "-c:v", "libx264", "-pix_fmt", "yuv420p", source }) ffmpeg.ArgumentList.Add(argument);
            using (var process = Process.Start(ffmpeg)!)
            {
                var stderr = process.StandardError.ReadToEndAsync(); await process.WaitForExitAsync();
                if (process.ExitCode != 0) throw new Exception("Smoke fixture FFmpeg failed: " + await stderr);
            }
            await Execute("Smoke 创建分组", async token =>
            {
                var group = await Call("group.create", new { name = "星轨物语 · 界面验收", description = "蓝色列车、车站与少女的日常；界面验收合成素材。" }, token);
                await LoadGroups(token, Text(group, "id"));
            });
            Expect(_state.Groups.Count == 1, "分组创建与读取", checks);
            _smokeImportPath = source; ImportVideos(this, new RoutedEventArgs()); await _lastOperation;
            Expect(_state.Videos.Count == 1, "MP4 导入与素材选择 handler", checks);
            SubtitleBox.Text = subtitle; OffsetBox.Text = "0";
            IndexSelected(this, new RoutedEventArgs()); await _lastOperation;
            Expect(!_state.Status.Contains("操作未完成"), "字幕索引 handler", checks);
            QueryBox.Text = "电车"; SearchClicked(this, new RoutedEventArgs()); await _lastOperation;
            Expect(_state.Results.Count > 0 && _previewSource == source, "中文检索与结果选择 handler", checks);
            MakePreview(this, new RoutedEventArgs()); await _lastOperation;
            Expect(File.Exists(_previewFile), "真实 FFmpeg 预览 handler", checks);
            PreviewPlayer.Pause(); PreviewStill.Source = _state.Results[0].ThumbnailImage;
            await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            Capture(Path.Combine(output, "02-search-results.png"));
            _smokeExportDirectory = Path.Combine(output, "exports"); ExportClip(this, new RoutedEventArgs()); await _lastOperation;
            Expect(Directory.GetFiles(_smokeExportDirectory, "*.mp4").Length > 0, "MP4 片段导出 handler", checks);
            SendToCutout(this, new RoutedEventArgs()); await _lastOperation;
            // Preview decoding is asynchronous; this check covers media setup, not frame readiness.
            Expect(CutoutPage.Visibility == Visibility.Visible && SourcePreview.Media != null, "送入抠像、探测与预览媒体载入 handler", checks);
            PromptModeBox.SelectedIndex = 1;
            Expect(BoxInputs.Visibility == Visibility.Visible && MaskInputs.Visibility == Visibility.Collapsed, "提示模式切换", checks);
            CutEndBox.Text = "NaN"; RunCutout(this, new RoutedEventArgs()); await _lastOperation;
            Expect(_state.Status.Contains("有效数字"), "非有限输入拦截", checks);
            CutEndBox.Text = "3.6"; _state.Settings.SamCheckpoint = "";
            RunCutout(this, new RoutedEventArgs()); await _lastOperation;
            Expect(_state.Status.Contains("权重"), "缺失 SAM 权重的中文指引", checks);
            _state.Status = "界面验收：首帧与本地框提示已准备，缺少权重时阻止空任务。";
            await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            Capture(Path.Combine(output, "03-cutout.png"));
            CheckEnvironment(this, new RoutedEventArgs()); await _lastOperation;
            Expect(_state.Diagnostics.Contains("FFmpeg", StringComparison.OrdinalIgnoreCase), "真实环境诊断 handler", checks);
            await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            Capture(Path.Combine(output, "04-settings.png"));
            var operation = Execute("Smoke cancel", async token =>
            {
                await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
                Expect(_state.Busy && ToolPages.IsEnabled && QueryBox.IsEnabled && VideoList.IsEnabled && SettingsNav.IsEnabled,
                    "运行期间仍可浏览、编辑与导航", checks);
                Expect(!SearchButton.IsEnabled && !IndexButton.IsEnabled && !GroupBox.IsEnabled,
                    "运行期间禁止新增任务与分组读取", checks);
                var duplicateStarted = false;
                await Execute("不应启动", _ => { duplicateStarted = true; return Task.CompletedTask; });
                Expect(!duplicateStarted, "处理入口阻止重复任务", checks);
                _ = Dispatcher.BeginInvoke(() => CancelTask(this, new RoutedEventArgs()));
                await Task.Delay(5000, token);
            });
            await operation;
            Expect(!_state.Busy && _state.Status.Contains("已取消"), "取消 handler 与界面恢复", checks);
            await VerifyProcessTreeCancellation(output, checks);
            var keySettings = new AppSettings { ApiKey = "fixture-key" };
            var keyRoot = Path.Combine(output, "settings-fixture");
            keySettings.Save(keyRoot);
            Expect(AppSettings.Load(keyRoot).ApiKey == keySettings.ApiKey, "本地设置恢复 API Key", checks);
            var snapshot = keySettings.Snapshot();
            keySettings.ApiKey = "changed-fixture-key";
            Expect(snapshot.ApiKey == "fixture-key", "修改设置不改变任务快照", checks);
            Width = 1160; Height = 760; Navigate("search");
            await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            Capture(Path.Combine(output, "05-search-minimum-size.png"));
            Navigate("cutout");
            await Dispatcher.InvokeAsync(() => { }, DispatcherPriority.ApplicationIdle);
            Capture(Path.Combine(output, "06-cutout-minimum-size.png"));
            await File.WriteAllTextAsync(Path.Combine(output, "result.json"), JsonSerializer.Serialize(new { success = true, checks, root = _root, workspace = _state.Settings.Workspace }, new JsonSerializerOptions { WriteIndented = true }));
            Application.Current.Shutdown(0);
        }
        catch (Exception ex)
        {
            await File.WriteAllTextAsync(Path.Combine(output, "result.json"), JsonSerializer.Serialize(new { success = false, checks, error = ex.ToString(), status = _state.Status }, new JsonSerializerOptions { WriteIndented = true }));
            Application.Current.Shutdown(1);
        }
    }

    private static void Expect(bool success, string label, ICollection<string> checks)
    {
        if (!success) throw new InvalidOperationException("Smoke 检查失败：" + label); checks.Add(label);
    }
    private async Task VerifyProcessTreeCancellation(string output, ICollection<string> checks)
    {
        var stubRoot = Path.Combine(output, "cancel-fixture");
        var module = Path.Combine(stubRoot, "worker", "mad_worker"); Directory.CreateDirectory(module);
        await File.WriteAllTextAsync(Path.Combine(module, "__init__.py"), "");
        await File.WriteAllTextAsync(Path.Combine(module, "__main__.py"), "import sys,json,subprocess,time\nfrom pathlib import Path\nr=json.loads(sys.stdin.readline())\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\nPath(r['workspace'],'cancel-child.pid').write_text(str(p.pid))\nprint(json.dumps({'type':'progress','progress':0.1,'message':'child-ready'}),flush=True)\ntime.sleep(60)\n", new UTF8Encoding(false));
        var client = new WorkerClient(stubRoot);
        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        var elapsed = Stopwatch.StartNew();
        var signalled = false;
        try
        {
            await client.RunAsync("cancel-fixture", new { }, _state.Settings, new Progress<(double Value, string Message)>(p =>
            { if (p.Message == "child-ready") { signalled = true; cancellation.Cancel(); } }), cancellation.Token);
            throw new InvalidOperationException("Worker 未响应取消。");
        }
        catch (OperationCanceledException) { }
        Expect(elapsed.Elapsed < TimeSpan.FromSeconds(8), "进程树取消在 8 秒内完成，未等待子进程自然结束", checks);
        Expect(signalled, "真实 Worker 接收子进程进度事件", checks);
        var pid = int.Parse(await File.ReadAllTextAsync(Path.Combine(_state.Settings.Workspace, "cancel-child.pid")));
        var stopped = false;
        try { using var process = Process.GetProcessById(pid); stopped = process.HasExited; }
        catch (ArgumentException) { stopped = true; }
        Expect(stopped, "取消终止 Python 及其真实子进程树", checks);
    }
    private void Capture(string path)
    {
        UpdateLayout();
        var content = (FrameworkElement)Content;
        var target = new RenderTargetBitmap((int)content.ActualWidth, (int)content.ActualHeight, 96, 96, PixelFormats.Pbgra32);
        target.Render(content);
        var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(target));
        using var stream = File.Create(path); encoder.Save(stream);
    }
}
