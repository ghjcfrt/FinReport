using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace FinReport.Desktop;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        Loaded += async (_, _) =>
        {
            var auditList = FindVisualChild<ListView>(this);
            if (auditList is not null)
            {
                ScrollViewer.SetVerticalScrollBarVisibility(auditList, ScrollBarVisibility.Disabled);
                ScrollViewer.SetCanContentScroll(auditList, true);
            }
            if (DataContext is ViewModels.MainViewModel vm)
                await vm.LoadModelSettingsAsync();
        };
    }

    private static T? FindVisualChild<T>(DependencyObject root) where T : DependencyObject
    {
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
        {
            var child = VisualTreeHelper.GetChild(root, index);
            if (child is T match) return match;
            var nested = FindVisualChild<T>(child);
            if (nested is not null) return nested;
        }
        return null;
    }

    private void AnalysisApiKeyChanged(object sender, System.Windows.RoutedEventArgs e)
    {
        if (DataContext is ViewModels.MainViewModel vm && sender is System.Windows.Controls.PasswordBox box) vm.AnalysisApiKey = box.Password;
    }

    private void OcrApiKeyChanged(object sender, System.Windows.RoutedEventArgs e)
    {
        if (DataContext is ViewModels.MainViewModel vm && sender is System.Windows.Controls.PasswordBox box) vm.OcrApiKey = box.Password;
    }
}
