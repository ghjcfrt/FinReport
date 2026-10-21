using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace FinReport.Desktop.ViewModels;

public partial class MainViewModel
{
    [ObservableProperty] private string automaticValidationSummary = "自动校验：需先完成整份原生 PDF 提取。选页和 OCR 结果保留候选，不会通过人员确认升级为自动事实。";
    [ObservableProperty] private string validatedChapterReport = "已核验报告：任务结束后可读取；发布门禁未通过时保留缺口。";

    [RelayCommand]
    private async Task ValidateNativeFactsAsync()
    {
        if (IsBusy || _filingId == 0) return;
        IsBusy = true;
        ErrorMessage = "";
        try
        {
            var filingId = _filingId;
            var result = await api.ValidateNativeFactsAsync(filingId, CancellationToken.None);
            if (_filingId == filingId)
                AutomaticValidationSummary = $"报告 {filingId} 自动校验结果：\n" + JsonSerializer.Serialize(result,
                    new JsonSerializerOptions { WriteIndented = true });
        }
        catch (Exception ex) { ErrorMessage = $"自动校验未完成：{ex.Message}"; }
        finally { IsBusy = false; }
    }

    [RelayCommand]
    private async Task LoadValidatedChapterReportAsync()
    {
        if (IsBusy || !_activeReportChapter || _reportAnalysisRunId == 0) return;
        IsBusy = true;
        ErrorMessage = "";
        try
        {
            var runId = _reportAnalysisRunId;
            var report = await api.GetValidatedChapterReportAsync(runId, CancellationToken.None);
            if (runId == _reportAnalysisRunId) ValidatedChapterReport = $"任务 {runId}\n\n{report}";
        }
        catch (Exception ex)
        {
            ValidatedChapterReport = "当前结果未通过报告发布检查。请查看任务缺口、核验记录和日志。";
            ErrorMessage = ex.Message;
        }
        finally { IsBusy = false; }
    }
}
