using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace MADSearcher.Desktop;

public sealed class WorkerClient(string root)
{
    public async Task<JsonElement> RunAsync(string command, object parameters, AppSettings settings,
        IProgress<(double Value, string Message)> progress, CancellationToken cancellationToken)
    {
        var start = new ProcessStartInfo
        {
            FileName = settings.PythonPath.Trim(), WorkingDirectory = Path.Combine(root, "worker"),
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        start.ArgumentList.Add("-u"); start.ArgumentList.Add("-m"); start.ArgumentList.Add("mad_worker");
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        start.Environment["PYTHONUTF8"] = "1";
        start.Environment["HF_HOME"] = Path.Combine(root, "models", "huggingface");
        start.Environment["TORCH_HOME"] = Path.Combine(root, "models", "torch");
        var temporary = Path.Combine(settings.Workspace, "temp"); Directory.CreateDirectory(temporary);
        start.Environment["TEMP"] = temporary; start.Environment["TMP"] = temporary;
        using var process = new Process { StartInfo = start };
        try { process.Start(); }
        catch (Exception e) when (e is System.ComponentModel.Win32Exception or InvalidOperationException)
        { throw new InvalidOperationException("无法启动 Python。请运行 scripts/setup.ps1，然后在设置中选择 .venv\\Scripts\\python.exe。", e); }
        using var job = AttachJob(process);
        using var registration = cancellationToken.Register(() =>
        {
            job.Terminate();
            try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
            catch (Exception e) when (e is InvalidOperationException or System.ComponentModel.Win32Exception) { }
        });
        // Drain stderr concurrently so model diagnostics can never block the protocol pipe.
        var errorTask = DrainErrorAsync(process.StandardError);
        JsonElement? result = null; string? failure = null;
        try
        {
            var request = new Dictionary<string, object>
            { ["command"] = command, ["params"] = parameters, ["settings"] = settings.WorkerValues(), ["workspace"] = settings.Workspace };
            await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize(request));
            process.StandardInput.Close();
            while (await process.StandardOutput.ReadLineAsync(cancellationToken) is { } line)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                using var doc = JsonDocument.Parse(line);
                var item = doc.RootElement;
                switch (item.GetProperty("type").GetString())
                {
                    case "progress":
                        progress.Report((item.TryGetProperty("progress", out var p) ? p.GetDouble() : 0,
                            item.TryGetProperty("message", out var m) ? m.GetString() ?? "正在处理…" : "正在处理…")); break;
                    case "result": result = item.GetProperty("data").Clone(); break;
                    case "error": failure = item.GetProperty("message").GetString(); break;
                }
            }
            await process.WaitForExitAsync(cancellationToken);
            var diagnostic = await errorTask;
            cancellationToken.ThrowIfCancellationRequested();
            if (failure != null) throw new InvalidOperationException(Redact(failure, settings.ApiKey));
            if (process.ExitCode != 0 || result == null)
                throw new InvalidOperationException("处理进程未完成。请运行环境检查。" + (diagnostic.Length > 0 ? "\n" + Redact(diagnostic, settings.ApiKey) : ""));
            return result.Value;
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception) when (cancellationToken.IsCancellationRequested) { throw new OperationCanceledException(cancellationToken); }
        catch (JsonException e) { throw new InvalidOperationException("处理进程返回了无效数据。请检查 Python 路径是否指向项目运行环境。", e); }
        finally
        {
            job.Terminate();
            try { if (!process.HasExited) { process.Kill(entireProcessTree: true); await process.WaitForExitAsync(); } }
            catch (Exception e) when (e is InvalidOperationException or System.ComponentModel.Win32Exception) { }
            await errorTask;
        }
    }

    private static ProcessJob AttachJob(Process process)
    {
        try { return new ProcessJob(process); }
        catch
        {
            try { if (!process.HasExited) process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
            throw;
        }
    }

    private static string Redact(string value, string key) => string.IsNullOrEmpty(key) ? value : value.Replace(key, "[已隐藏密钥]", StringComparison.Ordinal);
    private static async Task<string> DrainErrorAsync(StreamReader stream)
    {
        var tail = new StringBuilder();
        var buffer = new char[1024]; int count;
        while ((count = await stream.ReadAsync(buffer)) > 0)
        {
            tail.Append(buffer, 0, count);
            if (tail.Length > 4096) tail.Remove(0, tail.Length - 4096);
        }
        return tail.ToString().Trim();
    }
}
