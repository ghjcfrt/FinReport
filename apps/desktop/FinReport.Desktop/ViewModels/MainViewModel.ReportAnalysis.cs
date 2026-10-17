using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FinReport.Desktop.Services;

namespace FinReport.Desktop.ViewModels;

public partial class MainViewModel
{
    [ObservableProperty] private string selectedPageNumbers = "";
    [ObservableProperty] private bool processWholeDocument;
    [ObservableProperty] private string jointReportDocumentIds = "";
    [ObservableProperty] private string reportDataSummary = "报告金额候选：请上传并解析报告，然后刷新。";
    [ObservableProperty] private string reportCalculations = "程序计算：尚未加载。";
    [ObservableProperty] private ReportMeasureRow? selectedReportMeasure;
    [ObservableProperty] private string reportAnalysisSummary = "报告试验分析：尚未启动。";
    [ObservableProperty] private string reportReplaySummary = "本地重放：尚未执行。";
    [ObservableProperty] private string reportReasoningSummary = "供应商思考文本：尚无已保存的模型调用。";
    public ObservableCollection<ReportMeasureRow> ReportMeasures { get; } = [];
    private long _reportDataFilingId;
    private long _reportAnalysisRunId;

    private int[]? ReadSelectedPageNumbers()
    {
        if (ProcessWholeDocument)
        {
            if (!string.IsNullOrWhiteSpace(SelectedPageNumbers))
                throw new InvalidOperationException("已选择整份报告，请先清空页码，或取消整份报告选项。");
            return null;
        }
        if (string.IsNullOrWhiteSpace(SelectedPageNumbers))
            throw new InvalidOperationException("请输入原始 PDF 页码，或显式勾选处理整份报告。");
        var pages = new SortedSet<int>();
        foreach (var raw in Regex.Split(SelectedPageNumbers.Trim(), @"[,，、;；\s]+"))
        {
            var range = raw.Split('-', '–', '—');
            if (range.Length is < 1 or > 2 || !int.TryParse(range[0], out var first) || first <= 0)
                throw new InvalidOperationException("页码格式不正确，例如：2,7-9,11（按 PDF 原始页码）。");
            var last = first;
            if (range.Length == 2 && (!int.TryParse(range[1], out last) || last < first))
                throw new InvalidOperationException("页码范围必须从小到大，例如 7-9。");
            if (last > 10000 || last - first > 1000)
                throw new InvalidOperationException("页码范围过大，请缩小范围后重试。");
            for (var page = first; page <= last; page++) pages.Add(page);
        }
        return pages.ToArray();
    }

    private void ClearReportData()
    {
        ReportMeasures.Clear();
        SelectedReportMeasure = null;
        _reportDataFilingId = 0;
        _reportAnalysisRunId = 0;
        ReportDataSummary = "报告金额候选：等待本次提取完成。";
        ReportCalculations = "程序计算：等待刷新。";
        ReportAnalysisSummary = "报告试验分析：尚未启动。";
        ReportReplaySummary = "本地重放：尚未执行。";
        ReportReasoningSummary = "供应商思考文本：尚无已保存的模型调用。";
    }

    [RelayCommand]
    private async Task RefreshReportDataAsync()
    {
        if (IsBusy || _filingId == 0) return;
        IsBusy = true;
        ErrorMessage = "";
        try { await LoadReportDataAsync(CancellationToken.None); }
        catch (Exception ex) { ErrorMessage = $"读取报告金额候选失败：{ex.Message}"; }
        finally { IsBusy = false; }
    }

    private async Task LoadReportDataAsync(CancellationToken cancellationToken)
    {
        var filingId = _filingId;
        var data = await api.GetReportDataAsync(filingId, cancellationToken);
        if (_filingId != filingId) return;
        var selected = SelectedReportMeasure?.Id;
        ReportMeasures.Clear();
        foreach (var item in data.Items ?? []) ReportMeasures.Add(new ReportMeasureRow(item));
        SelectedReportMeasure = ReportMeasures.FirstOrDefault(item => item.Id == selected) ?? ReportMeasures.FirstOrDefault();
        _reportDataFilingId = filingId;
        var scope = data.SelectedPageNumbers is null ? "整份报告" : $"原始 PDF 页 {string.Join(",", data.SelectedPageNumbers)}";
        ReportDataSummary = $"当前报告 ID {filingId} · {data.ReportType}/{data.PeriodBasis} · {data.PeriodStart} 至 {data.PeriodEnd} · {scope} · {ReportMeasures.Count} 项金额候选。\n"
            + string.Join("\n", data.Limitations ?? []);
        ReportCalculations = JsonSerializer.Serialize(new { data.Calculations, data.Issues }, new JsonSerializerOptions { WriteIndented = true });
    }

    [RelayCommand] private Task ConfirmReportMeasureAsync() => ReviewReportMeasureAsync("confirmed");
    [RelayCommand] private Task RejectReportMeasureAsync() => ReviewReportMeasureAsync("rejected");

    private async Task ReviewReportMeasureAsync(string decision)
    {
        if (IsBusy) return;
        if (SelectedReportMeasure is null || _reportDataFilingId != _filingId || _filingId == 0
            || string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(ReviewReason))
        { ErrorMessage = "请选择当前报告金额候选，并填写实际复核人标识与核对说明。"; return; }
        IsBusy = true;
        ErrorMessage = "";
        try
        {
            await api.ReviewReportDataAsync(_filingId, SelectedReportMeasure.Id, decision,
                ReviewerId.Trim(), ReviewReason.Trim(), CancellationToken.None);
            await LoadReportDataAsync(CancellationToken.None);
            ReportReplaySummary = "候选审核已改变；旧分析仅保留历史快照，请重新启动分析。";
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { IsBusy = false; }
    }

    [RelayCommand] private Task StartIndependentReportAnalysisAsync() => StartReportAnalysisAsync(false);
    [RelayCommand] private Task StartJointReportAnalysisAsync() => StartReportAnalysisAsync(true);

    private async Task StartReportAnalysisAsync(bool joint)
    {
        if (IsBusy || _filingId == 0) return;
        IsBusy = true;
        ErrorMessage = "";
        try
        {
            long[]? ids = null;
            if (joint)
            {
                var tokens = Regex.Split(JointReportDocumentIds.Trim(), @"[,，、;；\s]+");
                if (tokens.Any(value => !long.TryParse(value, out var id) || id <= 0))
                    throw new InvalidOperationException("联合分析需显式填写正整数报告 ID，例如 12,13,14,15。");
                ids = tokens.Select(long.Parse).Distinct().ToArray();
                if (ids.Length < 2 || !ids.Contains(_filingId))
                    throw new InvalidOperationException($"请显式选择至少两份报告，并包含当前报告 ID {_filingId}。");
            }
            using var cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(30));
            ReportReasoningSummary = "供应商思考文本：等待本次模型调用。";
            _reportAnalysisRunId = await api.StartReportAnalysisAsync(_filingId, ids, cancellation.Token);
            while (true)
            {
                var run = await api.GetReportAnalysisAsync(_reportAnalysisRunId, cancellation.Token);
                ReportAnalysisSummary = $"报告分析任务 {_reportAnalysisRunId} · {(joint ? "显式联合" : "当前报告独立")} · {run.Status}\n"
                    + (run.Output is JsonElement output ? JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true }) : "");
                ReportReasoningSummary = FormatProviderReasoning(run.ProviderReasoning);
                if (run.Status is "completed" or "invalidated" or "partial_result" or "waiting_for_documents") break;
                if (run.Status == "failed") throw new InvalidOperationException(run.FailureReason ?? "报告分析失败。");
                await Task.Delay(1000, cancellation.Token);
            }
            ReportReplaySummary = "可执行本地重放核验保存的输入、计算与模型响应；不会再次请求模型。";
        }
        catch (Exception ex) { ErrorMessage = ex.Message; }
        finally { IsBusy = false; }
    }

    private static string FormatProviderReasoning(ProviderReasoningView? view)
    {
        if (view?.Calls is not { Length: > 0 }) return "供应商思考文本：尚无已保存的模型调用。";
        return string.Join("\n\n", view.Calls.Select(call =>
        {
            var status = call.Status switch
            {
                "returned" => "已返回思考文本",
                "disabled" => "本次请求已关闭思考；未返回思考正文",
                _ => "供应商未返回思考正文"
            };
            return $"{call.Stage} · 第 {call.CallIndex} 次 · {status}\n"
                + string.Join("\n\n", (call.Fields ?? []).Select(field => $"字段：{field.Field}\n{field.Content}"));
        }));
    }

    [RelayCommand]
    private async Task ReplayReportAnalysisAsync()
    {
        if (IsBusy || _reportAnalysisRunId == 0) return;
        IsBusy = true;
        try
        {
            var replay = await api.ReplayReportAnalysisAsync(_reportAnalysisRunId, CancellationToken.None);
            ReportReplaySummary = JsonSerializer.Serialize(replay, new JsonSerializerOptions { WriteIndented = true });
        }
        catch (Exception ex) { ErrorMessage = $"本地重放失败：{ex.Message}"; }
        finally { IsBusy = false; }
    }
}

public sealed record ReportMeasureRow(JsonElement Source)
{
    private string Read(string key) => Source.TryGetProperty(key, out var value) ? value.ToString() : "";
    public string Id => Read("id");
    public string Label => Read("label");
    public string Amount => decimal.TryParse(Read("value"), NumberStyles.Float, CultureInfo.InvariantCulture, out var value) ? $"{value:N2} 元" : Read("value");
    public string Period => $"{Read("period_start")} 至 {Read("period_end")} / {Read("group")}";
    public string Location => $"第 {Read("page_no")} 页 · {Read("column_header")} · 原数 {Read("raw_value")} × {Read("source_unit_scale")}";
    public string Review => $"{Read("review_status")} · {Read("review_count")}/2";
    public string Evidence => JsonSerializer.Serialize(Source, new JsonSerializerOptions { WriteIndented = true });
}
