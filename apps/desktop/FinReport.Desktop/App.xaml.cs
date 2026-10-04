using System.Net.Http;
using System.Windows;
using FinReport.Desktop.Services;
using FinReport.Desktop.ViewModels;
using Microsoft.Extensions.DependencyInjection;

namespace FinReport.Desktop;

public partial class App : Application
{
    private ServiceProvider? _services;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var services = new ServiceCollection();
        services.AddHttpClient<FinReportApiClient>(client =>
        {
            client.BaseAddress = new Uri("http://127.0.0.1:8000/");
            client.Timeout = TimeSpan.FromSeconds(30);
        });
        services.AddSingleton<MainViewModel>();
        _services = services.BuildServiceProvider();
        var mainWindow = new MainWindow { DataContext = _services.GetRequiredService<MainViewModel>() };
        mainWindow.Show();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _services?.Dispose();
        base.OnExit(e);
    }
}
