using System.IO;
using System.Windows;

namespace MADSearcher.Desktop;

/// <summary>Local settings, persistence and environment diagnostics.</summary>
public partial class MainWindow
{
    private void ValidateSettings(AppSettings settings)
    {
        if (string.IsNullOrWhiteSpace(settings.PythonPath))
            throw new InvalidOperationException("请先在设置中填写 Python 路径。");
        if (string.IsNullOrWhiteSpace(settings.Workspace))
            throw new InvalidOperationException("请先在设置中填写项目内工作区路径。");
        var workspace = Path.GetFullPath(settings.Workspace, _root);
        var prefix = Path.GetFullPath(_root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!workspace.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("工作区必须放在当前项目目录内，例如 " + Path.Combine(_root, "workspace"));
        settings.Workspace = workspace;
        Directory.CreateDirectory(workspace);
        if (!Directory.Exists(Path.Combine(_root, "worker", "mad_worker")))
            throw new InvalidOperationException("找不到 worker。请通过项目 scripts/start.ps1 启动，或把发布目录放回项目内。");
    }

    private void ApiKeyChanged(object sender, RoutedEventArgs e)
    {
        if (_state != null)
            _state.Settings.ApiKey = ApiKeyBox.Password;
    }
    private void SaveSettings(object sender, RoutedEventArgs e)
    {
        try
        {
            ValidateSettings(_state.Settings);
            if (_state.Busy && !string.Equals(_state.Settings.Workspace, OperationSettings.Workspace, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("请等当前任务结束后再切换工作区；其他设置可以继续保存。");
            _state.Settings.Save(_root);
            _state.SettingsHint = "设置与 API Key 已保存到本地。" + (_state.Busy ? "当前任务继续使用启动时的参数，新设置在下次任务生效。" : "下次启动会自动恢复。");
            if (!_state.Busy)
            {
                _state.Status = _state.SettingsHint;
                if (!string.Equals(_libraryWorkspace, _state.Settings.Workspace, StringComparison.OrdinalIgnoreCase))
                {
                    ClearResults();
                    _ = Execute("正在刷新工作区…", async token => await LoadGroups(token));
                }
            }
        }
        catch (Exception ex)
        {
            _state.SettingsHint = "设置未保存：" + SafeError(ex.Message);
            if (!_state.Busy)
                _state.Status = _state.SettingsHint;
        }
    }
    private async void CheckEnvironment(object sender, RoutedEventArgs e)
    {
        if (_state.Busy)
            return;
        Navigate("settings");
        await Execute("正在检查运行环境…", async token =>
        {
            var data = await Call("system.check", new
            {
            }, token);
            var lines = data.GetProperty("checks").EnumerateArray().Select(item => $"{(item.GetProperty("ok").GetBoolean() ? "✓" : "○")} {Text(item, "name")}：{Text(item, "message")}");
            _state.Diagnostics = "Python " + Text(data, "python") + "\n\n" + string.Join("\n\n", lines);
            _state.Status = "环境检查完成。未配置的可选功能可稍后安装。";
        });
    }
}
