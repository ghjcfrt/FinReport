using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace FinReport.Desktop.Controls;

/// <summary>Selections use the OCR PNG's pixels, independent of window scaling or PDF text coordinates.</summary>
public sealed class OcrEvidenceImage : FrameworkElement
{
    public static readonly DependencyProperty SourceProperty = DependencyProperty.Register(nameof(Source), typeof(BitmapSource),
        typeof(OcrEvidenceImage), new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsMeasure | FrameworkPropertyMetadataOptions.AffectsRender,
            (owner, _) => ((OcrEvidenceImage)owner).ClearSelection()));
    public BitmapSource? Source { get => (BitmapSource?)GetValue(SourceProperty); set => SetValue(SourceProperty, value); }
    public event Action<double[]>? SelectionCommitted;
    private Point? _start;
    private Rect? _selection;
    private Rect ImageRect => Source is null ? Rect.Empty : new Rect(0, 0, ActualWidth, ActualWidth * Source.PixelHeight / Source.PixelWidth);

    public OcrEvidenceImage() { Cursor = Cursors.Cross; Focusable = true; ClipToBounds = true; }

    protected override Size MeasureOverride(Size availableSize)
    {
        var width = double.IsFinite(availableSize.Width) ? Math.Max(1, availableSize.Width) : 720;
        return new Size(width, Source is null ? 180 : width * Source.PixelHeight / Source.PixelWidth);
    }

    protected override void OnRender(DrawingContext drawingContext)
    {
        drawingContext.DrawRectangle(Brushes.WhiteSmoke, null, new Rect(RenderSize));
        if (Source is null) return;
        drawingContext.DrawImage(Source, ImageRect);
        if (_selection is Rect selection)
            drawingContext.DrawRectangle(new SolidColorBrush(Color.FromArgb(45, 0, 125, 205)), new Pen(Brushes.DeepSkyBlue, 2), selection);
    }

    protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        if (Source is null) return;
        _start = Clamp(e.GetPosition(this)); _selection = null;
        CaptureMouse(); e.Handled = true; InvalidateVisual();
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        if (_start is not Point start || !IsMouseCaptured) return;
        _selection = new Rect(start, Clamp(e.GetPosition(this))); InvalidateVisual();
    }

    protected override void OnMouseLeftButtonUp(MouseButtonEventArgs e)
    {
        if (_start is not Point start || Source is null) return;
        var end = Clamp(e.GetPosition(this)); _start = null; ReleaseMouseCapture();
        _selection = new Rect(start, end); InvalidateVisual(); e.Handled = true;
        var pixels = ToPixelBounds(ImageRect, Source.PixelWidth, Source.PixelHeight, start, end);
        if (pixels is not null) SelectionCommitted?.Invoke(pixels);
    }

    private Point Clamp(Point point) => new(Math.Clamp(point.X, 0, ImageRect.Width), Math.Clamp(point.Y, 0, ImageRect.Height));
    private void ClearSelection() { _start = null; _selection = null; ReleaseMouseCapture(); InvalidateVisual(); }

    public static double[]? ToPixelBounds(Rect imageRect, int pixelWidth, int pixelHeight, Point start, Point end)
    {
        if (imageRect.Width <= 0 || imageRect.Height <= 0 || pixelWidth <= 0 || pixelHeight <= 0) return null;
        var x0 = Math.Clamp((Math.Min(start.X, end.X) - imageRect.X) / imageRect.Width * pixelWidth, 0, pixelWidth);
        var y0 = Math.Clamp((Math.Min(start.Y, end.Y) - imageRect.Y) / imageRect.Height * pixelHeight, 0, pixelHeight);
        var x1 = Math.Clamp((Math.Max(start.X, end.X) - imageRect.X) / imageRect.Width * pixelWidth, 0, pixelWidth);
        var y1 = Math.Clamp((Math.Max(start.Y, end.Y) - imageRect.Y) / imageRect.Height * pixelHeight, 0, pixelHeight);
        return x1 - x0 < 1 || y1 - y0 < 1 ? null : [x0, y0, x1, y1];
    }
}
