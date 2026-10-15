using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FinReport.Desktop.Services;

namespace FinReport.Desktop.ViewModels;

public partial class OcrMappingViewModel
{
    public ObservableCollection<AnnualDisclosureDefinition> DisclosureDefinitions { get; } = [];
    [ObservableProperty] private AnnualDisclosureDefinition? selectedDisclosureDefinition;

    [RelayCommand]
    public async Task LoadDisclosureDefinitionsAsync()
    {
        if (IsBusy) return;
        IsBusy = true; Error = "";
        try
        {
            var context = await api.GetAnnualDisclosuresAsync(filingId, CancellationToken.None);
            DisclosureDefinitions.Clear();
            foreach (var definition in context.Definitions ?? []) DisclosureDefinitions.Add(definition);
            SelectedDisclosureDefinition = DisclosureDefinitions.FirstOrDefault();
            Status = "已加载年度披露定义。请核对原图、年度列与单位后登记，随后在年度披露指标页审核。";
        }
        catch (Exception ex) { Error = ex.Message; }
        finally { IsBusy = false; }
    }

    [RelayCommand]
    public async Task SaveDisclosureMappingAsync()
    {
        if (IsBusy) return;
        Error = "";
        var page = _context?.Pages.FirstOrDefault(item => item.OcrCallId == Table.OcrCallId);
        if (page is null || SelectedRow is null || SelectedColumn is null || SelectedDisclosureDefinition is null
            || _valueBbox is null || _rowBbox is null || _unitBbox is null || _unitPage is null
            || string.IsNullOrWhiteSpace(UnitText) || string.IsNullOrWhiteSpace(RawValue)
            || string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(Reason))
        {
            Error = "请选择披露指标、原始行和年度列，框选数值/整行/单位，并填写登记人和说明。";
            return;
        }
        var scale = SelectedDisclosureDefinition.Unit == "currency" ? UnitScale : 1;
        if (scale == 0) { Error = "请明确金额原文单位及倍率。"; return; }
        IsBusy = true;
        try
        {
            var request = new OcrDisclosureMappingRequest(SelectedDisclosureDefinition.Code, Table.Id,
                page.OcrCallId, page.PageSha256, page.ResponseSha256, SelectedRow.Index, SelectedColumn.Index,
                RawLabel, RawValue, string.IsNullOrWhiteSpace(CorrectedRawValue) ? null : CorrectedRawValue.Trim(),
                ColumnLabel, UnitText, _unitPage.OcrCallId, _unitPage.PageSha256, _unitPage.ResponseSha256,
                scale, "CNY", _valueBbox, _rowBbox, _unitBbox, ReviewerId.Trim(), Reason.Trim());
            var result = await api.MapOcrDisclosureAsync(filingId, request, CancellationToken.None);
            Status = $"已登记年度披露候选，当前共 {result.Items.Length} 项。请返回年度披露指标页刷新并审核。";
            _valueBbox = _rowBbox = null; CorrectedRawValue = ""; UpdateSelectionSummary();
        }
        catch (Exception ex) { Error = ex.Message; }
        finally { IsBusy = false; }
    }
}
