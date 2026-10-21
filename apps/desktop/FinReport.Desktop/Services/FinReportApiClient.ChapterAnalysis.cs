using System.Net.Http.Json;
using System.Text.Json;

namespace FinReport.Desktop.Services;

public sealed partial class FinReportApiClient
{
    public async Task<long> StartChapterAnalysisAsync(long filingId, long[]? selectedDocumentIds,
        CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync($"api/v1/filings/{filingId}/chapter-analyses",
            new { selected_document_ids = selectedDocumentIds }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").GetProperty("analysis_run_id").GetInt64();
    }

    public async Task<ReportAnalysisStatus> GetChapterAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/chapter-analyses/{runId}", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var body = await response.Content.ReadFromJsonAsync<ApiEnvelope<ReportAnalysisStatus>>(ApiJson, cancellationToken);
        return body?.Data ?? throw new InvalidOperationException("未读取到章节分析结果。");
    }

    public async Task<JsonElement> GetChapterEventsAsync(long runId, long after, string level,
        CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/chapter-analyses/{runId}/events?after={after}&limit=250&min_level={Uri.EscapeDataString(level)}", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").Clone();
    }

    public async Task CancelChapterAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/chapter-analyses/{runId}/cancel", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<JsonElement> ReplayChapterAnalysisAsync(long runId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/chapter-analyses/{runId}/replay", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").Clone();
    }
}
