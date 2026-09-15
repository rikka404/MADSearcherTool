using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows.Media.Imaging;

namespace MADSearcher.Desktop;

public class Observable : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;
    protected void Changed([CallerMemberName] string? name = null) => PropertyChanged?.Invoke(this, new(name));
    protected bool Set<T>(ref T field, T value, [CallerMemberName] string? name = null)
    {
        if (EqualityComparer<T>.Default.Equals(field, value)) return false;
        field = value; Changed(name); return true;
    }
}

public sealed class AppSettings
{
    public string PythonPath { get; set; } = "python";
    public string Workspace { get; set; } = "";
    public string FfmpegPath { get; set; } = "ffmpeg";
    public string FfprobePath { get; set; } = "ffprobe";
    public string VisionModel { get; set; } = "gpt-4.1-mini";
    public string EmbeddingModel { get; set; } = "text-embedding-3-small";
    public string SamCheckpoint { get; set; } = "";
    public string SamConfig { get; set; } = "configs/sam2.1/sam2.1_hiera_t.yaml";
    public string Device { get; set; } = "cpu";
    public string WhisperModel { get; set; } = "small";
    public string WhisperDevice { get; set; } = "cpu";
    public string AfterFxPath { get; set; } = "";
    public string ApiKey { get; set; } = "";

    // Settings contain only value/string properties. Each task keeps its own copy.
    public AppSettings Snapshot() => (AppSettings)MemberwiseClone();

    public Dictionary<string, object> WorkerValues() => new()
    {
        ["ffmpeg_path"] = FfmpegPath, ["ffprobe_path"] = FfprobePath,
        ["api_key"] = ApiKey, ["vision_model"] = VisionModel, ["embedding_model"] = EmbeddingModel,
        ["sam_checkpoint"] = SamCheckpoint, ["sam_config"] = SamConfig, ["device"] = Device,
        ["whisper_model"] = WhisperModel, ["whisper_device"] = WhisperDevice
    };

    public static AppSettings Load(string root)
    {
        AppSettings? value = null;
        var path = Path.Combine(root, "workspace", "settings.json");
        if (File.Exists(path))
        {
            try { value = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(path)); }
            catch (Exception e) when (e is JsonException or IOException or UnauthorizedAccessException) { }
        }
        value ??= new();
        if (string.IsNullOrWhiteSpace(value.Workspace)) value.Workspace = Path.Combine(root, "workspace");
        var python = Path.Combine(root, ".venv", "Scripts", "python.exe");
        if (value.PythonPath == "python" && File.Exists(python)) value.PythonPath = python;
        var checkpoint = Path.Combine(root, "models", "sam2.1_hiera_tiny.pt");
        if (value.SamCheckpoint.Length == 0 && File.Exists(checkpoint)) value.SamCheckpoint = checkpoint;
        return value;
    }

    public void Save(string root)
    {
        var directory = Path.Combine(root, "workspace"); Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, "settings.json");
        File.WriteAllText(path + ".tmp", JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(path + ".tmp", path, true);
    }
}

public sealed class GroupInfo
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("description")] public string Description { get; set; } = "";
    [JsonPropertyName("characters")] public ObservableCollection<CharacterInfo> Characters { get; set; } = [];
    [JsonPropertyName("video_count")] public int VideoCount { get; set; }
    [JsonPropertyName("segment_count")] public int SegmentCount { get; set; }
    public override string ToString() => $"{Name} · {VideoCount} 个素材";
}

public sealed class CharacterInfo : Observable
{
    [JsonPropertyName("id")] public string Id { get; set; } = Guid.NewGuid().ToString("N");
    private string _name = "";
    [JsonPropertyName("name")] public string Name { get => _name; set { if (Set(ref _name, value)) Changed(nameof(DisplayName)); } }
    private List<string> _aliases = [];
    private string? _aliasesText;
    [JsonPropertyName("aliases")] public List<string> Aliases
    {
        get => _aliases;
        set
        {
            _aliases = value ?? [];
            _aliasesText = null;
            Changed();
            Changed(nameof(AliasesText));
        }
    }
    [JsonPropertyName("work_info")] public string WorkInfo { get; set; } = "";
    [JsonPropertyName("identity")] public string Identity { get; set; } = "";
    [JsonPropertyName("appearance")] public string Appearance { get; set; } = "";
    [JsonPropertyName("reference_images")] public ObservableCollection<string> ReferenceImages { get; set; } = [];
    [JsonIgnore] public string DisplayName => string.IsNullOrWhiteSpace(Name) ? "未命名角色 · " + Id[..Math.Min(6, Id.Length)] : Name;
    [JsonIgnore] public string AliasesText
    {
        get => _aliasesText ?? string.Join(Environment.NewLine, Aliases);
        set => Set(ref _aliasesText, value);
    }

    public void ApplyAliasEdits()
    {
        // Preserve the editing buffer, including trailing newlines, even if saving fails.
        _aliases = AliasesText.Split(['\r', '\n', ',', '，', ';', '；'],
            StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).Distinct(StringComparer.Ordinal).ToList();
        Changed(nameof(Aliases));
    }
}

public sealed class CharacterMatchInfo
{
    [JsonPropertyName("character_id")] public string CharacterId { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("confidence")] public string Confidence { get; set; } = "";
    [JsonPropertyName("evidence")] public string Evidence { get; set; } = "";
    [JsonPropertyName("start")] public double Start { get; set; }
    [JsonPropertyName("end")] public double End { get; set; }
}

public sealed class VideoInfo
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("width")] public int Width { get; set; }
    [JsonPropertyName("height")] public int Height { get; set; }
    [JsonPropertyName("fps")] public double Fps { get; set; }
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("subtitle_path")] public string? SubtitlePath { get; set; }
    [JsonPropertyName("subtitle_offset")] public double SubtitleOffset { get; set; }
    [JsonPropertyName("dialogue_cue_count")] public int DialogueCueCount { get; set; }
    [JsonPropertyName("dialogue_semantic_count")] public int DialogueSemanticCount { get; set; }
    [JsonPropertyName("dialogue_ready")] public bool DialogueReady { get; set; }
    public string Details => $"{FormatTime(Duration)} · {Width} × {Height} · {StatusLabel}"
        + (DialogueCueCount > 0 ? $"\n台词 {DialogueCueCount} 条 · {(DialogueReady ? DialogueSemanticCount > 0 ? "可语义检索" : "仅文字" : "需补建")}" : "");
    private string StatusLabel => Status switch { "indexed" => "已索引", "ready" or "not_indexed" or "imported" => "待索引", "error" => "索引失败", "indexing" => "索引中", "missing" => "文件已移动", "changed" => "文件有变化", "outdated" => "需重新索引", _ => Status };
    public static string FormatTime(double seconds) => TimeSpan.FromSeconds(Math.Max(0, seconds)).ToString(seconds >= 3600 ? @"hh\:mm\:ss" : @"mm\:ss", CultureInfo.InvariantCulture);
}

/// <summary>Worker-normalized source frame range; timecode arithmetic belongs to the worker.</summary>
public sealed class CutRangeInfo
{
    [JsonPropertyName("video")] public VideoInfo Video { get; set; } = new();
    [JsonPropertyName("start_frame")] public long StartFrame { get; set; }
    [JsonPropertyName("end_frame")] public long EndFrame { get; set; }
    [JsonPropertyName("start_timecode")] public string StartTimecode { get; set; } = "";
    [JsonPropertyName("end_timecode")] public string EndTimecode { get; set; } = "";
    [JsonPropertyName("frame_count")] public int FrameCount { get; set; }
    [JsonPropertyName("nominal_fps")] public int NominalFps { get; set; }
    [JsonPropertyName("total_frames")] public long TotalFrames { get; set; }
    [JsonPropertyName("total_frames_estimated")] public bool TotalFramesEstimated { get; set; }
}

public sealed class SearchHit
{
    [JsonPropertyName("shot_id")] public string ShotId { get; set; } = "";
    [JsonPropertyName("context_start")] public double ContextStart { get; set; }
    [JsonPropertyName("context_end")] public double ContextEnd { get; set; }
    public bool CanExpandContext => !string.IsNullOrEmpty(ShotId) && ContextStart >= 0 && ContextEnd > ContextStart
        && (ContextStart < Start || ContextEnd > End);
    [JsonPropertyName("video_id")] public string VideoId { get; set; } = "";
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("start")] public double Start { get; set; }
    [JsonPropertyName("end")] public double End { get; set; }
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("score")] public double Score { get; set; }
    [JsonPropertyName("thumbnail")] public string? Thumbnail { get; set; }
    [JsonPropertyName("match_type")] public string MatchType { get; set; } = "";
    [JsonPropertyName("ranking_summary")] public string RankingSummary { get; set; } = "";
    [JsonPropertyName("dialogue_matches")] public List<DialogueMatchInfo> DialogueMatches { get; set; } = [];
    public string DialogueEvidence => string.Join(Environment.NewLine, DialogueMatches.Select(m =>
        $"{m.Start:0.###}—{m.End:0.###} 秒{(m.Approximate ? "（旧片段时码）" : "")} · {(m.Kind == "context" ? "短上下文" : "单条字幕")}：{m.Text}"));
    [JsonPropertyName("character_matches")] public List<CharacterMatchInfo> CharacterMatches { get; set; } = [];
    public string CharacterSummary => CharacterMatches.Count == 0 ? "" : "画面角色：" + string.Join("、",
        CharacterMatches.Select(m => m.Name + (m.Confidence == "medium" ? "（待确认）" : "")).Distinct());
    public string CharacterEvidence => string.Join(Environment.NewLine, CharacterMatches.Select(m =>
        $"{m.Name} · {VideoInfo.FormatTime(m.Start)}—{VideoInfo.FormatTime(m.End)} · {(m.Confidence == "medium" ? "待确认" : "清晰依据")}：{m.Evidence}"));
    public string Timing => $"{VideoInfo.FormatTime(Start)} — {VideoInfo.FormatTime(End)}";
    public string Evidence => $"{(string.IsNullOrWhiteSpace(MatchType) ? "关键词匹配" : MatchType)} · {Score:0.###}";
    public BitmapSource? ThumbnailImage => Images.Load(Thumbnail);
}

public sealed class DialogueMatchInfo
{
    [JsonPropertyName("start")] public double Start { get; set; }
    [JsonPropertyName("end")] public double End { get; set; }
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("approximate")] public bool Approximate { get; set; }
}

public static class Images
{
    public static BitmapSource? Load(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return null;
        try
        {
            var result = new BitmapImage(); result.BeginInit(); result.CacheOption = BitmapCacheOption.OnLoad;
            result.UriSource = new Uri(System.IO.Path.GetFullPath(path)); result.EndInit(); result.Freeze(); return result;
        }
        catch (Exception e) when (e is IOException or NotSupportedException or System.IO.FileFormatException) { return null; }
    }
}

public sealed class ViewState : Observable
{
    public required AppSettings Settings { get; init; }
    public ObservableCollection<GroupInfo> Groups { get; } = [];
    public ObservableCollection<VideoInfo> Videos { get; } = [];
    public ObservableCollection<SearchHit> Results { get; } = [];
    private bool _busy;
    public bool Busy { get => _busy; set { if (Set(ref _busy, value)) Changed(nameof(IsIdle)); } }
    public bool IsIdle => !Busy;
    private string _status = "准备就绪。创建动画分组，再导入素材开始。";
    public string Status { get => _status; set => Set(ref _status, value); }
    private double _progress;
    public double Progress { get => _progress; set => Set(ref _progress, value); }
    private string _libraryHint = "每个动画一个分组，查询范围清晰可控。";
    public string LibraryHint { get => _libraryHint; set => Set(ref _libraryHint, value); }
    private string _libraryBackground = "";
    public string LibraryBackground { get => _libraryBackground; set => Set(ref _libraryBackground, value); }
    private string _resultHint = "输入人物、场景或台词，定位属于你的镜头。";
    public string ResultHint { get => _resultHint; set => Set(ref _resultHint, value); }
    private string _indexHint = "按镜头分段先在本地扫描视频；每段动态选取1～6张图。短镜头增多可能增加请求次数和参考图费用。";
    public string IndexHint { get => _indexHint; set => Set(ref _indexHint, value); }
    private string _cutInfo = "选择视频后，查看所选片段的第一帧。";
    public string CutInfo { get => _cutInfo; set => Set(ref _cutInfo, value); }
    private string _diagnostics = "检查 FFmpeg、Python、分割模型和语音识别环境。";
    public string Diagnostics { get => _diagnostics; set => Set(ref _diagnostics, value); }
    private string _settingsHint = "设置和 API Key 可保存到本地；修改将在下次任务使用。";
    public string SettingsHint { get => _settingsHint; set => Set(ref _settingsHint, value); }
}
