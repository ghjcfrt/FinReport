using System.Text.Json;

namespace FinReport.Desktop.Services;

public sealed partial class FinReportApiClient
{
    public async Task<JsonElement> ValidateNativeFactsAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/filings/{filingId}/auto-validation", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        using var body = JsonDocument.Parse(await response.Content.ReadAsStreamAsync(cancellationToken));
        return body.RootElement.GetProperty("data").Clone();
    }

    public async Task<string> GetValidatedChapterReportAsync(long runId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/chapter-analyses/{runId}/report.md", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        return await response.Content.ReadAsStringAsync(cancellationToken);
    }
}
