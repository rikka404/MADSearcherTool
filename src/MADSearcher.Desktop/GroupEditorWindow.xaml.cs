using Microsoft.Win32;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Media.Imaging;

namespace MADSearcher.Desktop;

public partial class GroupEditorWindow : Window
{
    private readonly GroupInfo _draft;
    private readonly Func<GroupInfo, Task<string?>> _save;
    private bool _saving;

    public GroupEditorWindow(GroupInfo? group, Func<GroupInfo, Task<string?>> save)
    {
        InitializeComponent();
        _draft = JsonSerializer.Deserialize<GroupInfo>(JsonSerializer.Serialize(group ?? new GroupInfo()))!;
        _save = save;
        Title = group == null ? "新建动画分组" : "编辑动画分组";
        DataContext = _draft;
        CharactersList.SelectedItem = _draft.Characters.FirstOrDefault();
    }

    private void CharacterSelected(object sender, SelectionChangedEventArgs e)
    {
        if (CharacterPanel == null) return;
        CharacterPanel.DataContext = CharactersList.SelectedItem;
        CharacterPanel.Visibility = CharactersList.SelectedItem == null ? Visibility.Collapsed : Visibility.Visible;
        EmptyHint.Visibility = CharactersList.SelectedItem == null ? Visibility.Visible : Visibility.Collapsed;
    }

    private void AddCharacter(object sender, RoutedEventArgs e)
    {
        if (_draft.Characters.Count >= 30) { ErrorLabel.Text = "每组最多30个角色。"; return; }
        var card = new CharacterInfo();
        _draft.Characters.Add(card);
        CharactersList.SelectedItem = card;
        CharactersList.ScrollIntoView(card);
        ErrorLabel.Text = "";
    }

    private void RemoveCharacter(object sender, RoutedEventArgs e)
    {
        if (CharactersList.SelectedItem is not CharacterInfo card) return;
        var index = CharactersList.SelectedIndex;
        _draft.Characters.Remove(card);
        CharactersList.SelectedIndex = Math.Min(index, _draft.Characters.Count - 1);
    }

    private void AddReference(object sender, RoutedEventArgs e)
    {
        if (CharactersList.SelectedItem is not CharacterInfo card) return;
        var picker = new OpenFileDialog { Title = "选择角色参考图", Filter = "参考图片|*.png;*.jpg;*.jpeg;*.webp", Multiselect = true };
        if (picker.ShowDialog(this) != true) return;
        var added = picker.FileNames.Except(card.ReferenceImages, StringComparer.OrdinalIgnoreCase).ToArray();
        if (card.ReferenceImages.Count + added.Length > 3) { ErrorLabel.Text = "每个角色最多3张参考图。"; return; }
        foreach (var path in added) card.ReferenceImages.Add(path);
        ErrorLabel.Text = "";
    }

    private void RemoveReference(object sender, RoutedEventArgs e)
    {
        if (CharactersList.SelectedItem is CharacterInfo card && sender is Button { Tag: string path })
            card.ReferenceImages.Remove(path);
    }

    private async void SaveGroup(object sender, RoutedEventArgs e)
    {
        if (_saving) return;
        if (string.IsNullOrWhiteSpace(_draft.Name)) { ErrorLabel.Text = "请填写分组名称，角色字段可全部留空。"; return; }
        foreach (var character in _draft.Characters) character.ApplyAliasEdits();
        if (_draft.Characters.Any(c => c.Aliases.Count > 20 || c.Aliases.Any(a => a.Length > 120)))
        { ErrorLabel.Text = "每个角色最多20个别名，每个最多120字。"; return; }
        var snapshot = JsonSerializer.Deserialize<GroupInfo>(JsonSerializer.Serialize(_draft))!;
        _saving = true;
        Form.IsEnabled = SaveButton.IsEnabled = CancelButton.IsEnabled = false;
        ErrorLabel.Text = "正在保存资料和参考图片…";
        string? error;
        try { error = await _save(snapshot); }
        catch (Exception) { error = "保存未完成，请查看主窗口状态；当前填写内容已保留，可重试。"; }
        finally
        {
            _saving = false;
            Form.IsEnabled = SaveButton.IsEnabled = CancelButton.IsEnabled = true;
        }
        if (error == null) DialogResult = true;
        else ErrorLabel.Text = error;
    }

    private void WindowClosing(object? sender, CancelEventArgs e) { if (_saving) e.Cancel = true; }
}

public sealed class ReferencePreviewConverter : IValueConverter
{
    public object? Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        if (value is not string path || !File.Exists(path)) return null;
        try
        {
            using var stream = File.OpenRead(path);
            var preview = new BitmapImage();
            preview.BeginInit();
            preview.CacheOption = BitmapCacheOption.OnLoad;
            preview.DecodePixelWidth = 256;
            preview.StreamSource = stream;
            preview.EndInit();
            preview.Freeze();
            return preview;
        }
        catch (Exception ex) when (ex is IOException or FileFormatException or NotSupportedException or UnauthorizedAccessException) { return null; }
    }
    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) => Binding.DoNothing;
}
