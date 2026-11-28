using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using WechatMemory.NativeApp.Models;
using WechatMemory.NativeApp.ViewModels;

namespace WechatMemory.NativeApp;

public sealed partial class MessageCardView : UserControl
{
    public static readonly DependencyProperty CardProjectionProperty = DependencyProperty.Register(
        nameof(CardProjection), typeof(MessageProjection), typeof(MessageCardView),
        new PropertyMetadata(null, OnProjectionChanged));

    public MessageProjection? CardProjection
    {
        get => (MessageProjection?)GetValue(CardProjectionProperty);
        set => SetValue(CardProjectionProperty, value);
    }

    public MessageCardViewModel? Vm { get; private set; }

    public MessageCardView() => InitializeComponent();

    private static void OnProjectionChanged(DependencyObject sender, DependencyPropertyChangedEventArgs args)
    {
        var view = (MessageCardView)sender;
        // No global expansion cache keyed by possibly duplicate message IDs. A
        // recycled container must not inherit another row's expanded display.
        view.Vm = args.NewValue is MessageProjection projection ? new MessageCardViewModel(projection) : null;
        view.Bindings.Update();
    }
}
