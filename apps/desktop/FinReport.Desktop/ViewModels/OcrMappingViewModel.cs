using System.Collections.ObjectModel;
using System.Globalization;
using System.IO;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FinReport.Desktop.Services;

namespace FinReport.Desktop.ViewModels;

public partial class OcrMappingViewModel(FinReportApiClient api, long filingId, OriginalTableItem table) : ObservableObject
{
    public OriginalTableItem Table { get; } = table;
    public string TableSummary => $"第 {Table.PageNo} 页 · 表 {Table.Id} · {Table.TableName}；来源 OCR 调用 {Table.OcrCallId}";
    public ObservableCollection<OcrMappingPage> Pages { get; } = [];
    public ObservableCollection<OcrMappingConcept> Concepts { get; } = [];
    public OcrSourceRow[] Rows { get; } = (table.Rows ?? []).Select((row, index) => new OcrSourceRow(index, row)).ToArray();
    public OcrSourceColumn[] Columns { get; } = (table.Columns ?? []).Select((label, index) => new OcrSourceColumn(index, label)).Where(column => column.Index > 0).ToArray();
    [ObservableProperty] private OcrMappingPage? selectedPage;
    [ObservableProperty] private OcrSourceRow? selectedRow;
    [ObservableProperty] private OcrSourceColumn? selectedColumn;
    [ObservableProperty] private OcrMappingConcept? selectedConcept;
    [ObservableProperty] private BitmapSource? pageImage;
    [ObservableProperty] private bool isBusy;
    [ObservableProperty] private string status = "请先载入当前 OCR 原图与映射上下文。";
    [ObservableProperty] private string error = "";
    [ObservableProperty] private string correctedRawValue = "";
    [ObservableProperty] private string unitText = "";
    [ObservableProperty] private int unitScale = 0;
    [ObservableProperty] private string periodRole = "current";
    [ObservableProperty] private string periodStart = "";
    [ObservableProperty] private string periodEnd = "";
    [ObservableProperty] private bool isRestatement;
    [ObservableProperty] private string tableName = table.TableName ?? "";
    [ObservableProperty] private string reviewerId = "";
    [ObservableProperty] private string reason = "";
    [ObservableProperty] private string auditQuote = "";
    [ObservableProperty] private string captureTarget = "value";
    [ObservableProperty] private string selectionSummary = "尚未框选金额、科目整行、单位。";
    private double[]? _valueBbox, _rowBbox, _unitBbox, _auditBbox;
    private OcrMappingPage? _unitPage;
    private OcrMappingContext? _context;
    private long _imageRequest;
    private long? _loadedCallId;

    public bool IsAvailable => !IsBusy;
    public string RawLabel => SelectedRow?.Source.RawLabel ?? SelectedRow?.Source.Cells?.FirstOrDefault() ?? "";
    public string RawValue => SelectedColumn is { } column && SelectedRow?.Source.Cells is { } cells && column.Index < cells.Length ? cells[column.Index] : "";
    public string ColumnLabel => SelectedColumn?.Label ?? "";
    public string SourceText => SelectedPage?.SourceText ?? "";
    public string PageFingerprint => SelectedPage is { } page ? $"{page.Width} × {page.Height} 像素；图像 SHA-256 {page.PageSha256}\nOCR 响应 SHA-256 {page.ResponseSha256}" : "";
    public bool IsMappingPage => SelectedPage?.OcrCallId == Table.OcrCallId && _loadedCallId == Table.OcrCallId;
    public string PageUseHint => IsMappingPage ? "当前原图对应候选表，可框选金额、整行、单位或审计意见。" : "此页可框选单位或审计意见；金额与整行须在候选表所在页框选。";

    partial void OnIsBusyChanged(bool value) => OnPropertyChanged(nameof(IsAvailable));
    partial void OnSelectedRowChanged(OcrSourceRow? value)
    {
        OnPropertyChanged(nameof(RawLabel)); OnPropertyChanged(nameof(RawValue));
        _valueBbox = _rowBbox = null; CorrectedRawValue = ""; UpdateSelectionSummary();
    }
    partial void OnSelectedColumnChanged(OcrSourceColumn? value)
    {
        OnPropertyChanged(nameof(RawValue)); OnPropertyChanged(nameof(ColumnLabel));
        _valueBbox = null; CorrectedRawValue = ""; UpdateSelectionSummary();
    }
    partial void OnPeriodRoleChanged(string value)
    {
        if (_context is null) return;
        var shift = value == "comparative" ? -1 : 0;
        PeriodStart = DateOnly.Parse(_context.Filing.PeriodStart, CultureInfo.InvariantCulture).AddYears(shift).ToString("yyyy-MM-dd");
        PeriodEnd = DateOnly.Parse(_context.Filing.PeriodEnd, CultureInfo.InvariantCulture).AddYears(shift).ToString("yyyy-MM-dd");
    }
    partial void OnSelectedPageChanged(OcrMappingPage? value)
    {
        _imageRequest++;
        PageImage = null; _loadedCallId = null;
        _auditBbox = null; AuditQuote = "";
        OnPropertyChanged(nameof(SourceText)); OnPropertyChanged(nameof(PageFingerprint));
        OnPropertyChanged(nameof(IsMappingPage)); OnPropertyChanged(nameof(PageUseHint)); UpdateSelectionSummary();
    }

    [RelayCommand]
    public async Task LoadContextAsync()
    {
        if (IsBusy) return;
        IsBusy = true; Error = "";
        try
        {
            var context = await api.GetOcrMappingContextAsync(filingId, CancellationToken.None);
            if (context.JobId != Table.ExtractionJobId || !context.Pages.Any(page => page.OcrCallId == Table.OcrCallId
                && page.PageSha256 == Table.PageSha256 && page.ResponseSha256 == Table.ResponseSha256))
                throw new InvalidOperationException("所选表格已不是当前 OCR 提取结果，请关闭窗口并刷新原表。");
            _context = context;
            _valueBbox = _rowBbox = _unitBbox = _auditBbox = null; _unitPage = null;
            UnitText = ""; UnitScale = 0;
            Concepts.Clear(); foreach (var concept in context.Concepts) Concepts.Add(concept);
            Pages.Clear(); foreach (var page in context.Pages) Pages.Add(page);
            SelectedPage = Pages.First(page => page.OcrCallId == Table.OcrCallId);
            PeriodRole = "current"; OnPeriodRoleChanged(PeriodRole);
            Status = $"当前任务 {context.JobId}；已映射 {context.MappedFactIds.Length} 个候选事实。请核对原图与原始 OCR。";
        }
        catch (Exception ex) { Error = ex.Message; }
        finally { IsBusy = false; }
        if (SelectedPage is not null && string.IsNullOrEmpty(Error)) await LoadPageAsync();
    }

    [RelayCommand]
    public async Task LoadPageAsync()
    {
        if (IsBusy || SelectedPage is not { } page) return;
        var requestId = ++_imageRequest;
        IsBusy = true; Error = "";
        try
        {
            var bytes = await api.GetOcrPageImageAsync(filingId, page, CancellationToken.None);
            using var stream = new MemoryStream(bytes);
            var bitmap = new BitmapImage(); bitmap.BeginInit(); bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.StreamSource = stream; bitmap.EndInit(); bitmap.Freeze();
            if (bitmap.PixelWidth != page.Width || bitmap.PixelHeight != page.Height)
                throw new InvalidOperationException("OCR 原图尺寸与提取记录不一致。");
            if (requestId != _imageRequest || SelectedPage?.OcrCallId != page.OcrCallId) return;
            PageImage = bitmap; _loadedCallId = page.OcrCallId;
            OnPropertyChanged(nameof(IsMappingPage)); OnPropertyChanged(nameof(PageUseHint));
        }
        catch (Exception ex) { Error = ex.Message; }
        finally { IsBusy = false; }
    }

    public void CaptureSelection(double[] bbox)
    {
        if (PageImage is null || SelectedPage is not { } page || _loadedCallId != page.OcrCallId || IsBusy) return;
        if (bbox.Length != 4 || bbox.Any(value => !double.IsFinite(value)) || bbox[0] < 0 || bbox[1] < 0
            || bbox[2] > page.Width || bbox[3] > page.Height || bbox[2] <= bbox[0] || bbox[3] <= bbox[1]) return;
        if (CaptureTarget == "audit") _auditBbox = bbox.ToArray();
        else if (CaptureTarget == "unit") { _unitBbox = bbox.ToArray(); _unitPage = page; }
        else if (IsMappingPage)
        {
            if (CaptureTarget == "value") _valueBbox = bbox.ToArray();
            if (CaptureTarget == "row") _rowBbox = bbox.ToArray();
        }
        UpdateSelectionSummary();
    }

    private void UpdateSelectionSummary()
    {
        static string Box(double[]? box) => box is null ? "未框选" : string.Join(", ", box.Select(value => value.ToString("0.0", CultureInfo.InvariantCulture)));
        SelectionSummary = $"表页 {Table.PageNo}：金额 [{Box(_valueBbox)}]；整行 [{Box(_rowBbox)}]；单位页 {_unitPage?.PageNo} [{Box(_unitBbox)}]；审计意见 [{Box(_auditBbox)}]（原图像素）";
    }

    [RelayCommand]
    public async Task SaveMappingAsync()
    {
        if (IsBusy) return;
        Error = "";
        var page = _context?.Pages.FirstOrDefault(item => item.OcrCallId == Table.OcrCallId);
        if (page is null || SelectedRow is null || SelectedColumn is null || SelectedConcept is null
            || _valueBbox is null || _rowBbox is null || _unitBbox is null || _unitPage is null || string.IsNullOrWhiteSpace(UnitText)
            || UnitScale == 0 || string.IsNullOrWhiteSpace(TableName) || string.IsNullOrWhiteSpace(RawValue)
            || string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(Reason))
        { Error = "请选择原始行、数值列和标准科目，框选金额/整行/单位，明确单位与期间，并填写登记人和说明。"; return; }
        IsBusy = true;
        try
        {
            var request = new OcrFactMappingRequest(Table.Id, page.OcrCallId, page.PageSha256, page.ResponseSha256,
                SelectedRow.Index, SelectedColumn.Index, SelectedConcept.Code, RawLabel, RawValue, UnitScale,
                "CNY", PeriodRole, PeriodStart, PeriodEnd, "consolidated", "PRC_ASBE", IsRestatement, TableName,
                ColumnLabel, _valueBbox, _rowBbox, UnitText, _unitBbox, ReviewerId.Trim(), Reason.Trim(),
                string.IsNullOrWhiteSpace(CorrectedRawValue) ? null : CorrectedRawValue.Trim(),
                _unitPage.OcrCallId, _unitPage.PageSha256, _unitPage.ResponseSha256);
            var result = await api.MapOcrFactAsync(filingId, request, CancellationToken.None);
            Status = $"已登记候选事实 {result.FactId}；财报状态 {result.FilingStatus}。请返回事实核对页完成双人审核。";
            _valueBbox = _rowBbox = null; CorrectedRawValue = ""; UpdateSelectionSummary();
        }
        catch (Exception ex) { Error = ex.Message; }
        finally { IsBusy = false; }
    }

    [RelayCommand]
    public async Task ConfirmAuditAsync()
    {
        if (IsBusy) return;
        Error = "";
        if (SelectedPage is not { } page || _loadedCallId != page.OcrCallId || _auditBbox is null
            || string.IsNullOrWhiteSpace(AuditQuote) || string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(Reason))
        { Error = "请载入审计意见所在原图，框选意见，并粘贴该页 OCR 原句，填写登记人和核对说明。"; return; }
        IsBusy = true;
        try
        {
            await api.ConfirmOcrAuditAsync(filingId, new OcrAuditConfirmation(page.OcrCallId, page.PageSha256,
                page.ResponseSha256, AuditQuote, _auditBbox, "standard_unqualified", ReviewerId.Trim(), Reason.Trim()), CancellationToken.None);
            Status = "审计意见来源已登记。该操作不替代事实审核；仅支持有原文证据的本年度合并报表标准无保留意见。";
        }
        catch (Exception ex) { Error = ex.Message; }
        finally { IsBusy = false; }
    }

    [RelayCommand]
    public async Task CompleteMappingAsync()
    {
        if (IsBusy) return;
        Error = "";
        if (_context is null || string.IsNullOrWhiteSpace(_context.Filing.FileSha256)
            || string.IsNullOrWhiteSpace(ReviewerId) || string.IsNullOrWhiteSpace(Reason))
        { Error = "请载入当前提取来源并填写实际登记人与归档说明；缺少文件指纹时需关闭并刷新窗口。"; return; }
        IsBusy = true;
        try
        {
            var result = await api.CompleteOcrMappingAsync(filingId, new OcrMappingCompletionRequest(_context.JobId,
                _context.Filing.FileSha256, ReviewerId.Trim(), Reason.Trim()), CancellationToken.None);
            Status = $"本次 OCR 映射已完成，归档 {result.ArchivedFactIds.Length} 个未采用的旧来源事实；历史证据与审批保留。当前 {result.CurrentFactIds.Length} 个候选仍须双人审核（{result.FilingStatus}）。";
        }
        catch (Exception ex) { Error = ex.Message; }
        finally { IsBusy = false; }
    }
}

public sealed record OcrSourceRow(int Index, OriginalTableRow Source)
{
    public string Display => $"行 {Index + 1} · {Source.RawLabel ?? Source.Cells?.FirstOrDefault()}";
}
public sealed record OcrSourceColumn(int Index, string Label)
{
    public string Display => $"列 {Index + 1} · {Label}";
}
