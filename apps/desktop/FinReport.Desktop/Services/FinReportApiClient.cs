using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;

namespace FinReport.Desktop.Services;

/// <summary>HTTP boundary for the desktop app. It never accesses storage, database, or model providers.</summary>
public sealed partial class FinReportApiClient(HttpClient http)
{
    private static readonly JsonSerializerOptions ApiJson = new(JsonSerializerDefaults.Web) { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    public async Task<long> UploadAsync(string filePath, string stockCode, string companyName, int reportYear, CancellationToken cancellationToken)
    {
        using var content = new MultipartFormDataContent();
        await using var stream = File.OpenRead(filePath);
        using var fileContent = new StreamContent(stream);
        fileContent.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("application/pdf");
        content.Add(fileContent, "file", Path.GetFileName(filePath));
        content.Add(new StringContent(stockCode), "stock_code");
        content.Add(new StringContent(companyName), "company_name");
        content.Add(new StringContent($"{reportYear:D4}-01-01"), "period_start");
        content.Add(new StringContent($"{reportYear:D4}-12-31"), "period_end");
        content.Add(new StringContent("annual"), "report_type");
        content.Add(new StringContent("annual"), "period_basis");
        using var response = await http.PostAsync("api/v1/filings", content, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("filing_version_id").GetInt64();
    }

    public Task<long> StartJobAsync(long filingId, CancellationToken cancellationToken)
        => StartJobAsync(filingId, "pdf_text", cancellationToken);

    public async Task<long> StartJobAsync(long filingId, string extractionMethod, CancellationToken cancellationToken)
    {
        if (extractionMethod is not ("pdf_text" or "ocr"))
            throw new ArgumentException("请选择直接 PDF 提取或 OCR 识别。", nameof(extractionMethod));
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/jobs",
            new { extraction_method = extractionMethod }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("job_id").GetInt64();
    }

    public async Task<JobStatus> GetJobAsync(long jobId, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<JobStatus>>($"api/v1/jobs/{jobId}", ApiJson, cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("API response did not contain job data.");
    }

    public async Task<ModelSettings> GetModelSettingsAsync(CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<ModelSettings>>("api/v1/settings/models", ApiJson, cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("API response did not contain model settings.");
    }

    public async Task<ModelSettings> UpdateModelSettingsAsync(ModelSettingsUpdate value, CancellationToken cancellationToken)
    {
        using var response = await http.PutAsJsonAsync("api/v1/settings/models", value, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var result = await response.Content.ReadFromJsonAsync<ApiEnvelope<ModelSettings>>(ApiJson, cancellationToken: cancellationToken);
        return result?.Data ?? throw new InvalidOperationException("API response did not contain model settings.");
    }

    /// <summary>Returns the ClamAV Supervisor readiness and active database metadata.</summary>
    public async Task<ScannerHealth> GetScannerHealthAsync(CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<ScannerHealth>>("api/v1/security/scanner", ApiJson, cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("API response did not contain scanner health data.");
    }

    /// <summary>Asks the API host to download, verify, stage and activate a new ClamAV database.</summary>
    public async Task<ScannerHealth> UpdateScannerDatabaseAsync(CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync("api/v1/security/scanner/update", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var result = await response.Content.ReadFromJsonAsync<ApiEnvelope<ScannerHealth>>(ApiJson, cancellationToken: cancellationToken);
        return result?.Data ?? throw new InvalidOperationException("API response did not contain scanner update data.");
    }

    public async Task<string[]> DiscoverModelsAsync(string stage, string provider, string baseUrl, string? apiKey, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync(
            $"api/v1/settings/models/{stage}/discover",
            new ModelDiscoveryRequest(provider, baseUrl, apiKey), ApiJson,
            cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var result = await response.Content.ReadFromJsonAsync<ApiEnvelope<ModelDiscoveryResponse>>(ApiJson, cancellationToken: cancellationToken);
        return result?.Data.Models ?? Array.Empty<string>();
    }

    public async Task<long> StartAnalysisAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/filings/{filingId}/analyses", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("analysis_run_id").GetInt64();
    }

    public async Task RetryJobAsync(long jobId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/jobs/{jobId}/retry", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task CancelJobAsync(long jobId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/jobs/{jobId}/cancel", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<FactItem[]> GetFactsAsync(long filingId, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<FactItem[]>>($"api/v1/filings/{filingId}/facts", ApiJson, cancellationToken);
        return response?.Data ?? Array.Empty<FactItem>();
    }

    public async Task<OriginalTableItem[]> GetTablesAsync(long filingId, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<OriginalTableItem[]>>(
            $"api/v1/filings/{filingId}/tables", ApiJson, cancellationToken);
        return response?.Data ?? Array.Empty<OriginalTableItem>();
    }

    public async Task ReviewFactAsync(long factId, string reviewerId, string decision, string reason, decimal? correctedValue, CancellationToken cancellationToken)
    {
        using var response = await http.PatchAsJsonAsync($"api/v1/facts/{factId}/review",
            new { reviewer_id = reviewerId, decision, reason, corrected_value = correctedValue }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<ValidationSummary> GetValidationsAsync(long filingId, CancellationToken cancellationToken)
    {
        var value = await http.GetFromJsonAsync<ApiEnvelope<ValidationSummary>>($"api/v1/filings/{filingId}/validations", ApiJson, cancellationToken);
        return value?.Data ?? throw new InvalidOperationException("未读取到校验结果。");
    }

    public async Task<AnalysisStatus> GetAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        var value = await http.GetFromJsonAsync<ApiEnvelope<AnalysisStatus>>($"api/v1/analyses/{runId}", ApiJson, cancellationToken);
        return value?.Data ?? throw new InvalidOperationException("未读取到分析结果。");
    }

    public async Task DownloadExportAsync(string url, string target, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync(url, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        await File.WriteAllBytesAsync(target, await response.Content.ReadAsByteArrayAsync(cancellationToken), cancellationToken);
    }

    private static async Task EnsureSuccessAsync(HttpResponseMessage response, CancellationToken cancellationToken)
    {
        if (response.IsSuccessStatusCode) return;
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        throw new InvalidOperationException($"API {(int)response.StatusCode}：{body}");
    }

    public async Task<MetricItem[]> CalculateMetricsAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/metrics", new { strict = true }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var value = await response.Content.ReadFromJsonAsync<ApiEnvelope<MetricItem[]>>(ApiJson, cancellationToken: cancellationToken);
        return value?.Data ?? Array.Empty<MetricItem>();
    }

    public async Task<AuditTrail> GetAuditTrailAsync(long filingId, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<AuditTrail>>($"api/v1/filings/{filingId}/audit-trail", ApiJson, cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("API response did not contain audit trail data.");
    }

    public async Task<string?> CreateExportAsync(long filingId, string format, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/exports?filing_version_id={filingId}&output_format={format}", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var value = await response.Content.ReadFromJsonAsync<ApiEnvelope<ExportItem>>(ApiJson, cancellationToken: cancellationToken);
        return value?.Data.DownloadUrl;
    }

    private sealed record ApiEnvelope<T>(T Data, string RequestId);
}

public sealed record JobStatus(long JobId, string Stage, string Status, int Progress, string? ErrorCode, string? ErrorDetail, int ReviewCount, string? ExtractionMethod = null, bool ReviewRequired = true);
public sealed record ModelSettings(ModelEndpoint Analysis, ModelEndpoint Ocr, string[] SupportedProviders, bool SecretsManagedServerSide);
public sealed record ModelEndpoint(string Provider, string? BaseUrl, string? Model, bool Configured, bool CredentialsConfigured);
public sealed record ModelSettingsUpdate(ModelEndpointUpdate Analysis, ModelEndpointUpdate Ocr);
public sealed record ModelEndpointUpdate(string Provider, string BaseUrl, string? ApiKey, string Model);
public sealed record ScannerHealth(
    bool Ready,
    int QueueDepth,
    int ActiveScans,
    string? UpdateError,
    string? DatabaseVersion = null,
    long? DatabaseAgeSeconds = null,
    string? DatabaseUpdatedAt = null,
    string? State = null);
public sealed record ModelDiscoveryRequest(string Provider, string BaseUrl, string? ApiKey);
public sealed record ModelDiscoveryResponse(string[] Models);
public sealed record FactItem(long Id, string ConceptCode, string ConceptName, decimal Value, string Currency, int UnitScale, string FlowOrStock, string QualityStatus, string ReviewStatus, int RevisionNo, FactEvidence? Evidence, string? Extractor, string RawValue, string PeriodStart, string PeriodEnd)
{
    public string AmountDisplay => $"{Value:N2} {Currency}元";
    public string SourceDisplay => $"第{Evidence?.PageNo}页 / {Evidence?.TableName} / {Evidence?.ColumnLocator} / 原数 {RawValue} × {UnitScale}";
}
public sealed record FactEvidence(int PageNo, string TableName, string RowLocator, string ColumnLocator, string RawLabel, string RawValue, EvidenceBoundingBox? BoundingBox = null);
public sealed record EvidenceBoundingBox(LabelLayout? LabelLayout = null);
public sealed record LabelLayout(
    double? LabelStartX = null,
    double? BodyStartX = null,
    string? Prefix = null,
    double? IndentPoints = null,
    int? IndentLevel = null,
    double[]? LabelBbox = null,
    double? FontSize = null,
    string? Source = null,
    double? TableOriginX = null)
{
    // PDF coordinates use points (1/72 inch); WPF uses DIP (1/96 inch).
    // Legacy evidence with no geometry stays flush left. IndentLevel is only
    // descriptive; it never controls a fixed amount of padding.
    public double IndentDip => IndentPoints is double points && double.IsFinite(points)
        ? Math.Max(0, points) * 96.0 / 72.0 : 0;
}
public sealed record OriginalTableItem(
    long Id,
    long ExtractionJobId,
    int PageNo,
    string? TableName,
    string[]? Columns,
    OriginalTableRow[]? Rows,
    string? Extractor,
    decimal? Confidence,
    bool ReviewRequired,
    string? PageSha256,
    string? SourceText = null,
    long? OcrCallId = null,
    string? ResponseSha256 = null);
public sealed record OriginalTableRow(
    string[]? Cells,
    string? RawLabel,
    LabelLayout? LabelLayout = null,
    double[]? RowBbox = null);
public sealed record ValidationSummary(string FilingStatus, ValidationItem[] Results);
public sealed record ValidationItem(string RuleCode, string Status, string? ActualValue, string? ExpectedValue);
public sealed record AnalysisStatus(string Status, JsonElement? Output, string? FailureReason);
public sealed record MetricItem(long Id, string MetricCode, decimal? Value, string Unit, string FormulaVersion, string ApplicabilityStatus, string ComputationMode, string QualityStatus, string Scope, string PeriodBasis);
public sealed record ExportItem(string Status, string Format, string DownloadUrl);
public sealed record AuditTrail(long FilingVersionId, ReproductionInfo Reproduction, AuditEventItem[] Events);
public sealed record ReproductionInfo(string FileSha256, string ReportingPeriod, string ReportType, string PeriodBasis, string SourceType, string? SourceUri);
public sealed record AuditEventItem(long Id, string Category, string Action, string Status, string? SourceRef, string? ToolName, string? ToolVersion, string? InputHash, string? OutputHash, Dictionary<string, object>? Detail, DateTime OccurredAt)
{
    public string DetailSummary => Detail is null ? "" : string.Join("；", Detail.Select(item => $"{item.Key}={item.Value}"));
}
