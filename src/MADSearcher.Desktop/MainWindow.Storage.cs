using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows;

namespace MADSearcher.Desktop;

/// <summary>Storage maintenance uses the same captured workspace and worker task lifecycle.</summary>
public partial class MainWindow
{
    private bool _storageMaintenance;
    private static long Integer(JsonElement data, string key) => data.TryGetProperty(key, out var value) && value.TryGetInt64(out var number) ? number : 0;
    private static string StorageSize(long bytes) => Math.Abs(bytes) >= 1024L * 1024 * 1024
        ? $"{bytes / (1024.0 * 1024 * 1024):0.00} GiB" : $"{bytes / (1024.0 * 1024):0.0} MiB";

    private void OpenIndexDirectory(object sender, RoutedEventArgs e)
    {
        try
        {
            var workspace = Path.GetFullPath(OperationSettings.Workspace, _root);
            Directory.CreateDirectory(workspace);
            var database = Path.Combine(workspace, "library.sqlite3");
            if (File.Exists(database))
            {
                var start = new ProcessStartInfo("explorer.exe") { UseShellExecute = false };
                start.ArgumentList.Add("/select,");
                start.ArgumentList.Add(database);
                Process.Start(start);
            }
            else OpenPath(workspace);
            _state.Status = "索引目录：" + workspace + "；library.sqlite3 保存索引，thumbnails 保存结果缩略图。";
        }
        catch (Exception ex) { _state.Status = "无法打开索引目录：" + SafeError(ex.Message); }
    }

    private string[] StorageProtectedPaths()
    {
        // Explicit inputs may be picked from a cache folder without being registered in the library.
        return new[] { CutPathBox.Text, MaskPathBox.Text, ReferencePathBox.Text, SubtitleBox.Text,
                CutOutputBox.Text, _cutResultDirectory, _aeScript, SourcePreview.Media?.Path }
            .Where(value => !string.IsNullOrWhiteSpace(value))
            .Select(value => Path.GetFullPath(value!.Trim().Trim('"'), _root))
            .Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
    }

    private async void CleanCache(object sender, RoutedEventArgs e) => await Execute("正在扫描缓存…", async token =>
    {
        _storageMaintenance = true;
        try
        {
            _cutRangeDelay.Stop();
            _cutRangeCancellation?.Cancel();
            SourcePreview.Suspend();
            ResultPreview.Suspend();
            var protectedPaths = StorageProtectedPaths();
            var plan = await Call("storage.scan", new { protected_paths = protectedPaths }, token);
            var description = new StringBuilder("将清理以下缓存和历史记录：\n\n");
            foreach (var group in plan.GetProperty("groups").EnumerateArray())
                description.AppendLine($"{Text(group, "name")}：{StorageSize(Integer(group, "bytes"))}（{Integer(group, "files")} 个文件）");
            description.AppendLine($"\n候选删除：{StorageSize(Integer(plan, "bytes"))}");
            var migrations = Integer(plan, "thumbnail_migration_count");
            if (migrations > 0)
                description.AppendLine($"先保存 {migrations} 张旧索引缩略图，最多需额外 {StorageSize(Integer(plan, "thumbnail_copy_bytes_upper_bound"))}；实际释放空间会相应减少。");
            description.AppendLine("\n保留索引数据库、正在使用的缩略图、角色参考图、模型和所有任务结果（含未完成抠像）。");
            description.AppendLine("清理后仍可搜索；再次生成预览或重建索引时，可能需要重新抽帧或请求模型。历史日志和数据库备份无法恢复。");
            description.AppendLine("安装包及依赖下载缓存来自当前项目：" + _root);
            var warnings = Warnings(plan);
            if (warnings.Length > 0) description.AppendLine("\n注意：" + SafeError(warnings));
            description.AppendLine("\n当前工作区：" + Text(plan, "workspace") + "\n\n确定删除？");
            if (Integer(plan, "files") == 0 && migrations == 0)
            {
                _state.Status = "没有可清理文件。" + SafeError(warnings);
                return;
            }
            if (MessageBox.Show(this, description.ToString(), "删除缓存", MessageBoxButton.YesNo,
                    MessageBoxImage.Question, MessageBoxResult.No) != MessageBoxResult.Yes)
            {
                _state.Status = "已取消清理，文件保持不变。";
                return;
            }
            token.ThrowIfCancellationRequested();
            // Release temporary previews and stale thumbnail objects before their paths change.
            ClearResults();
            var result = await Call("storage.clean", new { plan_token = Text(plan, "plan_token"), protected_paths = protectedPaths }, token);
            var failed = Integer(result, "failed_files");
            var summary = $"已删除 {Integer(result, "removed_files")} 个缓存文件，净释放 {StorageSize(Integer(result, "net_reclaimed_bytes"))}；"
                + $"保护迁移 {Integer(result, "migrated_thumbnails")} 张缩略图。索引和任务结果保留，可重新搜索。";
            if (failed > 0 || Integer(result, "skipped_files") > 0)
                summary += $" {failed} 个文件未能删除，{Integer(result, "skipped_files")} 个文件因保护或变化跳过。";
            if (failed > 0)
            {
                var details = string.Join("\n", result.GetProperty("failures").EnumerateArray()
                    .Select(item => Text(item, "path") + "：" + Text(item, "message")));
                MessageBox.Show(this, SafeError(summary + "\n\n" + details), "部分缓存未清理", MessageBoxButton.OK, MessageBoxImage.Information);
            }
            _state.Status = summary + SafeError(Warnings(result));
        }
        finally { _storageMaintenance = false; }
    });
}
