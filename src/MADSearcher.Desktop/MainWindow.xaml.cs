using Microsoft.Win32;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace MADSearcher.Desktop;

/// <summary>Window shell, shared task lifecycle and UI helpers. Feature handlers live in the named partial files.</summary>
public partial class MainWindow : Window
{
    private readonly string _root;
    private readonly WorkerClient _worker;
    private readonly ViewState _state;
    private readonly bool _smoke;
    private CancellationTokenSource? _cancellation;
    private AppSettings? _taskSettings;
    private AppSettings OperationSettings => _taskSettings ?? _state.Settings;
    private readonly DispatcherTimer _playbackTimer = new() { Interval = TimeSpan.FromMilliseconds(300) };
    private Task _lastOperation = Task.CompletedTask;

    public MainWindow(bool smoke = false)
    {
        _smoke = smoke;
        _root = FindRoot();
        _state = new ViewState { Settings = AppSettings.Load(_root) };
        if (_smoke)
            _state.Settings.Workspace = Path.Combine(_root, "artifacts", "desktop-smoke", "workspace-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss"));
        _worker = new WorkerClient(_root);
        var savedKey = _state.Settings.ApiKey;
        InitializeComponent();
        InitializeCutPreview();
        DataContext = _state;
        ApiKeyBox.Password = savedKey ?? "";
        CutOutputBox.Text = Path.Combine(_state.Settings.Workspace, "exports", "cutout");
        Navigate("search");
        _playbackTimer.Tick += (_, _) =>
        {
            if (PreviewPlayer.Source == null || !PreviewPlayer.NaturalDuration.HasTimeSpan)
                return;
            if (!PreviewSeek.IsMouseCaptureWithin)
                PreviewSeek.Value = PreviewPlayer.Position.TotalSeconds;
            PlaybackPosition.Text = $"{VideoInfo.FormatTime(PreviewPlayer.Position.TotalSeconds)} / {VideoInfo.FormatTime(PreviewPlayer.NaturalDuration.TimeSpan.TotalSeconds)}";
        };
        _playbackTimer.Start();
    }

    private static string FindRoot()
    {
        foreach (var origin in new[] { AppContext.BaseDirectory, Directory.GetCurrentDirectory() })
            for (var directory = new DirectoryInfo(origin); directory != null; directory = directory.Parent)
                if (Directory.Exists(Path.Combine(directory.FullName, "worker", "mad_worker")))
                    return directory.FullName;
        // Preserve a usable settings page when someone moves only the EXE.
        return AppContext.BaseDirectory;
    }

    private async void WindowLoaded(object sender, RoutedEventArgs e)
    {
        if (_smoke)
        {
            await SmokeAsync();
            return;
        }
        await Execute("正在打开素材库…", token => LoadGroups(token));
    }

    private void WindowClosing(object? sender, CancelEventArgs e)
    {
        _cancellation?.Cancel();
        _playbackTimer.Stop();
        PreviewPlayer.Close();
        CloseCutPreview();
        _state.Settings.ApiKey = "";
    }

    private Task Execute(string message, Func<CancellationToken, Task> action)
    {
        if (_state.Busy)
            return Task.CompletedTask;
        _lastOperation = ExecuteCore(message, action);
        return _lastOperation;
    }

    private async Task ExecuteCore(string message, Func<CancellationToken, Task> action)
    {
        _state.Busy = true;
        _state.Progress = 0;
        _state.Status = message;
        _cancellation = new CancellationTokenSource();
        _taskSettings = _state.Settings.Snapshot();
        try
        {
            ValidateSettings(_taskSettings);
            await action(_cancellation.Token);
            _state.Progress = 1;
            if (_state.Status == message)
                _state.Status = "已完成。";
        }
        catch (OperationCanceledException)
        {
            _state.Status = "任务已取消。原素材和已完成的索引仍保留。";
            _state.Progress = 0;
        }
        catch (Exception ex)
        {
            _state.Status = "操作未完成：" + SafeError(ex.Message);
            _state.Progress = 0;
        }
        finally
        {
            _taskSettings = null;
            _state.Busy = false;
            _cancellation.Dispose();
            _cancellation = null;
        }
    }

    private string SafeError(string message)
    {
        foreach (var key in new[] { _state.Settings.ApiKey, _taskSettings?.ApiKey })
            if (!string.IsNullOrEmpty(key))
                message = message.Replace(key, "[已隐藏密钥]", StringComparison.Ordinal);
        return message;
    }

    private Task<JsonElement> Call(string command, object parameters, CancellationToken token)
    {
        var settings = OperationSettings;
        return _worker.RunAsync(command, parameters, settings, new Progress<(double Value, string Message)>(p =>
        {
            if (!ReferenceEquals(_taskSettings, settings) || token.IsCancellationRequested)
                return;
            _state.Progress = Math.Clamp(p.Value, 0, 1);
            _state.Status = SafeError(p.Message);
        }), token);
    }

    private static T[] Items<T>(JsonElement data, string property) => data.TryGetProperty(property, out var array) ? JsonSerializer.Deserialize<T[]>(array.GetRawText()) ?? [] : [];
    private static string Text(JsonElement data, string property) => data.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : "";
    private static string Warnings(JsonElement data) => data.TryGetProperty("warnings", out var values) && values.ValueKind == JsonValueKind.Array ? string.Join("；", values.EnumerateArray().Select(v => v.ToString())) : "";
    private static string Number(double value) => value.ToString("0.###", CultureInfo.InvariantCulture);
    private static double Parse(string value, string label)
    {
        if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var number) || !double.IsFinite(number))
            throw new InvalidOperationException(label + "必须是有效数字，秒数可使用小数点，例如 12.5。");
        return number;
    }
    private static (double Start, double End) Range(TextBox startBox, TextBox endBox, double? duration = null, double? maxLength = null)
    {
        var start = Parse(startBox.Text, "起点");
        var end = Parse(endBox.Text, "终点");
        return Range(start, end, duration, maxLength);
    }
    private static (double Start, double End) Range(double start, double end, double? duration = null, double? maxLength = null)
    {
        if (start < 0 || end <= start)
            throw new InvalidOperationException("起点必须大于等于 0，终点必须晚于起点。");
        if (duration.HasValue && end > duration.Value + 0.001)
            throw new InvalidOperationException($"终点不能超过视频时长 {Number(duration.Value)} 秒。");
        if (maxLength.HasValue && end - start > maxLength.Value)
            throw new InvalidOperationException($"本操作最长支持 {maxLength} 秒，请先缩短片段。");
        return (start, end);
    }
    private static string RequireFile(string path, string label)
    {
        path = path.Trim().Trim('"');
        if (!File.Exists(path))
            throw new InvalidOperationException($"{label}文件不存在，请重新选择。");
        return Path.GetFullPath(path);
    }
    private void CancelTask(object sender, RoutedEventArgs e)
    {
        _state.Status = "正在取消任务并停止子进程…";
        _cancellation?.Cancel();
    }
    private void ShowSearch(object sender, RoutedEventArgs e) => Navigate("search");
    private void ShowCutout(object sender, RoutedEventArgs e) => Navigate("cutout");
    private void ShowSettings(object sender, RoutedEventArgs e) => Navigate("settings");
    private void Navigate(string page)
    {
        PreviewPlayer.Pause();
        SourcePreview.Suspend();
        ResultPreview.Suspend();
        SearchPage.Visibility = page == "search" ? Visibility.Visible : Visibility.Collapsed;
        CutoutPage.Visibility = page == "cutout" ? Visibility.Visible : Visibility.Collapsed;
        SettingsPage.Visibility = page == "settings" ? Visibility.Visible : Visibility.Collapsed;
        foreach (var (button, id) in new[] { (SearchNav, "search"), (CutoutNav, "cutout"), (SettingsNav, "settings") })
        {
            button.Background = page == id ? new SolidColorBrush(Color.FromRgb(35, 66, 60)) : Brushes.Transparent;
            button.Foreground = page == id ? (Brush)FindResource("AccentBrush") : (Brush)FindResource("MutedBrush");
        }
    }
    private static bool ChooseFile(TextBox target, string filter, string title)
    {
        var dialog = new OpenFileDialog { Filter = filter, Title = title };
        if (dialog.ShowDialog() != true)
            return false;
        target.Text = dialog.FileName;
        return true;
    }
    private static string? ChooseDirectory(string title, string initial)
    {
        var dialog = new OpenFolderDialog { Title = title };
        if (Directory.Exists(initial))
            dialog.InitialDirectory = initial;
        return dialog.ShowDialog() == true ? dialog.FolderName : null;
    }
    private void OpenPath(string path)
    {
        try
        {
            if (!File.Exists(path) && !Directory.Exists(path))
                throw new InvalidOperationException("路径不存在，请先完成对应操作。");
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            _state.Status = "无法打开：" + SafeError(ex.Message);
        }
    }
    private void OpenWorkspace(object sender, RoutedEventArgs e)
    {
        try
        {
            var workspace = Path.GetFullPath(OperationSettings.Workspace, _root);
            Directory.CreateDirectory(workspace);
            OpenPath(workspace);
        }
        catch (Exception ex)
        {
            _state.Status = "无法打开工作区：" + SafeError(ex.Message);
        }
    }
    private void OpenGuide(object sender, RoutedEventArgs e)
    {
        var path = Path.Combine(_root, "Docs", "使用说明.md");
        if (!File.Exists(path))
            path = Path.Combine(_root, "Docs", "设计文档.md");
        OpenPath(path);
    }
    private void ShowAbout(object sender, RoutedEventArgs e) => MessageBox.Show(this, "MADSearcher 1.0\n\n面向 MAD 创作的素材检索与视频人物分割工具。\n本地素材库 · 外挂字幕 / ASR · 可选 AI 画面分析 · SAM 2 时序分割\n\nPSD 序列暂不支持。", "关于 MADSearcher");
    private void ExitApp(object sender, RoutedEventArgs e) => Close();

    private void PlayerFailed(object sender, ExceptionRoutedEventArgs e) => _state.Status = "内置播放器无法播放此文件。生成标准 MP4 预览后重试，或使用外部播放器检查输出。";
}
