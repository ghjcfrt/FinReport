using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;

namespace FinReport.Desktop.Services;

/// <summary>HTTP boundary for the desktop app. It never accesses storage, database, or model providers.</summary>
public sealed class FinReportApiClient(HttpClient http)
{
    public async Task<long> UploadAsync(string filePath, string stockCode, string companyName, CancellationToken cancellationToken)
    {
        using var content = new MultipartFormDataContent();
        await using var stream = File.OpenRead(filePath);
        using var fileContent = new StreamContent(stream);
        fileContent.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("application/pdf");
        content.Add(fileContent, "file", Path.GetFileName(filePath));
        content.Add(new StringContent(stockCode), "stock_code");
        content.Add(new StringContent(companyName), "company_name");
        content.Add(new StringContent("2025-01-01"), "period_start");
        content.Add(new StringContent("2025-12-31"), "period_end");
        content.Add(new StringContent("annual"), "report_type");
        content.Add(new StringContent("annual"), "period_basis");
        using var response = await http.PostAsync("api/v1/filings", content, cancellationToken);
        response.EnsureSuccessStatusCode();
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("filing_version_id").GetInt64();
    }

    public async Task<long> StartJobAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/filings/{filingId}/jobs", null, cancellationToken);
        response.EnsureSuccessStatusCode();
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("job_id").GetInt64();
    }

    public async Task<JobStatus> GetJobAsync(long jobId, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<JobStatus>>($"api/v1/jobs/{jobId}", cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("API response did not contain job data.");
    }

    public async Task<ModelSettings> GetModelSettingsAsync(CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<ModelSettings>>("api/v1/settings/models", cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("API response did not contain model settings.");
    }

    public async Task<ModelSettings> UpdateModelSettingsAsync(ModelSettingsUpdate value, CancellationToken cancellationToken)
    {
        using var response = await http.PutAsJsonAsync("api/v1/settings/models", value, cancellationToken);
        response.EnsureSuccessStatusCode();
        var result = await response.Content.ReadFromJsonAsync<ApiEnvelope<ModelSettings>>(cancellationToken: cancellationToken);
        return result?.Data ?? throw new InvalidOperationException("API response did not contain model settings.");
    }

    public async Task<string[]> DiscoverModelsAsync(string stage, string provider, string baseUrl, string? apiKey, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync(
            $"api/v1/settings/models/{stage}/discover",
            new ModelDiscoveryRequest(provider, baseUrl, apiKey),
            cancellationToken);
        response.EnsureSuccessStatusCode();
        var result = await response.Content.ReadFromJsonAsync<ApiEnvelope<ModelDiscoveryResponse>>(cancellationToken: cancellationToken);
        return result?.Data.Models ?? Array.Empty<string>();
    }

    public async Task<long> StartAnalysisAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/filings/{filingId}/analyses", null, cancellationToken);
        response.EnsureSuccessStatusCode();
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("analysis_run_id").GetInt64();
    }

    public async Task RetryJobAsync(long jobId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/jobs/{jobId}/retry", null, cancellationToken);
        response.EnsureSuccessStatusCode();
    }

    public async Task CancelJobAsync(long jobId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/jobs/{jobId}/cancel", null, cancellationToken);
        response.EnsureSuccessStatusCode();
    }

    public async Task<FactItem[]> GetFactsAsync(long filingId, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<FactItem[]>>($"api/v1/filings/{filingId}/facts", cancellationToken);
        return response?.Data ?? Array.Empty<FactItem>();
    }

    public async Task<MetricItem[]> CalculateMetricsAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/metrics", new { strict = true }, cancellationToken);
        response.EnsureSuccessStatusCode();
        var value = await response.Content.ReadFromJsonAsync<ApiEnvelope<MetricItem[]>>(cancellationToken: cancellationToken);
        return value?.Data ?? Array.Empty<MetricItem>();
    }

    public async Task<AuditTrail> GetAuditTrailAsync(long filingId, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<AuditTrail>>($"api/v1/filings/{filingId}/audit-trail", cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("API response did not contain audit trail data.");
    }

    public async Task<string?> CreateExportAsync(long filingId, string format, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/exports?filing_version_id={filingId}&output_format={format}", null, cancellationToken);
        response.EnsureSuccessStatusCode();
        var value = await response.Content.ReadFromJsonAsync<ApiEnvelope<ExportItem>>(cancellationToken: cancellationToken);
        return value?.Data.DownloadUrl;
    }

    private sealed record ApiEnvelope<T>(T Data, string RequestId);
}

public sealed record JobStatus(long JobId, string Stage, string Status, int Progress, string? ErrorCode, string? ErrorDetail, int ReviewCount);
public sealed record ModelSettings(ModelEndpoint Analysis, ModelEndpoint Ocr, string[] SupportedProviders, bool SecretsManagedServerSide);
public sealed record ModelEndpoint(string Provider, string? BaseUrl, string? Model, bool Configured, bool CredentialsConfigured);
public sealed record ModelSettingsUpdate(ModelEndpointUpdate Analysis, ModelEndpointUpdate Ocr);
public sealed record ModelEndpointUpdate(string Provider, string BaseUrl, string? ApiKey, string Model);
public sealed record ModelDiscoveryRequest(string Provider, string BaseUrl, string? ApiKey);
public sealed record ModelDiscoveryResponse(string[] Models);
public sealed record FactItem(long Id, string ConceptCode, string ConceptName, decimal Value, string Currency, int UnitScale, string FlowOrStock, string QualityStatus, string ReviewStatus, int RevisionNo, object? Evidence, string? Extractor);
public sealed record MetricItem(long Id, string MetricCode, decimal? Value, string Unit, string FormulaVersion, string ApplicabilityStatus, string ComputationMode, string QualityStatus, string Scope, string PeriodBasis);
public sealed record ExportItem(string Status, string Format, string DownloadUrl);
public sealed record AuditTrail(long FilingVersionId, ReproductionInfo Reproduction, AuditEventItem[] Events);
public sealed record ReproductionInfo(string FileSha256, string ReportingPeriod, string ReportType, string PeriodBasis, string SourceType, string? SourceUri);
public sealed record AuditEventItem(long Id, string Category, string Action, string Status, string? SourceRef, string? ToolName, string? ToolVersion, string? InputHash, string? OutputHash, Dictionary<string, object>? Detail, DateTime OccurredAt)
{
    public string DetailSummary => Detail is null ? "" : string.Join("；", Detail.Select(item => $"{item.Key}={item.Value}"));
}
