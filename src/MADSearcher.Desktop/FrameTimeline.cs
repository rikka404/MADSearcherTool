using System.Globalization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;

namespace MADSearcher.Desktop;

/// <summary>Frame-coordinate timeline. All ranges use an exclusive right boundary.</summary>
public sealed class FrameTimeline : FrameworkElement
{
    public event Action<long>? SeekRequested;
    private long _count = 1, _position, _selectionStart, _selectionEnd;
    private double _viewStart, _viewEnd = 1;
    private (long Start, long End)[] _cached = [];
    private readonly Brush _accent = new SolidColorBrush(Color.FromRgb(65, 211, 182));
    public int NominalFps { get; set; } = 24;
    public bool HasSelection { get; set; }
    public long Position
    {
        get => _position;
        set { _position = value; InvalidateVisual(); }
    }
    public FrameTimeline()
    {
        Height = 62;
        Cursor = Cursors.Hand;
        ToolTip = "拖动指针定位；青色为已缓存画面，蓝色为抠像范围。Ctrl+滚轮缩放，左右键逐帧。";
    }
    public void Reset(long count, int nominal)
    {
        _count = Math.Max(1, count); NominalFps = nominal;
        _viewStart = 0; _viewEnd = _count; _cached = [];
        _position = 0; _selectionStart = _selectionEnd = 0;
        InvalidateVisual();
    }
    public void SetCount(long count)
    {
        if (_count == count) return;
        _count = Math.Max(1, count); _viewEnd = Math.Min(_viewEnd, _count);
        _viewStart = Math.Min(_viewStart, Math.Max(0, _viewEnd - 1));
        InvalidateVisual();
    }
    public void SetSelection(long start, long end)
    {
        _selectionStart = start; _selectionEnd = end; InvalidateVisual();
    }
    public void SetCached((long Start, long End)[] ranges) { _cached = ranges; InvalidateVisual(); }
    public void Zoom(double factor)
    {
        var span = Math.Clamp((_viewEnd - _viewStart) * factor, Math.Min(_count, 12), _count);
        _viewStart = Math.Clamp(_position - span / 2, 0, _count - span);
        _viewEnd = _viewStart + span;
        InvalidateVisual();
    }
    public void ShowAll() { _viewStart = 0; _viewEnd = _count; InvalidateVisual(); }
    public void Follow()
    {
        if (_position >= _viewStart && _position < _viewEnd) return;
        var span = _viewEnd - _viewStart;
        _viewStart = Math.Clamp(_position - span / 4, 0, _count - span);
        _viewEnd = _viewStart + span;
        InvalidateVisual();
    }
    private double X(double frame) => 8 + (frame - _viewStart) / Math.Max(1, _viewEnd - _viewStart) * Math.Max(1, ActualWidth - 16);
    private long At(double x) => Math.Clamp((long)Math.Floor(_viewStart + (x - 8) / Math.Max(1, ActualWidth - 16) * (_viewEnd - _viewStart)), 0, _count - 1);
    protected override void OnRender(DrawingContext dc)
    {
        base.OnRender(dc);
        dc.DrawRectangle(new SolidColorBrush(Color.FromRgb(15, 25, 35)), null, new Rect(0, 0, ActualWidth, ActualHeight));
        void Bar(long start, long end, double y, double height, Brush brush)
        {
            var left = X(Math.Max(start, _viewStart)); var right = X(Math.Min(end, _viewEnd));
            if (right > left) dc.DrawRectangle(brush, null, new Rect(left, y, right - left, height));
        }
        if (HasSelection && _selectionEnd > _selectionStart)
        {
            Bar(_selectionStart, _selectionEnd, 9, 24, new SolidColorBrush(Color.FromArgb(150, 56, 113, 177)));
            foreach (var boundary in new[] { _selectionStart, _selectionEnd })
                if (boundary >= _viewStart && boundary <= _viewEnd)
                    dc.DrawLine(new Pen(Brushes.LightSkyBlue, 2), new Point(X(boundary), 8), new Point(X(boundary), 34));
        }
        foreach (var range in _cached) Bar(range.Start, range.End, 35, 4, _accent);
        for (var i = 0; i <= 4; i++)
        {
            var frame = (long)(_viewStart + (_viewEnd - _viewStart) * i / 4);
            var seconds = frame / Math.Max(1, NominalFps);
            var label = $"{seconds / 60:00}:{seconds % 60:00}:{frame % Math.Max(1, NominalFps):00}";
            var text = new FormattedText(label, CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
                new Typeface("Segoe UI"), 10, Brushes.LightSlateGray, VisualTreeHelper.GetDpi(this).PixelsPerDip);
            dc.DrawText(text, new Point(Math.Clamp(X(frame) - text.Width / 2, 0, Math.Max(0, ActualWidth - text.Width)), 44));
        }
        if (_position >= _viewStart && _position <= _viewEnd)
        {
            var x = X(_position);
            dc.DrawLine(new Pen(Brushes.White, 2), new Point(x, 5), new Point(x, 40));
            var triangle = new StreamGeometry();
            using (var path = triangle.Open()) { path.BeginFigure(new Point(x - 5, 2), true, true); path.LineTo(new Point(x + 5, 2), true, false); path.LineTo(new Point(x, 9), true, false); }
            dc.DrawGeometry(Brushes.White, null, triangle);
        }
    }
    protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonDown(e);
        CaptureMouse(); SeekRequested?.Invoke(At(e.GetPosition(this).X)); e.Handled = true;
    }
    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        if (IsMouseCaptured && e.LeftButton == MouseButtonState.Pressed)
            SeekRequested?.Invoke(At(e.GetPosition(this).X));
    }
    protected override void OnMouseLeftButtonUp(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonUp(e);
        if (IsMouseCaptured) { SeekRequested?.Invoke(At(e.GetPosition(this).X)); ReleaseMouseCapture(); e.Handled = true; }
    }
    protected override void OnMouseWheel(MouseWheelEventArgs e)
    {
        base.OnMouseWheel(e);
        if ((Keyboard.Modifiers & ModifierKeys.Control) != 0) { Zoom(e.Delta > 0 ? 0.5 : 2); e.Handled = true; }
    }
}
