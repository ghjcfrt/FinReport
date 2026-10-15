using System.Globalization;
using System.Net.Http.Json;
using System.Text.Json;

namespace FinReport.Desktop.Services;

public sealed partial class FinReportApiClient
{
    public async Task<AnnualDisclosureContext> MapOcrDisclosureAsync(long filingId,
        OcrDisclosureMappingRequest request, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsJsonAsync(
            $"api/v1/filings/{filingId}/ocr-annual-disclosure-mappings", request, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var body = await response.Content.ReadFromJsonAsync<ApiEnvelope<AnnualDisclosureContext>>(ApiJson, cancellationToken);
        return body?.Data ?? throw new InvalidOperationException("未读取到年度披露映射结果。");
    }

    public async Task ExtractAnnualDisclosuresAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.PostAsync($"api/v1/filings/{filingId}/annual-disclosures/extract", null, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }

    public async Task<AnnualDisclosureContext> GetAnnualDisclosuresAsync(long filingId, CancellationToken cancellationToken)
    {
        using var response = await http.GetAsync($"api/v1/filings/{filingId}/annual-disclosures", cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
        var body = await response.Content.ReadFromJsonAsync<ApiEnvelope<AnnualDisclosureContext>>(ApiJson, cancellationToken);
        return body?.Data ?? throw new InvalidOperationException("未读取到年度披露指标。");
    }

    public async Task ReviewAnnualDisclosureAsync(long filingId, string disclosureId, string reviewerId,
        string decision, string reason, CancellationToken cancellationToken)
    {
        using var response = await http.PatchAsJsonAsync(
            $"api/v1/filings/{filingId}/annual-disclosures/{Uri.EscapeDataString(disclosureId)}/review",
            new { reviewer_id = reviewerId, decision, reason }, ApiJson, cancellationToken);
        await EnsureSuccessAsync(response, cancellationToken);
    }
}

public sealed record OcrDisclosureMappingRequest(string Code, long TableId, long OcrCallId,
    string PageSha256, string ResponseSha256, int RowIndex, int ColumnIndex,
    string RawLabel, string RawValue, string? CorrectedRawValue, string ColumnLabel,
    string UnitText, long? UnitOcrCallId, string? UnitPageSha256, string? UnitResponseSha256,
    int UnitScale, string Currency, double[] ValueBbox, double[] RowBbox, double[] UnitBbox,
    string ReviewerId, string Reason);

public sealed record AnnualDisclosureContext(string SourceKind, long? ExtractionJobId,
    AnnualDisclosureItem[] Items, string[] Limitations, JsonElement DerivedComparisons,
    JsonElement Reconciliations, AnnualDisclosureDefinition[] Definitions);

public sealed record AnnualDisclosureDefinition(string Code, string Name, string Unit, string Section)
{
    public string Display => $"{Name} ({Code})";
}

public sealed record AnnualDisclosureItem(string Id, string Code, string Name, string Value,
    string Unit, string? Currency, string RawValue, string RawLabel, int PageNo, string PageSha256,
    string ColumnLabel, string PeriodStart, string PeriodEnd, string Basis, string Assurance,
    JsonElement Evidence, int ReviewCount, string ReviewStatus, string? Reason)
{
    public string ValueDisplay => decimal.TryParse(Value, NumberStyles.Float, CultureInfo.InvariantCulture, out var amount)
        ? Unit switch
        {
            "ratio" => $"{amount * 100:0.####}%",
            "currency_per_share" => $"{amount:0.####} 元/股",
            "currency" => $"{amount:N2} 元",
            _ => $"{Value} {Unit}",
        } : $"{Value} {Unit}";
    public string SourceDisplay => $"第 {PageNo} 页 · {ColumnLabel} · 原数 {RawValue}";
    public string PeriodDisplay => $"{PeriodStart} 至 {PeriodEnd}";
    public string ReviewDisplay => $"{ReviewStatus} · {ReviewCount}/2";
    public string EvidenceDisplay => JsonSerializer.Serialize(Evidence, new JsonSerializerOptions { WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping });
}
