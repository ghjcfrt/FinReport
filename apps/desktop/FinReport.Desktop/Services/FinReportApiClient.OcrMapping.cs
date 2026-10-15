using System.Net.Http.Json;
using System.Security.Cryptography;

namespace FinReport.Desktop.Services;

public sealed partial class FinReportApiClient
{
    public async Task<OcrMappingContext> GetOcrMappingContextAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/filings/{filingId}/ocr-mapping-context", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var value = await response.Content.ReadFromJsonAsync<ApiEnvelope<OcrMappingContext>>(ApiJson, cancellationToken);
        return value?.Data ?? throw new InvalidOperationException("未读取到当前 OCR 映射上下文。");
    }

    public async Task<byte[]> GetOcrPageImageAsync(long filingId, OcrMappingPage page, CancellationToken cancellationToken)
    {
        // The server-owned image route pins the OCR attempt; never follow an arbitrary OCR URL.
        var expected = $"/api/v1/filings/{filingId}/ocr-pages/{page.PageNo}.png?ocr_call_id={page.OcrCallId}";
        if (page.ImageUrl != expected) throw new InvalidOperationException("OCR 原图地址与当前财报不符。");
        using var response = await http.GetAsync(expected.TrimStart('/'), cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var bytes = await response.Content.ReadAsByteArrayAsync(cancellationToken);
        var hash = Convert.ToHexString(SHA256.HashData(bytes));
        if (!hash.Equals(page.PageSha256, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("OCR 原图指纹不匹配，请刷新当前提取结果。");
        return bytes;
    }

    public async Task<OcrMappingResult> MapOcrFactAsync(long filingId, OcrFactMappingRequest request, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/ocr-fact-mappings", request, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var value = await response.Content.ReadFromJsonAsync<ApiEnvelope<OcrMappingResult>>(ApiJson, cancellationToken);
        return value?.Data ?? throw new InvalidOperationException("未读取到映射结果。");
    }

    public async Task ConfirmOcrAuditAsync(long filingId, OcrAuditConfirmation request, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/ocr-audit-confirmations", request, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<OcrMappingCompletionResult> CompleteOcrMappingAsync(long filingId, OcrMappingCompletionRequest request, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/ocr-mapping-completion", request, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var value = await response.Content.ReadFromJsonAsync<ApiEnvelope<OcrMappingCompletionResult>>(ApiJson, cancellationToken);
        return value?.Data ?? throw new InvalidOperationException("未读取到映射完成结果。");
    }
}

public sealed record OcrMappingContext(long JobId, OcrMappingFiling Filing, OcrMappingConcept[] Concepts, OcrMappingPage[] Pages, long[] MappedFactIds);
public sealed record OcrMappingFiling(string PeriodStart, string PeriodEnd, string Scope, string AccountingStandard, string? FileSha256 = null);
public sealed record OcrMappingConcept(string Code, string Name, string StatementType, string FlowOrStock)
{
    public string Display => $"{Name} ({Code})";
}
public sealed record OcrMappingPage(int PageNo, long OcrCallId, string PageSha256, string ResponseSha256, string SourceText, string ImageUrl, int Width, int Height)
{
    public string Display => $"第 {PageNo} 页 · OCR 调用 {OcrCallId}";
}
public sealed record OcrFactMappingRequest(long TableId, long OcrCallId, string PageSha256, string ResponseSha256,
    int RowIndex, int ColumnIndex, string ConceptCode, string RawLabel, string RawValue, int UnitScale,
    string Currency, string PeriodRole, string PeriodStart, string PeriodEnd, string Scope, string AccountingStandard,
    bool IsRestatement, string TableName, string ColumnLabel, double[] ValueBbox, double[] RowBbox,
    string UnitText, double[] UnitBbox, string ReviewerId, string Reason, string? CorrectedRawValue = null,
    long? UnitOcrCallId = null, string? UnitPageSha256 = null, string? UnitResponseSha256 = null);
public sealed record OcrMappingResult(long FactId, string FilingStatus, bool ReviewRequired);
public sealed record OcrAuditConfirmation(long OcrCallId, string PageSha256, string ResponseSha256,
    string Quote, double[] Bbox, string Opinion, string ReviewerId, string Reason);
public sealed record OcrMappingCompletionRequest(long JobId, string FileSha256, string ReviewerId, string Reason);
public sealed record OcrMappingCompletionResult(long[] ArchivedFactIds, long[] CurrentFactIds, string FilingStatus, bool ReviewRequired);
