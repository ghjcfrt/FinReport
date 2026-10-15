using System.Windows;
using CommunityToolkit.Mvvm.Input;

namespace FinReport.Desktop.ViewModels;

public partial class MainViewModel
{
    public bool CanOpenOcrMapping => !IsBusy && _filingId > 0 && SelectedOriginalTable?.Source.OcrCallId is > 0;

    partial void OnSelectedOriginalTableChanged(OriginalTableView? value)
        => OnPropertyChanged(nameof(CanOpenOcrMapping));

    [RelayCommand]
    private async Task OpenOcrMappingAsync()
    {
        if (!CanOpenOcrMapping || SelectedOriginalTable is not { } table) return;
        var mapping = new OcrMappingViewModel(api, _filingId, table.Source);
        var window = new OcrMappingWindow(mapping) { Owner = Application.Current?.Windows.OfType<Window>().FirstOrDefault(w => w.IsActive) };
        window.ShowDialog();
        await RefreshFactsAsync(CancellationToken.None);
        await RefreshAuditTrailAsync(CancellationToken.None);
    }
}
