using System.Collections.ObjectModel;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FinReport.Desktop.Services;

namespace FinReport.Desktop.ViewModels;

public partial class MainViewModel
{
    [ObservableProperty] private bool canResumeAnalysis;
    [ObservableProperty] private string analysisWorkflowStatus = "分析工作流：尚未启动";
    [ObservableProperty] private string analysisReasoningSummary = "供应商思考文本：尚无已保存的模型调用。";
    [ObservableProperty] private int textEvidencePageNo = 1;
    [ObservableProperty] private TextEvidenceItem? selectedTextEvidence;
    [ObservableProperty] private TextReviewDocument? selectedTextReviewDocument;
    [ObservableProperty] private string textEvidenceSummary = "原文证据与结构化数值分开审核。请按页加载，核对 PDF 后确认。";
    [ObservableProperty] private string selectedReportPeriod = "annual";
    public ObservableCollection<TextEvidenceItem> TextEvidence { get; } = [];
    public ObservableCollection<TextReviewDocument> TextReviewDocuments { get; } = [];
    private long _analysisRunId;
    private long _analysisFilingId;
    private long _textReviewAnchorId;

    private void EnsureTextReviewDocuments()
    {
        var anchorId = _analysisFilingId != 0 ? _analysisFilingId : _filingId;
        if (anchorId == 0) return;
        if ((_analysisFilingId != 0 && _textReviewAnchorId != anchorId)
            || (_analysisFilingId == 0 && !TextReviewDocuments.Any(item => item.FilingVersionId == _filingId)))
        {
            TextReviewDocuments.Clear();
            SelectedTextReviewDocument = null;
            TextEvidence.Clear();
            SelectedTextEvidence = null;
        }
        _textReviewAnchorId = anchorId;
        RememberTextReviewDocument(anchorId);
        if (_filingId != 0) RememberTextReviewDocument(_filingId);
    }

    private void RememberTextReviewDocument(long filingId)
    {
        if (TextReviewDocuments.Any(item => item.FilingVersionId == filingId)) return;
        var label = filingId == _textReviewAnchorId ? "分析主财报" : "补充材料";
        TextReviewDocuments.Add(new TextReviewDocument(filingId, $"材料 {filingId}（{label}）"));
        SelectedTextReviewDocument ??= TextReviewDocuments[0];
    }

    partial void OnSelectedTextReviewDocumentChanged(TextReviewDocument? value)
    {
        TextEvidencePageNo = 1;
        TextEvidence.Clear();
        SelectedTextEvidence = null;
        TextEvidenceSummary = value is null ? "请选择审核材料。"
            : $"已选择{value.Label}，请加载本页原文，并对照该材料的原始 PDF。";
    }

    private void ApplyAnalysisStatus(long runId, AnalysisStatus run)
    {
        AnalysisWorkflowStatus = $"分析任务 {runId}：{run.Status}";
        AnalysisReasoningSummary = FormatProviderReasoning(run.ProviderReasoning);
        if (run.Output is JsonElement output)
        {
            if (output.TryGetProperty("agent_workflow", out var workflow)
                && workflow.ValueKind == JsonValueKind.Object
                && workflow.TryGetProperty("document_snapshot", out var documents)
                && documents.ValueKind == JsonValueKind.Array)
                foreach (var document in documents.EnumerateArray())
                    if (document.TryGetProperty("filing_version_id", out var id) && id.TryGetInt64(out var filingId))
                        RememberTextReviewDocument(filingId);
            AnalysisSummary = JsonSerializer.Serialize(output, new JsonSerializerOptions {
                WriteIndented = true,
                Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            });
        }
        else AnalysisSummary = "分析未返回内容";
        CanResumeAnalysis = run.Status is "partial_result" or "waiting_for_documents" or "invalidated" or "continued";
        if (run.Status == "invalidated")
            AnalysisSummary = "以下为历史结果：原文审核已变化，需要继续分析后重新校验。\n" + AnalysisSummary;
        StatusMessage = run.Status switch {
            "completed" => "分析完成。数值与公式已校验，模型分析与原文依据分别保留。",
            "invalidated" => "原文审核已变化，原结果已失效并保留为历史记录。可点击继续分析生成新版本。",
            "partial_result" or "waiting_for_documents" => "已有部分结果。请查看材料需求，选择相应财报审核，或登记补充材料后继续分析。",
            "continued" => "此结果已有后续版本，点击继续分析可接回后续任务。",
            "failed" => run.FailureReason ?? "分析失败。",
            _ => "分析正在执行。",
        };
    }

    [RelayCommand]
    private async Task ResumeAnalysisAsync()
    {
        if (!CanResumeAnalysis || _analysisRunId == 0) return;
        CanResumeAnalysis = false;
        CanStartAnalysis = false;
        ErrorMessage = "";
        try
        {
            using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(30));
            _analysisRunId = await api.ResumeAnalysisAsync(_analysisRunId, cancellation.Token);
            await TrackAnalysisAsync(_analysisRunId, cancellation.Token);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; CanResumeAnalysis = true; }
        finally { CanStartAnalysis = _filingId == _analysisFilingId && _analysisFilingId != 0; }
    }

    private async Task TrackAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        EnsureTextReviewDocuments();
        while (true)
        {
            var run = await api.GetAnalysisAsync(runId, cancellationToken);
            ApplyAnalysisStatus(runId, run);
            if (run.Status is "completed" or "partial_result" or "waiting_for_documents" or "invalidated" or "continued")
            {
                await RefreshAuditTrailAsync(CancellationToken.None);
                return;
            }
            if (run.Status == "failed")
                throw new InvalidOperationException(run.FailureReason ?? run.Status);
            await Task.Delay(1000, cancellationToken);
        }
    }

    [RelayCommand]
    private async Task RegisterSupplementAsync()
    {
        if (!CanSubmit) return;
        int[]? pageNumbers;
        try { pageNumbers = ReadSelectedPageNumbers(); }
        catch (Exception ex) { ErrorMessage = ex.Message; return; }
        EnsureTextReviewDocuments();
        ClearReportData();
        IsBusy = true;
        CanStartAnalysis = false;
        ErrorMessage = "";
        try
        {
            using var cancellation = BeginExtractionTracking();
            _filingId = await api.UploadDocumentAsync(SelectedFilePath, StockCode, CompanyName,
                ReportYear, SelectedReportPeriod, cancellation.Token);
            RememberTextReviewDocument(_filingId);
            SelectedTextReviewDocument = TextReviewDocuments.First(item => item.FilingVersionId == _filingId);
            ActiveExtractionMethod = SelectedExtractionMethod;
            _jobId = await api.StartJobAsync(_filingId, SelectedExtractionMethod, pageNumbers, cancellation.Token);
            await TrackExtractionAsync(_jobId, cancellation.Token);
            await api.IndexTextEvidenceAsync(_filingId, cancellation.Token);
            await LoadTextEvidencePageAsync(cancellation.Token);
            StatusMessage = $"补充材料 {_filingId} 已登记。请核对原文；原分析任务 {_analysisRunId} 保留，可审核后继续。";
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { _extractionCancellation = null; IsBusy = false; }
    }

    [RelayCommand]
    private async Task LoadTextEvidenceAsync()
    {
        EnsureTextReviewDocuments();
        var filingId = SelectedTextReviewDocument?.FilingVersionId ?? 0;
        if (filingId == 0) return;
        try
        {
            await api.IndexTextEvidenceAsync(filingId, CancellationToken.None);
            await LoadTextEvidencePageAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = $"读取原文证据失败：{ex.Message}"; }
    }

    private async Task LoadTextEvidencePageAsync(CancellationToken cancellationToken)
    {
        var filingId = SelectedTextReviewDocument?.FilingVersionId ?? 0;
        if (filingId == 0) return;
        var pageNo = Math.Max(1, TextEvidencePageNo);
        var page = await api.GetTextEvidenceAsync(filingId, pageNo, cancellationToken);
        if (SelectedTextReviewDocument?.FilingVersionId != filingId || Math.Max(1, TextEvidencePageNo) != pageNo) return;
        var selectedId = SelectedTextEvidence?.Id;
        TextEvidence.Clear();
        foreach (var item in page.Hits) TextEvidence.Add(item);
        SelectedTextEvidence = TextEvidence.FirstOrDefault(item => item.Id == selectedId) ?? TextEvidence.FirstOrDefault();
        TextEvidenceSummary = $"材料 {filingId} · 第 {pageNo} 页 · {TextEvidence.Count} 个文本块；确认原文只确认转录一致，不证明公司解释为真。";
    }

    [RelayCommand] private Task ConfirmTextEvidenceAsync() => ReviewTextEvidenceAsync("confirmed");
    [RelayCommand] private Task RejectTextEvidenceAsync() => ReviewTextEvidenceAsync("rejected");

    private async Task ReviewTextEvidenceAsync(string decision)
    {
        EnsureTextReviewDocuments();
        if (SelectedTextEvidence is null || string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(ReviewReason))
        { ErrorMessage = "请选择文本块，并填写复核人标识与核对说明。"; return; }
        try
        {
            await api.ReviewTextEvidenceAsync(SelectedTextEvidence.FilingVersionId, SelectedTextEvidence, decision,
                ReviewerId.Trim(), ReviewReason.Trim(), CancellationToken.None);
            ErrorMessage = "";
            await LoadTextEvidencePageAsync(CancellationToken.None);
            if (_analysisRunId != 0)
                ApplyAnalysisStatus(_analysisRunId, await api.GetAnalysisAsync(_analysisRunId, CancellationToken.None));
            await RefreshAuditTrailAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
    }
}

public sealed record TextReviewDocument(long FilingVersionId, string Label);
