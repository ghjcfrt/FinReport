using System.IO;
using System.Globalization;
using System.Text.Json;
using System.Collections.ObjectModel;
using System.Windows;
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
    [ObservableProperty] private string selectedExtractionMethod = "pdf_text";
    [ObservableProperty] private string? activeExtractionMethod;
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
    [ObservableProperty] private FactRowView? selectedFact;
    [ObservableProperty] private string validationMessage = "校验：未执行";
    [ObservableProperty] private string analysisSummary = "分析：未生成";
    public ObservableCollection<FactRowView> Facts { get; } = [];
    public ObservableCollection<int> OriginalTablePages { get; } = [];
    public ObservableCollection<OriginalTableView> OriginalTables { get; } = [];
    [ObservableProperty] private int? selectedTablePage;
    [ObservableProperty] private OriginalTableView? selectedOriginalTable;
    [ObservableProperty] private string originalTableSummary = "原表：请先上传并解析财报。";
    private OriginalTableView[] _originalTables = [];
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
    [ObservableProperty] private string ocrProvider = "baidu-unlimited-ocr";
    [ObservableProperty] private string ocrBaseUrl = "";
    [ObservableProperty] private string ocrApiKey = "";
    [ObservableProperty] private string ocrModel = "";
    [ObservableProperty] private bool isDiscoveringAnalysisModels;
    [ObservableProperty] private bool isDiscoveringOcrModels;
    public ObservableCollection<string> AnalysisModels { get; } = [];
    public ObservableCollection<string> OcrModels { get; } = [];
    private long _filingId;
    private long _jobId;
    private CancellationTokenSource? _extractionCancellation;

    public bool CanSubmit => !IsBusy && File.Exists(SelectedFilePath) && !string.IsNullOrWhiteSpace(StockCode) && !string.IsNullOrWhiteSpace(CompanyName) && ReportYear is >= 1900 and <= 2100;

    public bool CanChooseExtractionMethod => !IsBusy;
    public string ExtractionMethodHint => SelectedExtractionMethod == "ocr"
        ? "OCR 生成候选表；请在原表页打开人工映射，框选原图并登记科目、单位与期间，再完成事实审核。"
        : "直接提取 PDF 文字与位置证据，生成待审核事实；人工核对确认后才可严格计算与分析。";
    public string ActiveExtractionSummary => ActiveExtractionMethod switch
    {
        "pdf_text" => "当前任务：方案1 · 直接 PDF 提取 → 人工审核",
        "ocr" => "当前任务：方案2 · OCR 识别 → 人工审核",
        null => "尚未开始任务；两种方案提取后均需人工审核。",
        _ => $"当前任务：未知提取方案（{ActiveExtractionMethod}）；请核对任务信息。",
    };

    partial void OnActiveExtractionMethodChanged(string? value) => OnPropertyChanged(nameof(ActiveExtractionSummary));
    partial void OnSelectedExtractionMethodChanged(string value) => OnPropertyChanged(nameof(ExtractionMethodHint));

    partial void OnSelectedFilePathChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnStockCodeChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnCompanyNameChanged(string value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnReportYearChanged(int value) => OnPropertyChanged(nameof(CanSubmit));
    partial void OnIsBusyChanged(bool value)
    {
        OnPropertyChanged(nameof(CanSubmit));
        OnPropertyChanged(nameof(CanChooseExtractionMethod));
        OnPropertyChanged(nameof(CanOpenOcrMapping));
    }

    partial void OnSelectedTablePageChanged(int? value)
    {
        var selectedId = SelectedOriginalTable?.Id;
        OriginalTables.Clear();
        foreach (var table in _originalTables.Where(table => table.PageNo == value))
            OriginalTables.Add(table);
        SelectedOriginalTable = OriginalTables.FirstOrDefault(table => table.Id == selectedId)
            ?? OriginalTables.FirstOrDefault();
    }

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
        int[]? pageNumbers;
        try { pageNumbers = ReadSelectedPageNumbers(); }
        catch (Exception ex) { ErrorMessage = ex.Message; return; }
        var extractionMethod = SelectedExtractionMethod;
        ActiveExtractionMethod = extractionMethod;
        IsBusy = true;
        ErrorMessage = "";
        Progress = 0;
        CanStartAnalysis = false;
        Facts.Clear();
        ClearAnnualDisclosures();
        ClearReportData();
        _originalTables = [];
        OriginalTablePages.Clear();
        OriginalTables.Clear();
        SelectedTablePage = null;
        SelectedOriginalTable = null;
        OriginalTableSummary = "原表：等待解析。";
        MetricsSummary = "指标：等待事实复核";
        AnalysisSummary = "分析：未生成";
        AnalysisReasoningSummary = "供应商思考文本：尚无已保存的模型调用。";
        CanResumeAnalysis = false;
        _analysisRunId = 0;
        _analysisFilingId = 0;
        ExportSummary = "导出：未生成";
        try
        {
            StatusMessage = "正在上传…";
            using var cancellation = BeginExtractionTracking();
            var filingId = await api.UploadDocumentAsync(SelectedFilePath, StockCode, CompanyName,
                ReportYear, SelectedReportPeriod, cancellation.Token);
            _filingId = filingId;
            var jobId = await api.StartJobAsync(filingId, extractionMethod, pageNumbers, cancellation.Token);
            _jobId = jobId;
            await TrackExtractionAsync(jobId, cancellation.Token);
        }
        catch (OperationCanceledException) { ErrorMessage = "请求已取消或超时。"; }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { _extractionCancellation = null; IsBusy = false; }
    }

    private CancellationTokenSource BeginExtractionTracking()
    {
        _extractionCancellation?.Cancel();
        var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(15));
        _extractionCancellation = cancellation;
        return cancellation;
    }

    private async Task TrackExtractionAsync(long jobId, CancellationToken cancellationToken)
    {
        while (true)
        {
            var job = await api.GetJobAsync(jobId, cancellationToken);
            if (!string.IsNullOrWhiteSpace(job.ExtractionMethod)) ActiveExtractionMethod = job.ExtractionMethod;
            Progress = job.Progress;
            StatusMessage = $"阶段：{job.Stage}；状态：{job.Status}；待复核：{job.ReviewCount}";
            if (job.Status is "completed" or "failed" or "cancelled")
            {
                ErrorMessage = job.ErrorDetail ?? "";
                if (job.Status == "completed")
                {
                    await RefreshFactsAsync(cancellationToken);
                    if (ActiveExtractionMethod == "pdf_text")
                    {
                        try { await LoadReportDataAsync(cancellationToken); }
                        catch (Exception ex) { ReportDataSummary = $"报告金额候选暂不可用：{ex.Message}"; }
                    }
                    StatusMessage = ActiveExtractionMethod == "ocr"
                        ? "OCR 识别完成。请在原表中打开人工映射，补齐原图定位证据、科目与期间，确认审计意见后，再逐条完成人工事实审核。"
                        : "提取完成，需逐项核对原文并完成人工事实审核，才可计算指标与启动分析。";
                }
                await RefreshAuditTrailAsync(cancellationToken);
                return;
            }
            await Task.Delay(TimeSpan.FromSeconds(1), cancellationToken);
        }
    }

    [RelayCommand]
    private async Task StartAnalysisAsync()
    {
        if (!CanStartAnalysis || _filingId == 0) return;
        try
        {
            CanStartAnalysis = false;
            using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(30));
            AnalysisReasoningSummary = "供应商思考文本：等待本次模型调用。";
            var runId = await api.StartAnalysisAsync(_filingId, cancellation.Token);
            _analysisRunId = runId;
            _analysisFilingId = _filingId;
            CanResumeAnalysis = false;
            StatusMessage = $"分析任务：{runId}，正在生成并校验…";
            await TrackAnalysisAsync(runId, cancellation.Token);
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { CanStartAnalysis = true; }
    }

    [RelayCommand]
    private async Task RefreshFactsAsync()
    {
        try { await RefreshFactsAsync(CancellationToken.None); }
        catch (Exception ex) { ErrorMessage = $"读取事实与原表失败：{ex.Message}"; }
    }

    [RelayCommand]
    private async Task RefreshOriginalTablesAsync()
    {
        try { await RefreshOriginalTablesAsync(CancellationToken.None); }
        catch (Exception ex) { OriginalTableSummary = $"读取原表失败：{ex.Message}"; }
    }

    private async Task RefreshOriginalTablesAsync(CancellationToken cancellationToken)
    {
        if (_filingId == 0) return;
        var selectedPage = SelectedTablePage;
        var selectedId = SelectedOriginalTable?.Id;
        var tables = await api.GetTablesAsync(_filingId, cancellationToken);
        _originalTables = tables.OrderBy(table => table.PageNo).ThenBy(table => table.Id)
            .Select(table => new OriginalTableView(table)).ToArray();
        OriginalTablePages.Clear();
        foreach (var page in _originalTables.Select(table => table.PageNo).Distinct())
            OriginalTablePages.Add(page);
        SelectedTablePage = selectedPage is int current && OriginalTablePages.Contains(current)
            ? current : OriginalTablePages.Cast<int?>().FirstOrDefault();
        // Refresh the table list even if the selected page number did not change.
        OnSelectedTablePageChanged(SelectedTablePage);
        SelectedOriginalTable = OriginalTables.FirstOrDefault(table => table.Id == selectedId)
            ?? OriginalTables.FirstOrDefault();
        OriginalTableSummary = tables.Length == 0
            ? "原表：尚无可展示的表格候选。"
            : $"原表：{tables.Length} 张，覆盖 {OriginalTablePages.Count} 页。只读展示，不作为已核验事实。";
    }

    private async Task RefreshFactsAsync(CancellationToken cancellationToken)
    {
        if (_filingId == 0) return;
        var selectedId = SelectedFact?.Id;
        var facts = await api.GetFactsAsync(_filingId, cancellationToken);
        Facts.Clear();
        foreach (var fact in facts) Facts.Add(new FactRowView(fact));
        SelectedFact = Facts.FirstOrDefault(f => f.Id == selectedId);
        ReviewSummary = $"事实：{facts.Length} 条；待复核：{facts.Count(f => f.ReviewStatus != "verified")} 条";
        var validation = await api.GetValidationsAsync(_filingId, cancellationToken);
        ValidationMessage = $"财报状态：{validation.FilingStatus}；" + string.Join("；", validation.Results.Select(r => $"{r.RuleCode}: {r.Status}（{r.ActualValue}）"));
        CanStartAnalysis = false;
        await RefreshOriginalTablesAsync(cancellationToken);
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
            if (SelectedFact.Fact.Extractor?.StartsWith("ocr", StringComparison.OrdinalIgnoreCase) == true)
            { ErrorMessage = "OCR 事实请在原表的 OCR 映射窗口订正原数并重新双人复核；金额必须继续绑定原图证据。"; return; }
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
        if (_jobId == 0 || IsBusy) return;
        IsBusy = true;
        ErrorMessage = "";
        CanStartAnalysis = false;
        try
        {
            using var cancellation = BeginExtractionTracking();
            await api.RetryJobAsync(_jobId, cancellation.Token);
            StatusMessage = "已重新提交任务";
            await TrackExtractionAsync(_jobId, cancellation.Token);
        }
        catch (OperationCanceledException) { ErrorMessage = "任务跟踪已取消或超时。"; }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { _extractionCancellation = null; IsBusy = false; }
    }

    [RelayCommand]
    private async Task CancelAsync()
    {
        if (_jobId == 0) return;
        try
        {
            await api.CancelJobAsync(_jobId, CancellationToken.None);
            _extractionCancellation?.Cancel();
            StatusMessage = "已取消任务";
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
    }

    [RelayCommand]
    private async Task ExportAsync()
    {
        var reportFilingId = _analysisFilingId != 0 ? _analysisFilingId : _filingId;
        if (reportFilingId == 0) return;
        try
        {
            var url = await api.CreateExportAsync(reportFilingId, "html", CancellationToken.None);
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

public sealed record FactRowView(FactItem Fact)
{
    public long Id => Fact.Id;
    public string ReviewStatus => Fact.ReviewStatus;
    public string ConceptName => Fact.ConceptName;
    public SourceLabelView Label => new(string.IsNullOrEmpty(Fact.Evidence?.RawLabel)
        ? Fact.ConceptName : Fact.Evidence.RawLabel, Fact.Evidence?.BoundingBox?.LabelLayout);
    public string AmountDisplay => Fact.AmountDisplay;
    public string SourceDisplay => Fact.SourceDisplay;
}

public sealed class OriginalTableView
{
    private static readonly HashSet<string> KnownNames = [
        "合并资产负债表", "合并利润表", "合并现金流量表",
        "母公司资产负债表", "母公司利润表", "母公司现金流量表",
        "资产负债表", "利润表", "现金流量表",
    ];

    public OriginalTableItem Source { get; }
    public long Id => Source.Id;
    public int PageNo => Source.PageNo;
    public string DisplayName => Source.TableName is string name && KnownNames.Contains(name)
        ? name : "原表候选";
    public string SelectionLabel => $"{DisplayName} · 第 {PageNo} 页 · 表 {Id}";
    public string Metadata => $"第 {PageNo} 页 / {DisplayName} / {Rows.Length} 行 · "
        + (Source.ReviewRequired ? "待核对候选" : "原表候选")
        + (Source.Confidence is decimal confidence ? $" · 提取置信度 {confidence:P0}" : "");
    public string SourceDetail => $"提取器：{Source.Extractor ?? "未记录"}；页指纹：{Source.PageSha256 ?? "未记录"}；"
        + $"原始表名：{Source.TableName ?? "未记录"}";
    public Visibility OcrSourceVisibility => string.IsNullOrWhiteSpace(Source.SourceText)
        ? Visibility.Collapsed : Visibility.Visible;
    public string FirstColumnHeading { get; }
    public string[] AdditionalColumnHeadings { get; }
    public OriginalTableRowView[] Rows { get; }

    public OriginalTableView(OriginalTableItem source)
    {
        Source = source;
        var rows = source.Rows ?? [];
        var columns = source.Columns ?? [];
        var width = Math.Max(columns.Length, rows.Select(row => row.Cells?.Length ?? 0).DefaultIfEmpty().Max());
        FirstColumnHeading = columns.FirstOrDefault() is string first && !string.IsNullOrWhiteSpace(first)
            ? first : "项目";
        AdditionalColumnHeadings = Enumerable.Range(1, Math.Max(0, width - 1))
            .Select(index => index < columns.Length && !string.IsNullOrWhiteSpace(columns[index])
                ? columns[index] : $"列 {index + 1}").ToArray();
        Rows = rows.Select(row => new OriginalTableRowView(row, width)).ToArray();
    }
}

public sealed class OriginalTableRowView
{
    public OriginalTableRow Source { get; }
    public SourceLabelView Label { get; }
    public string LayoutDetail => Source.LabelLayout is LabelLayout layout
        ? $"原文缩进 {layout.IndentPoints?.ToString("0.###", CultureInfo.InvariantCulture) ?? "未记录"} 点；标签起点 {layout.LabelStartX?.ToString("0.###", CultureInfo.InvariantCulture) ?? "未记录"} 点"
        : "此候选未记录科目几何位置；按原始单元格平铺展示。";
    public OriginalCellView[] AdditionalCells { get; }

    public OriginalTableRowView(OriginalTableRow source, int width)
    {
        Source = source;
        Label = new SourceLabelView(source.RawLabel ?? source.Cells?.FirstOrDefault() ?? "", source.LabelLayout);
        var cells = source.Cells ?? [];
        AdditionalCells = Enumerable.Range(1, Math.Max(0, width - 1))
            .Select(index => index < cells.Length
                ? new OriginalCellView(cells[index], false) : new OriginalCellView("—", true)).ToArray();
    }
}

public sealed class SourceLabelView
{
    private readonly LabelLayout? _layout;
    public string RawLabel { get; }
    public string Prefix { get; }
    public string Body { get; }
    public Thickness PrefixMargin { get; }
    public Thickness BodyMargin { get; }
    public Thickness RawMargin { get; }
    public double FontSize { get; }
    public Visibility GeometryVisibility { get; }
    public Visibility PlainVisibility => GeometryVisibility == Visibility.Visible
        ? Visibility.Collapsed : Visibility.Visible;

    public SourceLabelView(string rawLabel, LabelLayout? layout)
    {
        RawLabel = rawLabel;
        _layout = layout;
        Prefix = layout?.Prefix ?? "";
        var labelX = layout?.LabelStartX;
        var bodyX = layout?.BodyStartX;
        var origin = layout?.TableOriginX;
        if (origin is null && labelX is double start && layout?.IndentPoints is double indent)
            origin = start - indent;
        var hasCoordinates = labelX is double lx && double.IsFinite(lx)
            && bodyX is double bx && double.IsFinite(bx)
            && origin is double ox && double.IsFinite(ox)
            && bx >= lx && (Prefix.Length == 0 || bx > lx)
            && layout?.Source != "pdf_span_bbox_fallback"
            && (Prefix.Length == 0 || rawLabel.StartsWith(Prefix, StringComparison.Ordinal));
        Body = hasCoordinates && Prefix.Length > 0 ? rawLabel[Prefix.Length..].TrimStart() : rawLabel;
        GeometryVisibility = hasCoordinates ? Visibility.Visible : Visibility.Collapsed;
        PrefixMargin = new Thickness(hasCoordinates ? Offset(labelX!.Value, origin!.Value) : 0, 0, 0, 0);
        BodyMargin = new Thickness(hasCoordinates ? Offset(bodyX!.Value, origin!.Value) : 0, 0, 0, 0);
        RawMargin = new Thickness(layout?.IndentDip ?? 0, 0, 0, 0);
        FontSize = layout?.FontSize is double size && double.IsFinite(size) && size > 0
            ? size * 96.0 / 72.0 : 12;
    }

    private static double Offset(double x, double origin) => Math.Max(0, x - origin) * 96.0 / 72.0;
    public string LayoutDetail => _layout is null ? "未记录原文几何位置；按原文科目平铺展示。"
        : $"原文标签起点 {_layout.LabelStartX?.ToString("0.###", CultureInfo.InvariantCulture) ?? "未记录"} 点；"
          + $"正文起点 {_layout.BodyStartX?.ToString("0.###", CultureInfo.InvariantCulture) ?? "未记录"} 点；"
          + $"表格起点 {_layout.TableOriginX?.ToString("0.###", CultureInfo.InvariantCulture) ?? "未记录"} 点";
}

public sealed record OriginalCellView(string Value, bool Missing)
{
    public string Detail => Missing ? "原始输出缺少此单元格；未补值。" : Value;
}
