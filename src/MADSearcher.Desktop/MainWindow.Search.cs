using Microsoft.Win32;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace MADSearcher.Desktop;

/// <summary>Library, indexing, search and clip preview UI. Media and indexing remain worker operations.</summary>
public partial class MainWindow
{
    private bool _loadingGroups;
    private string _libraryWorkspace = "";
    private long _previewVersion;
    private string _previewSource = "";
    private string _previewFile = "";

    private GroupInfo RequireGroup() => GroupBox.SelectedItem as GroupInfo ?? throw new InvalidOperationException("请先新建或选择一个动画分组。");
    private VideoInfo RequireVideo() => VideoList.SelectedItem as VideoInfo ?? throw new InvalidOperationException("请先在素材列表中选择一个视频。");

    private async Task LoadGroups(CancellationToken token, string? selectId = null, bool preserveInputs = true)
    {
        selectId ??= (GroupBox.SelectedItem as GroupInfo)?.Id;
        var data = await Call("group.list", new
        {
        }, token);
        _loadingGroups = true;
        try
        {
            _state.Groups.Clear();
            foreach (var item in Items<GroupInfo>(data, "groups"))
                _state.Groups.Add(item);
            GroupBox.SelectedItem = _state.Groups.FirstOrDefault(g => g.Id == selectId) ?? _state.Groups.FirstOrDefault();
        }
        finally
        {
            _loadingGroups = false;
        }
        _libraryWorkspace = OperationSettings.Workspace;
        await LoadVideos(token, preserveInputs: preserveInputs);
    }

    private async Task LoadVideos(CancellationToken token, string? selectId = null, bool preserveInputs = false)
    {
        if (GroupBox.SelectedItem is not GroupInfo group)
        {
            _state.Videos.Clear();
            _state.LibraryHint = "先创建分组，再批量导入动画素材。";
            _state.LibraryBackground = "";
            LibraryBackgroundScroll.ScrollToTop();
            return;
        }
        var data = await Call("video.list", new
        {
            group_id = group.Id
        }, token);
        var previous = VideoList.SelectedItem as VideoInfo;
        selectId ??= previous?.Id;
        var subtitle = SubtitleBox.Text;
        var offset = OffsetBox.Text;
        _state.Videos.Clear();
        foreach (var item in Items<VideoInfo>(data, "videos"))
            _state.Videos.Add(item);
        VideoList.SelectedItem = _state.Videos.FirstOrDefault(v => v.Id == selectId) ?? _state.Videos.FirstOrDefault();
        if (preserveInputs && previous != null && (VideoList.SelectedItem as VideoInfo)?.Id == previous.Id)
        {
            SubtitleBox.Text = subtitle;
            OffsetBox.Text = offset;
        }
        _state.LibraryHint = $"{group.Name} · {_state.Videos.Count} 个视频 · {group.Characters.Count} 个角色";
        _state.LibraryBackground = group.Description;
        LibraryBackgroundScroll.ScrollToTop();
    }

    private void ClearResults()
    {
        _previewVersion++;
        _state.Results.Clear();
        _state.ResultHint = "输入人物、场景或台词，定位属于你的镜头。";
        _previewSource = "";
        _previewFile = "";
        PreviewPlayer.Close();
        PreviewPlayer.Source = null;
        PreviewStill.Source = null;
        PreviewName.Text = "选择一个检索结果";
        PreviewPathLabel.Text = "预览与导出不会修改原视频。";
        PreviewSeek.Value = 0;
        PreviewSeek.Maximum = 1;
        PlaybackPosition.Text = "00:00 / 00:00";
        PreviewStart.Text = "0";
        PreviewEnd.Text = "8";
    }
    private async void GroupChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingGroups || _state.Busy)
            return;
        ClearResults();
        await Execute("正在读取分组…", LoadVideosWithoutSelection);
    }
    private Task LoadVideosWithoutSelection(CancellationToken token) => LoadVideos(token);
    private void VideoChanged(object sender, SelectionChangedEventArgs e)
    {
        if (VideoList.SelectedItem is not VideoInfo video)
        {
            SubtitleBox.Text = "";
            OffsetBox.Text = "0";
            return;
        }
        SubtitleBox.Text = video.SubtitlePath ?? "";
        OffsetBox.Text = Number(video.SubtitleOffset);
    }

    private void CreateGroup(object sender, RoutedEventArgs e)
    {
        if (_state.Busy) return;
        OpenGroupEditor(null);
    }

    private void EditGroup(object sender, RoutedEventArgs e)
    {
        if (_state.Busy) return;
        if (GroupBox.SelectedItem is not GroupInfo group)
        {
            _state.Status = "请先选择分组。";
            return;
        }
        OpenGroupEditor(group);
    }

    private void OpenGroupEditor(GroupInfo? group)
    {
        var editor = new GroupEditorWindow(group, async draft =>
        {
            string? error = "当前有任务进行，请稍后保存。";
            await Execute("正在保存动画分组与角色资料…", async token =>
            {
                System.Text.Json.JsonElement saved;
                try
                {
                    saved = await Call(group == null ? "group.create" : "group.update", new
                    {
                        group_id = group?.Id,
                        name = draft.Name.Trim(),
                        description = draft.Description.Trim(),
                        characters = draft.Characters
                    }, token);
                }
                catch (Exception ex)
                {
                    error = SafeError(ex.Message);
                    _state.Status = "保存分组失败：" + error;
                    return;
                }
                // Once the transaction succeeds, a listing failure must not retry creation.
                error = null;
                ClearResults();
                await LoadGroups(token, Text(saved, "id"));
                _state.Status = "分组及角色资料已保存。修改资料后请更新对应素材的视觉索引。";
            });
            return error;
        }) { Owner = this };
        editor.ShowDialog();
    }

    private async void DeleteGroup(object sender, RoutedEventArgs e)
    {
        if (_state.Busy)
            return;
        if (GroupBox.SelectedItem is not GroupInfo group)
        {
            _state.Status = "请先选择分组。";
            return;
        }
        if (MessageBox.Show(this, $"从素材库删除“{group.Name}”及其索引？原视频文件会保留。", "删除分组", MessageBoxButton.OKCancel, MessageBoxImage.Warning) != MessageBoxResult.OK)
            return;
        await Execute("正在删除分组记录…", async token => { await Call("group.delete", new { group_id = group.Id }, token); ClearResults(); await LoadGroups(token); });
    }
    private async void ImportVideos(object sender, RoutedEventArgs e)
    {
        if (_state.Busy)
            return;
        if (GroupBox.SelectedItem is not GroupInfo group)
        {
            _state.Status = "请先创建或选择动画分组，再导入素材。";
            Navigate("search");
            return;
        }
        string[] paths;
        if (_smokeImportPath != null)
            paths = [_smokeImportPath];
        else
        {
            var dialog = new OpenFileDialog { Title = "批量导入 MP4 素材", Filter = "MP4 视频|*.mp4", Multiselect = true };
            if (dialog.ShowDialog(this) != true)
                return;
            paths = dialog.FileNames;
        }
        await Execute("正在读取素材信息…", async token =>
        {
            var data = await Call("video.add", new
            {
                group_id = group.Id,
                paths
            }, token);
            await LoadGroups(token, group.Id);
            _state.Status = $"导入已完成。{Warnings(data)}";
        });
    }
    private async void RemoveVideo(object sender, RoutedEventArgs e)
    {
        if (_state.Busy)
            return;
        if (VideoList.SelectedItem is not VideoInfo video)
        {
            _state.Status = "请先选择要移除的素材。";
            return;
        }
        if (MessageBox.Show(this, $"移除“{video.Name}”的库记录与索引？原文件会保留。", "移除素材", MessageBoxButton.OKCancel) != MessageBoxResult.OK)
            return;
        await Execute("正在移除素材记录…", async token => { await Call("video.remove", new { video_id = video.Id }, token); ClearResults(); await LoadGroups(token); });
    }
    private void ChooseSubtitle(object sender, RoutedEventArgs e) => ChooseFile(SubtitleBox, "字幕文件|*.srt;*.ass;*.ssa;*.vtt", "选择当前素材的外挂字幕");
    private async void IndexSelected(object sender, RoutedEventArgs e) => await RunIndex(false);
    private async void IndexAll(object sender, RoutedEventArgs e) => await RunIndex(true);
    private async void IndexDialogue(object sender, RoutedEventArgs e) => await Execute("正在补建独立台词索引…", async token =>
    {
        var group = RequireGroup();
        var video = RequireVideo();
        var subtitle = SubtitleBox.Text.Trim();
        var offset = Parse(OffsetBox.Text, "字幕偏移");
        var semantic = SemanticIndexCheck.IsChecked == true;
        if (Math.Abs(offset) > 86400)
            throw new InvalidOperationException("字幕偏移应在正负86400秒以内。");
        if (subtitle.Length > 0)
            subtitle = RequireFile(subtitle, "字幕");
        if (semantic && string.IsNullOrWhiteSpace(OperationSettings.ApiKey))
            throw new InvalidOperationException("台词语义索引需要在设置中填写OpenAI API Key。");
        _state.IndexHint = semantic ? "正在补齐单句和短上下文向量，复用已保存文本向量；此次不分析画面。" : "正在保存台词文字与时间；勾选字幕语义索引后可补建同义检索能力。";
        var data = await Call("index.dialogue", new { video_id = video.Id, subtitle_path = subtitle, subtitle_offset = offset, semantic }, token);
        var info = data.GetProperty("subtitle_index");
        var cueCount = info.GetProperty("cue_count").GetInt32();
        var semanticCount = info.GetProperty("semantic_count").GetInt32();
        await LoadGroups(token, group.Id, preserveInputs: true);
        ClearResults();
        _state.IndexHint = $"{video.Name}：已保存{cueCount}条台词、{semanticCount}个语义单元。视觉索引保持原样。";
        _state.Status = "台词索引完成。选择“台词”模式搜索；同义查询请勾选语义检索。" + Warnings(data);
    });
    private Task RunIndex(bool all) => Execute("正在准备素材索引…", async token =>
    {
        RequireGroup();
        var selected = RequireVideo();
        var offset = Parse(OffsetBox.Text, "字幕偏移");
        if (Math.Abs(offset) > 86400)
            throw new InvalidOperationException("字幕偏移应在正负 86400 秒以内。");
        var visual = VisualCheck.IsChecked == true;
        var semantic = SemanticIndexCheck.IsChecked == true;
        var transcribe = AsrCheck.IsChecked == true;
        var language = (LanguageBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "auto";
        var segmentation = SegmentationBox.SelectedValue?.ToString() ?? "shot";
        var sensitivity = SceneSensitivityBox.SelectedValue?.ToString() ?? "medium";
        var shotSeconds = segmentation == "shot" ? Parse(ShotMaxSecondsBox.Text, "长镜头上限") : 20;
        var fixedSeconds = segmentation == "fixed" ? Parse(FixedSecondsBox.Text, "固定分段时长") : 8;
        if (shotSeconds < 2 || shotSeconds > 60 || fixedSeconds < 2 || fixedSeconds > 60)
            throw new InvalidOperationException("分段时长应在2～60秒以内。");
        if ((visual || semantic) && string.IsNullOrWhiteSpace(OperationSettings.ApiKey))
            throw new InvalidOperationException("AI 画面或语义索引需要 OpenAI API Key，请先在设置中填写。纯字幕关键词索引无需密钥。");
        var subtitle = SubtitleBox.Text.Trim();
        if (subtitle.Length > 0)
            subtitle = RequireFile(subtitle, "字幕");
        var targets = all ? _state.Videos.ToArray() : [selected];
        var warnings = new List<string>();
        var logDirectory = Path.Combine(OperationSettings.Workspace, "logs", "index");
        int total = 0;
        int shots = 0, requests = 0, sampleImages = 0, referenceImages = 0, subtitleCues = 0;
        _state.IndexHint = "正在准备索引。检测和采样完成后，进度栏会显示本次预计视觉请求与图片数量。";
        foreach (var video in targets)
        {
            token.ThrowIfCancellationRequested();
            var data = await Call("index.run", new
            {
                video_id = video.Id,
                subtitle_path = video.Id == selected.Id ? subtitle : video.SubtitlePath ?? "",
                subtitle_offset = video.Id == selected.Id ? offset : video.SubtitleOffset,
                transcribe,
                language,
                visual,
                semantic,
                segmentation,
                scene_sensitivity = sensitivity,
                shot_max_seconds = shotSeconds,
                segment_seconds = fixedSeconds
            }, token);
            total += data.GetProperty("segment_count").GetInt32();
            if (data.TryGetProperty("subtitle_index", out var subtitleIndex))
                subtitleCues += subtitleIndex.GetProperty("cue_count").GetInt32();
            if (data.TryGetProperty("shot_plan", out var plan))
                shots += plan.GetProperty("shot_count").GetInt32();
            if (data.TryGetProperty("image_estimate", out var estimate))
            {
                requests += estimate.GetProperty("pending_visual_requests").GetInt32();
                sampleImages += estimate.GetProperty("pending_sample_images").GetInt32();
                referenceImages += estimate.GetProperty("pending_reference_images").GetInt32();
            }
            if (Warnings(data).Length > 0)
                warnings.Add(video.Name + "：" + Warnings(data));
        }
        await LoadGroups(token, preserveInputs: true);
        ClearResults();
        _state.IndexHint = $"本次完成：{shots}个镜头、{total}个片段、{subtitleCues}条独立台词；视觉分析{requests}段，采样图{sampleImages}张、参考图累计{referenceImages}张。重试次数和实际token见日志。";
        _state.Status = $"已完成 {targets.Length} 个素材、{total} 个片段的索引。耗时日志：{logDirectory}。" + string.Join("；", warnings);
    });

    private void OpenIndexLogs(object sender, RoutedEventArgs e)
    {
        try
        {
            var directory = Path.Combine(OperationSettings.Workspace, "logs", "index");
            Directory.CreateDirectory(directory);
            OpenPath(directory);
        }
        catch (Exception ex) { _state.Status = "无法打开索引日志：" + SafeError(ex.Message); }
    }

    private async void SearchClicked(object sender, RoutedEventArgs e) => await SearchAsync();
    private async void QueryKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
        {
            e.Handled = true;
            await SearchAsync();
        }
    }
    private Task SearchAsync() => Execute("正在查找匹配镜头…", async token =>
    {
        var group = RequireGroup();
        var query = QueryBox.Text.Trim();
        var mode = SearchModeBox.SelectedValue?.ToString() ?? "combined";
        if (query.Length == 0)
            throw new InvalidOperationException("请输入要查找的人物、场景、情节或台词。");
        var data = await Call("search.run", new
        {
            group_id = group.Id,
            query,
            mode,
            limit = 30,
            semantic = SemanticCheck.IsChecked == true
        }, token);
        // An empty response must also release the previous selection and export source.
        ClearResults();
        foreach (var item in Items<SearchHit>(data, "results"))
            _state.Results.Add(item);
        var modeLabel = mode == "dialogue" ? "台词" : mode == "scene" ? "画面情节" : "综合";
        _state.ResultHint = _state.Results.Count == 0 ? $"“{query}”没有匹配片段。台词查询请先补建台词索引，同义查询需勾选语义检索。" : $"{_state.Results.Count} 个匹配 · {modeLabel} · {group.Name} · {query}";
        _state.ResultHint += QueryDescription(data);
        _state.Status = $"检索完成。{Warnings(data)}";
        if (_state.Results.Count > 0)
            ResultsList.SelectedIndex = 0;
    });
    private static string QueryDescription(System.Text.Json.JsonElement data)
    {
        if (!data.TryGetProperty("query_analysis", out var analysis)
            || !analysis.TryGetProperty("characters", out var characters))
            return "";
        var names = characters.EnumerateArray().Select(item =>
            string.Join(" / ", item.GetProperty("names").EnumerateArray().Select(n => n.GetString()))
            + (item.GetProperty("ambiguous").GetBoolean() ? "（有歧义）" : "")).ToArray();
        if (names.Length == 0)
            return "";
        var message = "\n识别角色：" + string.Join("、", names.Take(6)) + (names.Length > 6 ? "等" : "");
        if (analysis.TryGetProperty("variants", out var variants) && variants.TryGetProperty("event", out var eventValue))
        {
            var eventQuery = eventValue.GetString() ?? "";
            message += " · 情节：" + (eventQuery.Length > 70 ? eventQuery[..70] + "…" : eventQuery);
            if (analysis.TryGetProperty("semantic_variants", out var semanticVariants)
                && semanticVariants.EnumerateArray().Any(v => v.GetString() == "appearance"))
                message += " · 已结合角色外观资料";
        }
        else
            message += " · 优先出镜证据，文字提及单独标记";
        return message;
    }
    private void ResultChanged(object sender, SelectionChangedEventArgs e)
    {
        _previewVersion++;
        if (ResultsList.SelectedItem is not SearchHit hit)
            return;
        _previewSource = hit.Path;
        _previewFile = "";
        PreviewPlayer.Close();
        PreviewPlayer.Source = null;
        PreviewStill.Source = hit.ThumbnailImage;
        PreviewStart.Text = Number(hit.Start);
        PreviewEnd.Text = Number(hit.End);
        PreviewName.Text = hit.Name + "\n" + hit.Timing;
        PreviewPathLabel.Text = hit.Path;
    }
    private void PreviewInputsChanged(object sender, TextChangedEventArgs e) => _previewVersion++;
    private void ExpandShotContext(object sender, RoutedEventArgs e)
    {
        if (ResultsList.SelectedItem is not SearchHit hit || !hit.CanExpandContext)
            return;
        if (hit.ContextEnd - hit.ContextStart > 300)
        {
            _state.Status = "前后镜头的总范围超过5分钟，请手动调整起止时间。";
            return;
        }
        ApplyHitPreviewRange(hit, hit.ContextStart, hit.ContextEnd);
        _state.Status = "已扩展到前后各一个镜头。点击生成预览查看完整上下文；人物依据仍对应原命中片段。";
    }
    private void RestoreHitRange(object sender, RoutedEventArgs e)
    {
        if (ResultsList.SelectedItem is SearchHit hit)
            ApplyHitPreviewRange(hit, hit.Start, hit.End);
    }
    private void ApplyHitPreviewRange(SearchHit hit, double start, double end)
    {
        _previewVersion++;
        PreviewPlayer.Close();
        PreviewPlayer.Source = null;
        _previewFile = "";
        PreviewStill.Source = hit.ThumbnailImage;
        PreviewStart.Text = start.ToString("0.######", System.Globalization.CultureInfo.InvariantCulture);
        PreviewEnd.Text = end.ToString("0.######", System.Globalization.CultureInfo.InvariantCulture);
    }
    private async Task<(string Path, double Start, double End)> PreviewRange(CancellationToken token)
    {
        var path = RequireFile(_previewSource, "预览源视频");
        var selectedRange = Range(PreviewStart, PreviewEnd);
        var info = await Call("video.probe", new
        {
            path
        }, token);
        var range = Range(selectedRange.Start, selectedRange.End, info.GetProperty("duration").GetDouble());
        return (path, range.Start, range.End);
    }
    private async void MakePreview(object sender, RoutedEventArgs e) => await Execute("正在生成预览…", async token =>
    {
        var version = _previewVersion;
        var range = await PreviewRange(token);
        if (range.End - range.Start > 300)
            throw new InvalidOperationException("预览最长 300 秒，请缩短范围。");
        var data = await Call("preview.make", new
        {
            path = range.Path,
            start = range.Start,
            end = range.End
        }, token);
        if (version != _previewVersion)
        {
            _state.Status = "预览已生成，但当前选择或时间范围已更改。请为新选择生成预览。";
            return;
        }
        _previewFile = Text(data, "path");
        PreviewStill.Source = null;
        PreviewPlayer.Source = new Uri(_previewFile);
        if (SearchPage.Visibility == Visibility.Visible)
            PreviewPlayer.Play();
        _state.Status = "预览已生成。可调整起止后再次预览，或导出片段。";
    });
    private async void ExportClip(object sender, RoutedEventArgs e)
    {
        if (_state.Busy)
            return;
        if (string.IsNullOrEmpty(_previewSource))
        {
            _state.Status = "请先选择一个检索结果。";
            return;
        }
        var output = _smokeExportDirectory ?? ChooseDirectory("选择 MP4 输出目录", Path.Combine(_state.Settings.Workspace, "exports"));
        if (output == null)
            return;
        await Execute("正在导出 MP4…", async token =>
        {
            var version = _previewVersion;
            var range = await PreviewRange(token);
            var data = await Call("clip.export", new
            {
                path = range.Path,
                start = range.Start,
                end = range.End,
                output_dir = output
            }, token);
            if (version == _previewVersion)
                PreviewPathLabel.Text = "已导出：" + Text(data, "path");
            _state.Status = "MP4 已导出到 " + Text(data, "path");
        });
    }
    private async void SendToCutout(object sender, RoutedEventArgs e) => await Execute("正在准备抠像片段…", async token =>
    {
        var path = RequireFile(_previewSource, "预览源视频");
        var range = Range(PreviewStart, PreviewEnd, maxLength: 120);
        CutPathBox.Text = path;
        CutStartBox.Text = Number(range.Start);
        CutEndBox.Text = Number(range.End);
        Navigate("cutout");
        await LoadCutFrame(token);
        _state.Status = "片段已送入抠像。指定首帧主体后即可开始。";
    });
    private void PreviewOpened(object sender, RoutedEventArgs e)
    {
        if (PreviewPlayer.NaturalDuration.HasTimeSpan)
            PreviewSeek.Maximum = PreviewPlayer.NaturalDuration.TimeSpan.TotalSeconds;
    }
    private void PreviewEnded(object sender, RoutedEventArgs e) => PreviewPlayer.Pause();
    private void PlayPreview(object sender, RoutedEventArgs e)
    {
        if (PreviewPlayer.Source != null)
            PreviewPlayer.Play();
        else
            _state.Status = "请先生成预览。";
    }
    private void PausePreview(object sender, RoutedEventArgs e) => PreviewPlayer.Pause();
    private void RestartPreview(object sender, RoutedEventArgs e)
    {
        PreviewPlayer.Position = TimeSpan.Zero;
        if (PreviewPlayer.Source != null)
            PreviewPlayer.Play();
    }
    private void SeekPreview(object sender, MouseButtonEventArgs e)
    {
        if (PreviewPlayer.Source != null)
            PreviewPlayer.Position = TimeSpan.FromSeconds(PreviewSeek.Value);
    }
    private void OpenPreview(object sender, RoutedEventArgs e) => OpenPath(_previewFile);
}
