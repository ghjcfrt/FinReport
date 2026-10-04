using System.IO;
using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FinReport.Desktop.Services;
using Microsoft.Win32;

namespace FinReport.Desktop.ViewModels;

public partial class MainViewModel(FinReportApiClient api) : ObservableObject
{
    [ObservableProperty] private string selectedFilePath = "未选择文件";
    [ObservableProperty] private string stockCode = "";
    [ObservableProperty] private string companyName = "";
    [ObservableProperty] private string statusMessage = "等待上传";
    [ObservableProperty] private string errorMessage = "";
    [ObservableProperty] private int progress;
    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private string modelSettingsMessage = "模型设置：加载中…";
    [ObservableProperty] private bool canStartAnalysis;
    [ObservableProperty] private string reviewSummary = "待复核：未加载";
    [ObservableProperty] private string metricsSummary = "指标：未计算";
    [ObservableProperty] private string exportSummary = "导出：未生成";
    [ObservableProperty] private string auditSummary = "审计记录：请先上传财报。";
    [ObservableProperty] private string reproducibilitySummary = "复现条件：尚未生成文件指纹。";
    public ObservableCollection<AuditEventItem> AuditEvents { get; } = [];
    [ObservableProperty] private string analysisProvider = "openai";
    [ObservableProperty] private string analysisBaseUrl = "";
    [ObservableProperty] private string analysisApiKey = "";
    [ObservableProperty] private string analysisModel = "";
    [ObservableProperty] private string ocrProvider = "openai";
    [ObservableProperty] private string ocrBaseUrl = "";
    [ObservableProperty] private string ocrApiKey = "";
    [ObservableProperty] private string ocrModel = "";
    [ObservableProperty] private bool isDiscoveringAnalysisModels;
    [ObservableProperty] private bool isDiscoveringOcrModels;
    public ObservableCollection<string> AnalysisModels { get; } = [];
    public ObservableCollection<string> OcrModels { get; } = [];
    private long _filingId;
    private long _jobId;

    public bool CanSubmit => !IsBusy && File.Exists(SelectedFilePath) && !string.IsNullOrWhiteSpace(StockCode) && !string.IsNullOrWhiteSpace(CompanyName);

    partial void OnSelectedFilePathChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnStockCodeChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnCompanyNameChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnIsBusyChanged(bool value) => OnPropertyChanged(nameof(CanSubmit));

    public async Task LoadModelSettingsAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            var value = await api.GetModelSettingsAsync(cancellationToken);
            AnalysisProvider = value.Analysis.Provider; AnalysisBaseUrl = value.Analysis.BaseUrl ?? ""; AnalysisModel = value.Analysis.Model ?? "";
            OcrProvider = value.Ocr.Provider; OcrBaseUrl = value.Ocr.BaseUrl ?? ""; OcrModel = value.Ocr.Model ?? "";
            ModelSettingsMessage = $"分析：{value.Analysis.Provider}/{value.Analysis.Model ?? "未配置"}（{(value.Analysis.Configured ? "已配置" : "未配置")}） | OCR：{value.Ocr.Provider}/{value.Ocr.Model ?? "未配置"}（{(value.Ocr.Configured ? "已配置" : "未配置")}）";
        }
        catch (Exception ex) { ModelSettingsMessage = $"模型设置读取失败：{ex.Message}"; }
    }

    [RelayCommand]
    private async Task SaveModelSettingsAsync()
    {
        try
        {
            var value = await api.UpdateModelSettingsAsync(new ModelSettingsUpdate(new ModelEndpointUpdate(AnalysisProvider, AnalysisBaseUrl, string.IsNullOrWhiteSpace(AnalysisApiKey) ? null : AnalysisApiKey, AnalysisModel), new ModelEndpointUpdate(OcrProvider, OcrBaseUrl, string.IsNullOrWhiteSpace(OcrApiKey) ? null : OcrApiKey, OcrModel)), CancellationToken.None);
            AnalysisApiKey = ""; OcrApiKey = "";
            ModelSettingsMessage = $"已保存。分析：{value.Analysis.Provider}/{value.Analysis.Model}；OCR：{value.Ocr.Provider}/{value.Ocr.Model}";
        }
        catch (Exception ex) { ErrorMessage = $"模型设置保存失败：{ex.Message}"; }
    }

    [RelayCommand]
    private async Task DiscoverAnalysisModelsAsync()
    {
        await DiscoverModelsAsync("analysis", AnalysisProvider, AnalysisBaseUrl, AnalysisApiKey, AnalysisModels, value => AnalysisModel = value, value => IsDiscoveringAnalysisModels = value);
    }

    [RelayCommand]
    private async Task DiscoverOcrModelsAsync()
    {
        await DiscoverModelsAsync("ocr", OcrProvider, OcrBaseUrl, OcrApiKey, OcrModels, value => OcrModel = value, value => IsDiscoveringOcrModels = value);
    }

    private async Task DiscoverModelsAsync(string stage, string provider, string baseUrl, string apiKey, ObservableCollection<string> destination, Action<string> selectModel, Action<bool> setBusy)
    {
        if (string.IsNullOrWhiteSpace(baseUrl))
        {
            ErrorMessage = "请先填写 API Base URL，再获取模型列表。";
            return;
        }
        setBusy(true);
        ErrorMessage = "";
        try
        {
            var models = await api.DiscoverModelsAsync(stage, provider, baseUrl, string.IsNullOrWhiteSpace(apiKey) ? null : apiKey, CancellationToken.None);
            destination.Clear();
            foreach (var model in models) destination.Add(model);
            if (models.Length == 0)
            {
                ModelSettingsMessage = "未从当前 API 连接中获取到模型。请检查 API 规范、Base URL 与密钥。";
                return;
            }
            if (!models.Contains(stage == "analysis" ? AnalysisModel : OcrModel)) selectModel(models[0]);
            ModelSettingsMessage = $"已获取 {models.Length} 个{(stage == "analysis" ? "结果分析" : " OCR")}模型，请从列表中选择。";
        }
        catch (Exception ex) { ErrorMessage = $"获取模型列表失败：{ex.Message}"; }
        finally { setBusy(false); }
    }

    [RelayCommand]
    private void SelectFile()
    {
        var dialog = new OpenFileDialog { Filter = "PDF 文件 (*.pdf)|*.pdf", Multiselect = false };
        if (dialog.ShowDialog() == true)
        {
            SelectedFilePath = dialog.FileName;
            ErrorMessage = "";
        }
    }

    [RelayCommand]
    private async Task UploadAsync()
    {
        if (!CanSubmit) return;
        IsBusy = true;
        ErrorMessage = "";
        Progress = 0;
        try
        {
            StatusMessage = "正在上传…";
            using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(5));
            var filingId = await api.UploadAsync(SelectedFilePath, StockCode, CompanyName, cancellation.Token);
            _filingId = filingId;
            var jobId = await api.StartJobAsync(filingId, cancellation.Token);
            _jobId = jobId;
            while (true)
            {
                var job = await api.GetJobAsync(jobId, cancellation.Token);
                Progress = job.Progress;
                StatusMessage = $"阶段：{job.Stage}；状态：{job.Status}；待复核：{job.ReviewCount}";
                if (job.Status is "completed" or "failed")
                {
                    ErrorMessage = job.ErrorDetail ?? "";
                    CanStartAnalysis = job.Status == "completed";
                    if (CanStartAnalysis)
                    {
                        var facts = await api.GetFactsAsync(_filingId, cancellation.Token);
                        ReviewSummary = $"事实：{facts.Length} 条；待复核：{facts.Count(item => item.ReviewStatus == "needs_review")} 条";
                        var metrics = await api.CalculateMetricsAsync(_filingId, cancellation.Token);
                        MetricsSummary = $"指标：{metrics.Length} 条；已计算：{metrics.Count(item => item.ApplicabilityStatus == "calculated")} 条";
                    }
                    await RefreshAuditTrailAsync(cancellation.Token);
                    break;
                }
                await Task.Delay(TimeSpan.FromSeconds(1), cancellation.Token);
            }
        }
        catch (OperationCanceledException) { ErrorMessage = "请求已取消或超时。"; }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { IsBusy = false; }
    }

    [RelayCommand]
    private async Task StartAnalysisAsync()
    {
        if (!CanStartAnalysis || _filingId == 0) return;
        try
        {
            StatusMessage = $"分析任务已创建：{await api.StartAnalysisAsync(_filingId, CancellationToken.None)}";
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
    }

    [RelayCommand]
    private async Task RetryAsync()
    {
        if (_jobId == 0) return;
        try { await api.RetryJobAsync(_jobId, CancellationToken.None); StatusMessage = "已重新提交任务"; }
        catch (Exception ex) { ErrorMessage = ex.Message; }
    }

    [RelayCommand]
    private async Task CancelAsync()
    {
        if (_jobId == 0) return;
        try { await api.CancelJobAsync(_jobId, CancellationToken.None); StatusMessage = "已请求取消任务"; }
        catch (Exception ex) { ErrorMessage = ex.Message; }
    }

    [RelayCommand]
    private async Task ExportAsync()
    {
        if (_filingId == 0) return;
        try
        {
            ExportSummary = $"导出：{await api.CreateExportAsync(_filingId, "html", CancellationToken.None)}";
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
    }

    [RelayCommand]
    private async Task RefreshAuditTrailAsync()
    {
        if (_filingId == 0)
        {
            ErrorMessage = "请先上传财报，再查看执行审计记录。";
            return;
        }
        await RefreshAuditTrailAsync(CancellationToken.None);
    }

    private async Task RefreshAuditTrailAsync(CancellationToken cancellationToken)
    {
        try
        {
            var trail = await api.GetAuditTrailAsync(_filingId, cancellationToken);
            AuditEvents.Clear();
            foreach (var item in trail.Events) AuditEvents.Add(item);
            AuditSummary = $"审计记录：{trail.Events.Length} 条，按发生时间排序；每条均包含输入/输出指纹或来源引用。";
            var reproduction = trail.Reproduction;
            ReproducibilitySummary = $"文件 SHA-256：{reproduction.FileSha256}；报告期：{reproduction.ReportingPeriod}；口径：{reproduction.ReportType}/{reproduction.PeriodBasis}";
        }
        catch (Exception ex) { ErrorMessage = $"读取执行审计记录失败：{ex.Message}"; }
    }
}
