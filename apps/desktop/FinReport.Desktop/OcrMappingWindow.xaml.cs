using System.Windows;
using FinReport.Desktop.ViewModels;

namespace FinReport.Desktop;

public partial class OcrMappingWindow : Window
{
    public OcrMappingWindow(OcrMappingViewModel viewModel)
    {
        InitializeComponent(); DataContext = viewModel;
        EvidenceImage.SelectionCommitted += viewModel.CaptureSelection;
        Loaded += async (_, _) => await viewModel.LoadContextAsync();
    }
}
