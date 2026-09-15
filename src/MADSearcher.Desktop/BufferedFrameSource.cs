using System.Diagnostics;
using System.Globalization;
using System.IO;

namespace MADSearcher.Desktop;

/// <summary>Immutable identity and frame mapping for one preview session.</summary>
public sealed record PreviewMedia(string Path, int Width, int Height, double Fps, int NominalFps,
    long FrameCount, long SourceOffset = 0, bool ImageSequence = false, bool EstimatedCount = false)
{
    public string Timecode(long frame)
    {
        var seconds = Math.Max(0, frame) / NominalFps;
        return $"{seconds / 60:00}:{seconds % 60:00}:{Math.Max(0, frame) % NominalFps:00}";
    }
}

/// <summary>
/// One read-only decoder with backpressure. Cache and request state are protected by _gate;
/// process ownership is confined to the pump, so a seek never starts overlapping decoders.
/// </summary>
public sealed class BufferedFrameSource : IDisposable
{
    private const long PixelBudget = 128L * 1024 * 1024;
    private readonly object _gate = new();
    private readonly Dictionary<long, byte[]> _frames = [];
    private readonly SemaphoreSlim _changed = new(0, 1);
    private readonly CancellationTokenSource _lifetime = new();
    private readonly string _ffmpeg;
    private readonly Task _pump;
    private CancellationTokenSource? _decoder;
    private long _position, _decodeStart, _next;
    private bool _suspended, _disposed;
    private string? _error;
    private long _count;
    private readonly int _capacity, _ahead, _behind;
    public PreviewMedia Media { get; }
    public int Width { get; }
    public int Height { get; }
    public long FrameCount { get { lock (_gate) return _count; } }
    public string? Error { get { lock (_gate) return _error; } }

    public BufferedFrameSource(PreviewMedia media, string ffmpeg, long initialFrame)
    {
        if (media.Width <= 0 || media.Height <= 0 || !double.IsFinite(media.Fps) || media.Fps is < 1 or > 120
            || media.FrameCount < 1 || media.NominalFps < 1)
            throw new InvalidOperationException("预览视频的尺寸、帧率或帧数无效。");
        Media = media;
        _ffmpeg = ffmpeg;
        _count = media.FrameCount;
        var scale = Math.Min(1, 640.0 / Math.Max(media.Width, media.Height));
        // Fit roughly eight seconds, including forward and backward frames, in the budget.
        scale = Math.Min(scale, Math.Sqrt(PixelBudget / (media.Width * (double)media.Height * 4 * Math.Ceiling(media.Fps * 8))));
        Width = Math.Max(1, (int)(media.Width * scale));
        Height = Math.Max(1, (int)(media.Height * scale));
        _capacity = (int)Math.Min(Math.Ceiling(media.Fps * 8), PixelBudget / ((long)Width * Height * 4));
        _ahead = Math.Min((int)Math.Ceiling(media.Fps * 5), _capacity - 2);
        _behind = Math.Min((int)Math.Ceiling(media.Fps * 2), _capacity - _ahead - 1);
        _position = Math.Clamp(initialFrame, 0, _count - 1);
        _pump = Task.Run(PumpAsync);
    }

    public void Request(long frame, bool retry = false)
    {
        lock (_gate)
        {
            if (_disposed) return;
            _position = Math.Clamp(frame, 0, _count - 1);
            _suspended = false;
            if (retry) _error = null;
            if (_decoder != null && !_frames.ContainsKey(_position)
                && (_position < _decodeStart || _position > _next + _ahead))
                _decoder.Cancel();
            Pulse();
        }
    }

    public void Suspend()
    {
        lock (_gate)
        {
            if (_disposed) return;
            _suspended = true;
            _decoder?.Cancel();
            Pulse();
        }
    }

    public bool TryGet(long frame, out byte[]? pixels)
    {
        lock (_gate) return _frames.TryGetValue(frame, out pixels);
    }

    public (long Start, long End)[] CachedRanges()
    {
        lock (_gate)
        {
            var ranges = new List<(long Start, long End)>();
            foreach (var frame in _frames.Keys.Order())
            {
                if (ranges.Count > 0 && ranges[^1].End == frame)
                    ranges[^1] = (ranges[^1].Start, frame + 1);
                else ranges.Add((frame, frame + 1));
            }
            return ranges.ToArray();
        }
    }

    private void Pulse() { if (_changed.CurrentCount == 0) _changed.Release(); }
    private long FirstMissing()
    {
        var end = Math.Min(_count - 1, _position + _ahead);
        for (var frame = _position; frame <= end; frame++)
            if (!_frames.ContainsKey(frame)) return frame;
        return -1;
    }

    private async Task PumpAsync()
    {
        try
        {
            while (!_lifetime.IsCancellationRequested)
            {
                long start;
                CancellationTokenSource? decoder = null;
                lock (_gate)
                {
                    start = _suspended || _error != null ? -1 : FirstMissing();
                    if (start >= 0)
                    {
                        _decodeStart = _next = start;
                        decoder = _decoder = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
                    }
                }
                if (decoder == null)
                {
                    await _changed.WaitAsync(_lifetime.Token);
                    continue;
                }
                try { await DecodeAsync(start, decoder.Token); }
                catch (OperationCanceledException) when (decoder.IsCancellationRequested) { }
                catch (Exception ex)
                {
                    lock (_gate)
                        if (!decoder.IsCancellationRequested)
                            _error = ex is System.ComponentModel.Win32Exception ? "无法启动FFmpeg，请检查设置中的路径。"
                                : "画面加载失败：" + ex.Message;
                }
                finally
                {
                    lock (_gate) { _decoder = null; decoder.Dispose(); }
                }
            }
        }
        catch (OperationCanceledException) when (_lifetime.IsCancellationRequested) { }
        finally { _changed.Dispose(); _lifetime.Dispose(); }
    }

    private async Task<bool> WaitForDemand(long frame, CancellationToken token)
    {
        while (true)
        {
            token.ThrowIfCancellationRequested();
            lock (_gate)
            {
                if (_suspended || frame >= _count) return false;
                var missing = FirstMissing();
                if (missing >= 0 && (missing < frame || missing > frame + _ahead)) return false;
                if (frame <= _position + _ahead) return true;
            }
            await _changed.WaitAsync(token);
        }
    }

    private async Task DecodeAsync(long startFrame, CancellationToken token)
    {
        var start = new ProcessStartInfo(_ffmpeg.Trim())
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        void Args(params string[] values) { foreach (var value in values) start.ArgumentList.Add(value); }
        string N(long value) => value.ToString(CultureInfo.InvariantCulture);
        Args("-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "2");
        if (Media.ImageSequence)
            Args("-framerate", Media.Fps.ToString("R", CultureInfo.InvariantCulture), "-start_number", N(startFrame));
        Args("-i", Media.Path, "-map", "0:v:0", "-an", "-sn", "-dn", "-filter_threads", "1");
        var filter = Media.ImageSequence ? "" : $"trim=start_frame={N(startFrame)},";
        filter += $"scale={Width}:{Height},setsar=1";
        Args("-vf", filter, "-fps_mode", "passthrough", "-pix_fmt", "bgra", "-c:v", "rawvideo", "-threads", "1",
            "-frames:v", N(Media.FrameCount - startFrame), "-f", "rawvideo", "pipe:1");
        using var process = new Process { StartInfo = start };
        token.ThrowIfCancellationRequested();
        process.Start();
        ProcessJob? job = null;
        // Drain stderr without retaining decoder text (paths/content are unnecessary for the UI).
        var stderr = process.StandardError.BaseStream.CopyToAsync(Stream.Null);
        try
        {
            if (!process.HasExited) job = new ProcessJob(process);
            using var cancellation = token.Register(() =>
            {
                try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
                catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception) { }
            });
            var output = process.StandardOutput.BaseStream;
            for (var frame = startFrame; await WaitForDemand(frame, token); frame++)
            {
                var pixels = new byte[checked(Width * Height * 4)];
                var offset = 0;
                while (offset < pixels.Length)
                {
                    var read = await output.ReadAsync(pixels.AsMemory(offset), token);
                    if (read == 0) break;
                    offset += read;
                }
                token.ThrowIfCancellationRequested();
                if (offset != pixels.Length)
                {
                    await process.WaitForExitAsync(token);
                    if (process.ExitCode != 0 || offset != 0 || frame == startFrame)
                        throw new IOException($"没有完整解码第{frame}帧（退出码{process.ExitCode}），请检查素材或重试。");
                    if (!Media.EstimatedCount)
                        throw new IOException("视频提前结束，与声明帧数不一致。");
                    lock (_gate) _count = Math.Max(1, frame);
                    return;
                }
                lock (_gate)
                {
                    _next = frame + 1;
                    _frames[frame] = pixels;
                    // Keep the local playback window; evict farthest frames if jumping left a full cache.
                    foreach (var key in _frames.Keys.Where(k => k < _position - _behind || k > _position + _ahead).ToArray())
                        _frames.Remove(key);
                    while (_frames.Count > _capacity)
                        _frames.Remove(_frames.Keys.MaxBy(k => Math.Abs(k - _position)));
                }
            }
        }
        finally
        {
            job?.Terminate();
            try { if (!process.HasExited) process.Kill(entireProcessTree: true); }
            catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception) { }
            await process.WaitForExitAsync();
            await stderr;
            job?.Dispose();
        }
    }

    public void Dispose()
    {
        lock (_gate)
        {
            if (_disposed) return;
            _disposed = true;
            _frames.Clear();
            Pulse();
            _lifetime.Cancel();
        }
        // The observed pump owns disposal after its process has exited; no UI-thread wait.
        _ = _pump.ContinueWith(t => Debug.WriteLine(t.Exception), TaskContinuationOptions.OnlyOnFaulted);
    }
}
