using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace MADSearcher.Desktop;

/// <summary>Playback and UI state are dispatcher-owned; decoding is a separate cancellable service.</summary>
public partial class BufferedPreview : UserControl, IDisposable
{
    private BufferedFrameSource? _source;
    private WriteableBitmap? _bitmap;
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(8) };
    private readonly DispatcherTimer _seekDelay = new() { Interval = TimeSpan.FromMilliseconds(120) };
    private readonly Stopwatch _clock = new();
    private long _position, _displayed = -1, _clockFrame, _selectionStart;
    private bool _playing, _waiting, _canMark, _disposed, _selectionValid;
    private long _lastStatus;
    public PreviewMedia? Media => _source?.Media;
    public long Position => _position;
    public event Action<BufferedPreview>? Activated;
    public event Action<long>? StartRequested;
    public event Action<long>? EndRequested;

    public BufferedPreview()
    {
        InitializeComponent();
        _timer.Tick += (_, _) => Tick();
        _seekDelay.Tick += (_, _) => { _seekDelay.Stop(); _source?.Request(_position); };
        Timeline.SeekRequested += frame => Seek(frame, debounce: true);
        IsVisibleChanged += (_, _) => { if (!(bool)IsVisible) Suspend(); else if (_source != null) Seek(_position); };
    }

    public void Configure(PreviewMedia media, string ffmpeg, long initialFrame = 0, bool canMark = false)
    {
        if (_disposed) return;
        Clear();
        _source = new BufferedFrameSource(media, ffmpeg, initialFrame);
        _canMark = canMark;
        MarkControls.Visibility = canMark ? Visibility.Visible : Visibility.Collapsed;
        _bitmap = new WriteableBitmap(_source.Width, _source.Height, 96, 96, PixelFormats.Bgra32, null);
        Picture.Source = _bitmap;
        Timeline.HasSelection = canMark;
        Timeline.Reset(media.FrameCount, media.NominalFps);
        Seek(initialFrame);
        if (!IsVisible) Suspend();
    }

    public void Clear()
    {
        Pause(); _seekDelay.Stop(); _source?.Dispose(); _source = null;
        _bitmap = null; Picture.Source = null; _displayed = -1; _position = 0;
        _canMark = false; _selectionValid = false; _waiting = false; MarkControls.Visibility = Visibility.Collapsed;
        SelectionLabel.Text = ""; Timeline.HasSelection = false; Timeline.Reset(1, 24);
        LoadingBadge.Visibility = Visibility.Visible; LoadingText.Text = "尚未载入画面";
        PositionLabel.Text = "00:00:00 · 无声画面预览";
        BufferLabel.Text = "青色：已缓存 · 蓝色：抠像范围 · 空格播放/暂停";
    }

    public void SetSelection(long start, long end)
    {
        _selectionStart = start;
        _selectionValid = end > start;
        Timeline.SetSelection(start, end);
        SelectionLabel.Text = Media == null || !_canMark ? "" : $"抠像范围 {Media.Timecode(start)} → {Media.Timecode(end)}（右边界不含，共{end - start}帧）";
    }
    public void SetSelectionError(string message)
    {
        _selectionValid = false;
        Timeline.SetSelection(0, 0);
        SelectionLabel.Text = message;
    }

    public void Seek(long frame, bool debounce = false)
    {
        if (_source == null) return;
        Pause();
        _position = Math.Clamp(frame, 0, _source.FrameCount - 1);
        Timeline.Position = _position;
        Timeline.Follow();
        _waiting = !ShowFrame(_position);
        if (debounce) { _seekDelay.Stop(); _seekDelay.Start(); }
        else { _seekDelay.Stop(); _source.Request(_position); }
        UpdateStatus();
    }

    public void Play()
    {
        if (_source == null || !IsVisible) return;
        Activated?.Invoke(this);
        if (_position >= _source.FrameCount - 1) Seek(_canMark && _selectionValid ? _selectionStart : 0);
        _playing = true; _clock.Reset(); _seekDelay.Stop();
        _source.Request(_position);
        PlayButton.Content = "Ⅱ 暂停";
    }
    public void Pause()
    {
        _playing = false; _clock.Reset();
        if (PlayButton != null) PlayButton.Content = "▶ 播放";
    }
    public void Suspend() { Pause(); _seekDelay.Stop(); _source?.Suspend(); }

    private bool ShowFrame(long frame)
    {
        if (_source == null || _bitmap == null || !_source.TryGet(frame, out var pixels) || pixels == null) return false;
        if (_displayed != frame)
        {
            _bitmap.WritePixels(new Int32Rect(0, 0, _source.Width, _source.Height), pixels, _source.Width * 4, 0);
            _displayed = frame;
        }
        _waiting = false; Picture.Opacity = 1;
        return true;
    }

    private void Tick()
    {
        if (_source == null || !IsVisible || _disposed) return;
        var count = _source.FrameCount;
        if (_position >= count) { _position = count - 1; _source.Request(_position); }
        if (_source.Error != null) { Pause(); UpdateStatus(); return; }
        if (_waiting && !_seekDelay.IsEnabled) _waiting = !ShowFrame(_position);
        if (_playing && !_seekDelay.IsEnabled)
        {
            if (!_clock.IsRunning)
            {
                // Wait for a small contiguous cushion, then keep roughly five seconds prefetched.
                var ready = !_waiting && ShowFrame(_position);
                var end = Math.Min(count - 1, _position + (long)Math.Ceiling(_source.Media.Fps * 0.5));
                for (var frame = _position; ready && frame <= end; frame++) ready &= _source.TryGet(frame, out _);
                if (ready) { _clockFrame = _position; _clock.Restart(); }
                else _waiting = true;
            }
            else
            {
                var due = Math.Min(count - 1, _clockFrame + (long)(_clock.Elapsed.TotalSeconds * _source.Media.Fps));
                if (due != _position)
                {
                    if (ShowFrame(due))
                    {
                        _position = due; Timeline.Position = due; Timeline.Follow(); _source.Request(due);
                        if (due == count - 1) Pause();
                    }
                    else
                    {
                        // Freeze at the last displayed frame, never advance the selection into a cache gap.
                        _clock.Reset(); _waiting = true; _source.Request(_position);
                    }
                }
            }
        }
        var now = Environment.TickCount64;
        if (now - _lastStatus >= 100) { _lastStatus = now; UpdateStatus(); }
    }

    private void UpdateStatus()
    {
        if (_source == null) return;
        Timeline.SetCount(_source.FrameCount);
        Timeline.SetCached(_source.CachedRanges());
        var media = _source.Media;
        PositionLabel.Text = $"{media.Timecode(_position)} / {media.Timecode(_source.FrameCount)} · 第{_position}帧"
            + (media.SourceOffset != 0 ? $" · 源第{_position + media.SourceOffset}帧" : "") + " · 无声";
        var error = _source.Error;
        LoadingBadge.Visibility = _waiting || error != null ? Visibility.Visible : Visibility.Collapsed;
        LoadingText.Text = error ?? (_displayed == _position ? "正在补充播放缓冲…" : $"正在加载第{_position}帧；首次定位可能需要等待…");
        Picture.Opacity = _displayed == _position ? 1 : 0.35;
        StartButton.IsEnabled = EndButton.IsEnabled = _canMark && _displayed == _position && error == null;
        var ranges = _source.CachedRanges();
        var count = ranges.Sum(r => r.End - r.Start);
        BufferLabel.Text = $"缓存 {count} 帧（约{count / media.Fps:0.0}秒）· 预览 {_source.Width}×{_source.Height} · 青色为已加载范围"
            + (media.EstimatedCount ? " · 全片帧数为估计值" : "");
    }

    private void TogglePlayback(object sender, RoutedEventArgs e) { if (_playing) Pause(); else Play(); }
    private void PreviousFrame(object sender, RoutedEventArgs e) => Seek(_position - 1);
    private void NextFrame(object sender, RoutedEventArgs e) => Seek(_position + 1);
    private void ZoomIn(object sender, RoutedEventArgs e) => Timeline.Zoom(0.5);
    private void ZoomOut(object sender, RoutedEventArgs e) => Timeline.Zoom(2);
    private void ShowAll(object sender, RoutedEventArgs e) => Timeline.ShowAll();
    private void Retry(object sender, RoutedEventArgs e) { if (_source != null) { _waiting = true; _source.Request(_position, retry: true); } }
    private void MarkStart(object sender, RoutedEventArgs e) { Pause(); if (_canMark && _displayed == _position) StartRequested?.Invoke(_position); }
    private void MarkEnd(object sender, RoutedEventArgs e) { Pause(); if (_canMark && _displayed == _position) EndRequested?.Invoke(_position + 1); }
    private void GoToStart(object sender, RoutedEventArgs e) { if (_selectionValid) Seek(_selectionStart); }
    private void PreviewClicked(object sender, MouseButtonEventArgs e) { Focus(); Activated?.Invoke(this); }
    private void PreviewKey(object sender, KeyEventArgs e)
    {
        if (e.OriginalSource is System.Windows.Controls.Primitives.TextBoxBase || Keyboard.Modifiers != ModifierKeys.None) return;
        if (e.Key == Key.Space) { if (!e.IsRepeat) { if (_playing) Pause(); else Play(); } }
        else if (e.Key == Key.Left) Seek(_position - 1);
        else if (e.Key == Key.Right) Seek(_position + 1);
        else if (e.Key == Key.I && _canMark) MarkStart(this, new RoutedEventArgs());
        else if (e.Key == Key.O && _canMark) MarkEnd(this, new RoutedEventArgs());
        else return;
        e.Handled = true;
    }
    private void PreviewLoaded(object sender, RoutedEventArgs e) => _timer.Start();
    private void PreviewUnloaded(object sender, RoutedEventArgs e) { Suspend(); _timer.Stop(); }
    public void Dispose() { if (_disposed) return; _disposed = true; Clear(); _timer.Stop(); }
}
