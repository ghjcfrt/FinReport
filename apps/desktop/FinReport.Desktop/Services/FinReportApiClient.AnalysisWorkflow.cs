using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;

namespace FinReport.Desktop.Services;

public sealed partial class FinReportApiClient
{
    public async Task<long> ResumeAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/analyses/{runId}/resume", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("analysis_run_id").GetInt64();
    }

    public async Task IndexTextEvidenceAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/filings/{filingId}/text-evidence/index", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<TextEvidencePage> GetTextEvidenceAsync(long filingId, int pageNo, CancellationToken cancellationToken)
    {
        var response = await http.GetFromJsonAsync<ApiEnvelope<TextEvidencePage>>(
            $"api/v1/filings/{filingId}/text-evidence/review?page_no={pageNo}&include_rejected=true&limit=100", ApiJson, cancellationToken);
        return response?.Data ?? throw new InvalidOperationException("未读取到原文证据。");
    }

    public async Task ReviewTextEvidenceAsync(long filingId, TextEvidenceItem chunk, string decision,
        string reviewerId, string reason, CancellationToken cancellationToken)
    {
        using var response = await http.PatchAsJsonAsync(
            $"api/v1/filings/{filingId}/text-evidence/{chunk.Id}/review", new {
                decision, reviewer_id = reviewerId, reason, expected_text_sha256 = chunk.TextSha256,
            }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<long> UploadDocumentAsync(string filePath, string stockCode, string companyName,
        int reportYear, string reportPeriod, CancellationToken cancellationToken)
    {
        var (basis, end) = reportPeriod switch {
            "annual" => ("annual", $"{reportYear:D4}-12-31"),
            "semi_annual" => ("semi_annual", $"{reportYear:D4}-06-30"),
            "q1" => ("quarterly", $"{reportYear:D4}-03-31"),
            "q3" => ("quarterly", $"{reportYear:D4}-09-30"),
            _ => throw new ArgumentException("请选择年报、半年报、一季报或三季报。", nameof(reportPeriod)),
        };
        using var content = new MultipartFormDataContent();
        await using var stream = File.OpenRead(filePath);
        using var fileContent = new StreamContent(stream);
        fileContent.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("application/pdf");
        content.Add(fileContent, "file", Path.GetFileName(filePath));
        content.Add(new StringContent(stockCode), "stock_code");
        content.Add(new StringContent(companyName), "company_name");
        content.Add(new StringContent($"{reportYear:D4}-01-01"), "period_start");
        content.Add(new StringContent(end), "period_end");
        content.Add(new StringContent(basis), "report_type");
        content.Add(new StringContent(basis), "period_basis");
        using var response = await http.PostAsync("api/v1/filings", content, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("filing_version_id").GetInt64();
    }
}

public sealed record TextEvidencePage(JsonElement Coverage, TextEvidenceItem[] Hits);
public sealed record TextEvidenceItem(long Id, long FilingVersionId, int PageNo, string Section,
    string Text, string TextSha256, string SourceSha256, string ExtractionMethod,
    string? ReviewStatus = null)
{
    public string Label => $"第 {PageNo} 页 · {Section} · {ReviewStatus ?? "unreviewed"}";
}
