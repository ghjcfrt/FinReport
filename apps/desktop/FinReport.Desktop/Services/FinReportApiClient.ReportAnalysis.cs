using System.Net.Http.Json;
using System.Text.Json;

namespace FinReport.Desktop.Services;

public sealed partial class FinReportApiClient
{
    public async Task<long> StartJobAsync(long filingId, string extractionMethod, int[]? pageNumbers,
        CancellationToken cancellationToken)
    {
        if (extractionMethod is not ("pdf_text" or "ocr"))
            throw new ArgumentException("请选择直接 PDF 提取或 OCR 识别。", nameof(extractionMethod));
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/jobs",
            new { extraction_method = extractionMethod, page_numbers = pageNumbers }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("job_id").GetInt64();
    }

    public async Task<ReportDataContext> GetReportDataAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/filings/{filingId}/report-data", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var result = await response.Content.ReadFromJsonAsync<ApiEnvelope<ReportDataContext>>(ApiJson, cancellationToken);
        return result?.Data ?? throw new InvalidOperationException("未读取到报告金额候选。");
    }

    public async Task ReviewReportDataAsync(long filingId, string candidateId, string decision,
        string reviewerId, string reason, CancellationToken cancellationToken)
    {
        using var response = await http.PatchAsJsonAsync(
            $"api/v1/filings/{filingId}/report-data/{Uri.EscapeDataString(candidateId)}/review",
            new { decision, reviewer_id = reviewerId, reason }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<long> StartReportAnalysisAsync(long filingId, long[]? selectedDocumentIds,
        CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/report-analyses",
            new { selected_document_ids = selectedDocumentIds }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("analysis_run_id").GetInt64();
    }

    public async Task<ReportAnalysisStatus> GetReportAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/report-analyses/{runId}", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var result = await response.Content.ReadFromJsonAsync<ApiEnvelope<ReportAnalysisStatus>>(ApiJson, cancellationToken);
        return result?.Data ?? throw new InvalidOperationException("未读取到报告分析。");
    }

    public async Task<JsonElement> ReplayReportAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/report-analyses/{runId}/replay", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").Clone();
    }
}

public sealed record ReportDataContext(long FilingVersionId, string ReportType, string PeriodBasis,
    string PeriodStart, string PeriodEnd, int[]? SelectedPageNumbers, string ProcessingScope,
    JsonElement[] Items, JsonElement Calculations, string[]? Issues, string[]? Limitations);

public sealed record ReportAnalysisStatus(string Status, JsonElement? Output, string? FailureReason,
    ProviderReasoningView? ProviderReasoning, JsonElement? SemanticLinkSummary);
