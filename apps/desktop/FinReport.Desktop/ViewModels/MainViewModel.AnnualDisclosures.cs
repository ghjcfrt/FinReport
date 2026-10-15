using System.Collections.ObjectModel;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FinReport.Desktop.Services;

namespace FinReport.Desktop.ViewModels;

public partial class MainViewModel
{
    public ObservableCollection<AnnualDisclosureItem> AnnualDisclosures { get; } = [];
    [ObservableProperty] private AnnualDisclosureItem? selectedAnnualDisclosure;
    [ObservableProperty] private string annualDisclosureSummary = "年度披露指标：提取完成后刷新并审核。";
    [ObservableProperty] private string annualDisclosureComparisons = "比较与比例核算：待加载。";
    private long _annualDisclosureFilingId;

    private void ClearAnnualDisclosures()
    {
        AnnualDisclosures.Clear();
        SelectedAnnualDisclosure = null;
        _annualDisclosureFilingId = 0;
        AnnualDisclosureSummary = "年度披露指标：等待本次提取完成。";
        AnnualDisclosureComparisons = "比较与比例核算：待加载。";
    }

    [RelayCommand]
    private async Task ExtractAnnualDisclosuresAsync()
    {
        if (IsBusy || _filingId == 0) return;
        IsBusy = true;
        try
        {
            await api.ExtractAnnualDisclosuresAsync(_filingId, CancellationToken.None);
            await LoadAnnualDisclosuresAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = $"补提取年度披露失败：{ex.Message}"; }
        finally { IsBusy = false; }
    }

    [RelayCommand]
    private async Task RefreshAnnualDisclosuresAsync()
    {
        if (IsBusy || _filingId == 0) return;
        IsBusy = true;
        try { await LoadAnnualDisclosuresAsync(CancellationToken.None); }
        catch (Exception ex) { ErrorMessage = $"年度披露指标读取失败：{ex.Message}"; }
        finally { IsBusy = false; }
    }

    private async Task LoadAnnualDisclosuresAsync(CancellationToken cancellationToken)
    {
        var filingId = _filingId;
        var context = await api.GetAnnualDisclosuresAsync(filingId, cancellationToken);
        if (filingId != _filingId) return;
        var selectedId = SelectedAnnualDisclosure?.Id;
        AnnualDisclosures.Clear();
        foreach (var item in context.Items ?? []) AnnualDisclosures.Add(item);
        _annualDisclosureFilingId = filingId;
        SelectedAnnualDisclosure = AnnualDisclosures.FirstOrDefault(item => item.Id == selectedId)
            ?? AnnualDisclosures.FirstOrDefault();
        AnnualDisclosureSummary = $"财报 {filingId} · {context.SourceKind} · {AnnualDisclosures.Count} 项年度披露候选。" +
            "这里保留公司披露口径，审核后也不称为已独立复算的加权指标。\n" + string.Join("\n", context.Limitations ?? []);
        AnnualDisclosureComparisons = FormatAnnualDisclosureComparisons(context);
    }

    private static string FormatAnnualDisclosureComparisons(AnnualDisclosureContext context)
    {
        var rows = new List<string>();
        var definitions = (context.Definitions ?? []).ToDictionary(item => item.Code);
        static string Read(JsonElement row, string key) => row.TryGetProperty(key, out var value)
            && value.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null) ? value.ToString() : "";
        static string Number(string raw, string unit) => decimal.TryParse(raw,
            System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var value)
            ? unit switch { "ratio" => $"{value * 100:0.####}%", "percentage_points" => $"{value:0.####} 个百分点",
                "currency_per_share" => $"{value:0.####} 元/股", "currency" => $"{value:N2} 元", _ => value.ToString("0.####") } : raw;
        if (context.DerivedComparisons.ValueKind == JsonValueKind.Array)
            foreach (var row in context.DerivedComparisons.EnumerateArray())
            {
                var code = Read(row, "code");
                definitions.TryGetValue(code, out var definition);
                var name = definition?.Name ?? code;
                var status = Read(row, "status") == "reviewed" ? "已审核" : "待审核算术";
                var growth = Read(row, "growth_rate");
                var differenceUnit = Read(row, "difference_unit");
                var difference = Read(row, differenceUnit == "percentage_points" ? "percentage_point_change" : "difference");
                rows.Add($"[{status}] {name}：本期 {Number(Read(row, "current_value"), definition?.Unit ?? "")}；" +
                    $"上期 {Number(Read(row, "prior_value"), definition?.Unit ?? "")}；差额 {Number(difference, differenceUnit)}；" +
                    (growth.Length == 0 ? "同比增长率不适用。" : $"同比 {Number(growth, "ratio")}。"));
            }
        if (context.Reconciliations.ValueKind == JsonValueKind.Array)
            foreach (var row in context.Reconciliations.EnumerateArray())
            {
                var status = Read(row, "status") == "reviewed" ? "已审核" : "待审核算术";
                rows.Add($"[{status}] 研发资本化率核算：计算值 {Number(Read(row, "calculated_value"), "ratio")}；" +
                    $"披露值 {Number(Read(row, "disclosed_value"), "ratio")}；" +
                    (row.TryGetProperty("matched", out var matched) && matched.ValueKind == JsonValueKind.True
                        ? "在披露精度容差内一致。" : "存在差异，请核对口径和原数。"));
            }
        return rows.Count == 0 ? "比较与比例核算：暂无配对输入。" : string.Join("\n\n", rows);
    }

    [RelayCommand]
    private Task ConfirmAnnualDisclosureAsync() => ReviewAnnualDisclosureAsync("confirmed");

    [RelayCommand]
    private Task RejectAnnualDisclosureAsync() => ReviewAnnualDisclosureAsync("rejected");

    private async Task ReviewAnnualDisclosureAsync(string decision)
    {
        if (IsBusy || SelectedAnnualDisclosure is not { } item) return;
        if (_annualDisclosureFilingId != _filingId || _filingId == 0)
        {
            ErrorMessage = "请先刷新当前财报的年度披露指标。";
            return;
        }
        if (string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(ReviewReason))
        {
            ErrorMessage = "请填写复核人标识和核对说明。";
            return;
        }
        IsBusy = true;
        try
        {
            await api.ReviewAnnualDisclosureAsync(_filingId, item.Id, ReviewerId.Trim(), decision,
                ReviewReason.Trim(), CancellationToken.None);
            await LoadAnnualDisclosuresAsync(CancellationToken.None);
        }
        catch (Exception ex) { ErrorMessage = $"年度披露指标审核失败：{ex.Message}"; }
        finally { IsBusy = false; }
    }
}
