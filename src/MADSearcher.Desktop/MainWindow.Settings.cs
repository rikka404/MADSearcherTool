using System.IO;
using System.Windows;

namespace MADSearcher.Desktop;

/// <summary>Session settings, persistence and environment diagnostics.</summary>
public partial class MainWindow
{
    private void ValidateSettings()
    {
        if (string.IsNullOrWhiteSpace(_state.Settings.PythonPath))
            throw new InvalidOperationException("请先在设置中填写 Python 路径。");
        if (string.IsNullOrWhiteSpace(_state.Settings.Workspace))
            throw new InvalidOperationException("请先在设置中填写项目内工作区路径。");
        var workspace = Path.GetFullPath(_state.Settings.Workspace, _root);
        var prefix = Path.GetFullPath(_root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!workspace.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("工作区必须放在当前项目目录内，例如 " + Path.Combine(_root, "workspace"));
        _state.Settings.Workspace = workspace;
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
            ValidateSettings();
            _state.Settings.Save(_root);
            ClearResults();
            _state.Status = "设置已保存。API Key 仅在本次会话中保留。";
            _ = Execute("正在刷新工作区…", async token => await LoadGroups(token));
        }
        catch (Exception ex)
        {
            _state.Status = "设置未保存：" + SafeError(ex.Message);
        }
    }
    private async void CheckEnvironment(object sender, RoutedEventArgs e)
    {
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
