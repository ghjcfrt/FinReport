using System.IO;
using System.Globalization;
using System.Text.Json;
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
    [ObservableProperty] private int reportYear = 2025;
    [ObservableProperty] private string statusMessage = "等待上传";
    [ObservableProperty] private string errorMessage = "";
    [ObservableProperty] private int progress;
    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private string modelSettingsMessage = "模型设置：加载中…";
    [ObservableProperty] private string scannerHealthMessage = "病毒库状态：加载中…";
    [ObservableProperty] private bool scannerReady;
    [ObservableProperty] private bool isUpdatingScanner;
    public bool CanUpdateScanner => !IsUpdatingScanner;
    partial void OnIsUpdatingScannerChanged(bool value) => OnPropertyChanged(nameof(CanUpdateScanner));
    [ObservableProperty] private bool canStartAnalysis;
    [ObservableProperty] private string reviewerId = "";
    [ObservableProperty] private string reviewReason = "";
    [ObservableProperty] private string correctedAmount = "";
    [ObservableProperty] private FactItem? selectedFact;
    [ObservableProperty] private string validationMessage = "校验：未执行";
    [ObservableProperty] private string analysisSummary = "分析：未生成";
    public ObservableCollection<FactItem> Facts { get; } = [];
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

    public bool CanSubmit => !IsBusy && File.Exists(SelectedFilePath) && !string.IsNullOrWhiteSpace(StockCode) && !string.IsNullOrWhiteSpace(CompanyName) && ReportYear is >= 1900 and <= 2100;

    partial void OnSelectedFilePathChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnStockCodeChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnCompanyNameChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnReportYearChanged(int value) => OnPropertyChanged(nameof(CanSubmit));
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
        await RefreshScannerHealthAsync(cancellationToken);
    }

    [RelayCommand]
    private async Task RefreshScannerHealthAsync()
        => await RefreshScannerHealthAsync(CancellationToken.None);

    private async Task RefreshScannerHealthAsync(CancellationToken cancellationToken)
    {
        try
        {
            var health = await api.GetScannerHealthAsync(cancellationToken);
            ApplyScannerHealth(health);
        }
        catch (Exception ex)
        {
            ScannerReady = false;
            ScannerHealthMessage = $"病毒库状态读取失败：{ex.Message}";
        }
    }

    [RelayCommand]
    private async Task UpdateScannerDatabaseAsync()
    {
        if (IsUpdatingScanner) return;
        IsUpdatingScanner = true;
        ErrorMessage = "";
        ScannerHealthMessage = "正在联网检查并更新病毒库，请稍候…";
        try
        {
            using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(10));
            var health = await api.UpdateScannerDatabaseAsync(cancellation.Token);
            ApplyScannerHealth(health);
            while (health.State is "queued" or "running")
            {
                await Task.Delay(TimeSpan.FromSeconds(2), cancellation.Token);
                health = await api.GetScannerHealthAsync(cancellation.Token);
                ApplyScannerHealth(health);
            }
            if (health.State == "succeeded")
            {
                StatusMessage = "ClamAV 病毒库更新完成，已通过校验并激活。";
            }
            else if (health.State == "failed")
            {
                throw new InvalidOperationException(health.UpdateError ?? "AV_UPDATE_FAILED");
            }
        }
        catch (OperationCanceledException) { ScannerHealthMessage = "病毒库更新超时或已取消。"; }
        catch (Exception ex) { ScannerReady = false; ScannerHealthMessage = $"病毒库更新失败：{ex.Message}"; ErrorMessage = ScannerHealthMessage; }
        finally { IsUpdatingScanner = false; }
    }

    private void ApplyScannerHealth(ScannerHealth health)
    {
        ScannerReady = health.Ready && string.IsNullOrWhiteSpace(health.UpdateError);
        var state = ScannerReady ? "正常" : "不可用";
        var version = string.IsNullOrWhiteSpace(health.DatabaseVersion) ? "版本未知" : $"版本 {health.DatabaseVersion}";
        var age = health.DatabaseAgeSeconds is long seconds ? $"，已更新 {FormatAge(seconds)}" : "";
        var detail = string.IsNullOrWhiteSpace(health.UpdateError) ? "" : $"；错误：{health.UpdateError}";
        ScannerHealthMessage = $"病毒库状态：{state}（{version}{age}；队列 {health.QueueDepth}；扫描中 {health.ActiveScans}）{detail}";
    }

    private static string FormatAge(long seconds)
    {
        if (seconds < 3600) return $"{Math.Max(1, seconds / 60)} 分钟前";
        if (seconds < 86400) return $"{seconds / 3600} 小时前";
        return $"{seconds / 86400} 天前";
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
    private void OpenSourcePdf()
    {
        if (!File.Exists(SelectedFilePath)) return;
        try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(SelectedFilePath) { UseShellExecute = true }); }
        catch (Exception ex) { ErrorMessage = $"打开原始 PDF 失败：{ex.Message}"; }
    }

    [RelayCommand]
    private async Task UploadAsync()
    {
        if (!CanSubmit) return;
        IsBusy = true;
        ErrorMessage = "";
        Progress = 0;
        CanStartAnalysis = false;
        Facts.Clear();
        MetricsSummary = "指标：等待事实复核";
        AnalysisSummary = "分析：未生成";
        ExportSummary = "导出：未生成";
        try
        {
            StatusMessage = "正在上传…";
            using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(15));
            var filingId = await api.UploadAsync(SelectedFilePath, StockCode, CompanyName, ReportYear, cancellation.Token);
            _filingId = filingId;
            var jobId = await api.StartJobAsync(filingId, cancellation.Token);
            _jobId = jobId;
            while (true)
            {
                var job = await api.GetJobAsync(jobId, cancellation.Token);
                Progress = job.Progress;
                StatusMessage = $"阶段：{job.Stage}；状态：{job.Status}；待复核：{job.ReviewCount}";
                if (job.Status is "completed" or "failed" or "cancelled")
                {
                    ErrorMessage = job.ErrorDetail ?? "";
                    if (job.Status == "completed")
                    {
                        await RefreshFactsAsync(cancellation.Token);
                        StatusMessage = "解析完成，请逐项核对原文，由两名独立复核人确认后计算指标。";
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
            CanStartAnalysis = false;
            using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(5));
            var runId = await api.StartAnalysisAsync(_filingId, cancellation.Token);
            StatusMessage = $"分析任务：{runId}，正在生成并校验…";
            while (true)
            {
                var run = await api.GetAnalysisAsync(runId, cancellation.Token);
                if (run.Status == "completed")
                {
                    AnalysisSummary = run.Output is JsonElement output ? JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true, Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping }) : "分析未返回内容";
                    StatusMessage = "分析完成，数值与公式已校验；推断仍需人工复核。";
                    break;
                }
                if (run.Status is "failed" or "invalidated") throw new InvalidOperationException(run.FailureReason ?? run.Status);
                await Task.Delay(1000, cancellation.Token);
            }
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { CanStartAnalysis = true; }
    }

    [RelayCommand]
    private async Task RefreshFactsAsync() => await RefreshFactsAsync(CancellationToken.None);

    private async Task RefreshFactsAsync(CancellationToken cancellationToken)
    {
        if (_filingId == 0) return;
        var selectedId = SelectedFact?.Id;
        var facts = await api.GetFactsAsync(_filingId, cancellationToken);
        Facts.Clear();
        foreach (var fact in facts) Facts.Add(fact);
        SelectedFact = Facts.FirstOrDefault(f => f.Id == selectedId);
        ReviewSummary = $"事实：{facts.Length} 条；待复核：{facts.Count(f => f.ReviewStatus != "verified")} 条";
        var validation = await api.GetValidationsAsync(_filingId, cancellationToken);
        ValidationMessage = $"财报状态：{validation.FilingStatus}；" + string.Join("；", validation.Results.Select(r => $"{r.RuleCode}: {r.Status}（{r.ActualValue}）"));
        CanStartAnalysis = false;
    }

    [RelayCommand] private async Task ConfirmFactAsync() => await ReviewSelectedAsync("confirmed");
    [RelayCommand] private async Task CorrectFactAsync() => await ReviewSelectedAsync("corrected");
    [RelayCommand] private async Task RejectFactAsync() => await ReviewSelectedAsync("rejected");

    private async Task ReviewSelectedAsync(string decision)
    {
        if (SelectedFact is null || string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(ReviewReason))
        { ErrorMessage = "请选择事实，并填写复核人标识与核对说明。"; return; }
        decimal? corrected = null;
        if (decision == "corrected")
        {
            if (!decimal.TryParse(CorrectedAmount, NumberStyles.Number, CultureInfo.InvariantCulture, out var amount))
            { ErrorMessage = "订正金额请填写人民币元。"; return; }
            corrected = amount;
        }
        try
        {
            await api.ReviewFactAsync(SelectedFact.Id, ReviewerId.Trim(), decision, ReviewReason, corrected, CancellationToken.None);
            ErrorMessage = "";
            await RefreshFactsAsync(CancellationToken.None);
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
    }

    [RelayCommand]
    private async Task CalculateMetricsAsync()
    {
        if (_filingId == 0) return;
        try
        {
            var metrics = await api.CalculateMetricsAsync(_filingId, CancellationToken.None);
            MetricsSummary = $"指标：{metrics.Length} 条；" + string.Join("；", metrics.Select(m => $"{m.MetricCode}={m.Value?.ToString() ?? m.ApplicabilityStatus}"));
            CanStartAnalysis = metrics.Any(m => m.ApplicabilityStatus == "calculated");
            ErrorMessage = "";
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { CanStartAnalysis = false; ErrorMessage = ex.Message; }
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
            var url = await api.CreateExportAsync(_filingId, "html", CancellationToken.None);
            if (url is null) throw new InvalidOperationException("导出未返回下载地址。");
            var dialog = new SaveFileDialog { Filter = "HTML 报告 (*.html)|*.html", FileName = $"财报分析-{StockCode}-{ReportYear}.html" };
            if (dialog.ShowDialog() == true)
            {
                await api.DownloadExportAsync(url, dialog.FileName, CancellationToken.None);
                ExportSummary = $"已保存：{dialog.FileName}";
            }
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
