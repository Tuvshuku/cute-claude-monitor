# claude-usage-bot -- Windows desktop pet + usage dashboard
#
#   powershell -ExecutionPolicy Bypass -File widget.ps1
#
# The bot wanders your desktop, hops, and blinks. Hover it and it jumps.
# Click it to pop the usage dashboard open, click again to fold it away.
# Drag it to move it. Right-click for the menu.

param(
    [string]$DataPath = "$env:USERPROFILE\.claude-widget\usage.json",
    [switch]$NativeMode,
    [string]$CollectorDataDir = ''
)

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Xaml
Add-Type -AssemblyName System.Windows.Forms

if ([string]::IsNullOrWhiteSpace($DataPath)) {
    $DataPath = Join-Path $env:USERPROFILE '.claude-widget\usage.json'
}

$script:SettingsPath = Join-Path $env:APPDATA 'ClaudeUsageBot\window.json'
$script:LegacySettingsPath = Join-Path $env:APPDATA 'CuteClaudeWidget\window.json'
$script:DataPath     = $DataPath
$script:ExplorerPath = Join-Path $env:SystemRoot 'explorer.exe'
$script:WslPath      = Join-Path $env:SystemRoot 'System32\wsl.exe'
$script:Snapshot     = $null
$script:LastError    = $null
$script:Mood         = ''
$script:BodyHex      = ''
$script:Open         = $false
$script:Wander       = $true

# motion state
$script:State    = 'idle'
$script:Tick     = 0
$script:NextAt   = 40
$script:Dir      = 1
$script:TargetX  = $null
$script:JumpT    = 0
$script:JumpLen  = 18
$script:Dragging = $false
$script:LastX    = 0
$script:HoverAt  = -999
$script:EyeKind  = ''
$script:LastHop  = 0
$script:WakeUntil = -1       # motion tick; dragging wakes a sleeping bot briefly

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
$script:Levels = @(
    @{ Max = 0.50; A = '#EFA07C'; B = '#D97757'; Mood = 'happy' }
    @{ Max = 0.75; A = '#EDA85C'; B = '#DC8A2E'; Mood = 'focus' }
    @{ Max = 0.90; A = '#E8834E'; B = '#CF6027'; Mood = 'warm'  }
    @{ Max = 99.0; A = '#E05C4C'; B = '#C0392B'; Mood = 'hot'   }
)
$script:IdleLevel = @{ A = '#CFB6A9'; B = '#B08F7E'; Mood = 'sleep' }
$script:DeadLevel = @{ A = '#C6BCB6'; B = '#9C918B'; Mood = 'lost'  }
$script:Level     = $script:Levels[0]

$CARD_A  = '#FFFFFF'
$CARD_B  = '#FBF6F3'
$SPARK_0 = '#EDE4DE'
$SPARK_1 = '#F2CDB9'

# ---------------------------------------------------------------------------
# XAML.  The pet is pixel art: flat rectangles, EdgeMode=Aliased for crisp
# edges, no gradients or effects -- cheap to redraw while it moves.
# ---------------------------------------------------------------------------
$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Claude Usage Bot" SizeToContent="WidthAndHeight"
        WindowStyle="None" AllowsTransparency="True" Background="Transparent"
        Topmost="True" ShowInTaskbar="False" ResizeMode="NoResize"
        UseLayoutRounding="True" SnapsToDevicePixels="True"
        FontFamily="Segoe UI">
  <StackPanel x:Name="Root" Background="Transparent">

    <!-- ================= the pet ================= -->
    <Canvas x:Name="BotHost" Width="112" Height="108" HorizontalAlignment="Left" Margin="18,0,0,0"
            RenderOptions.EdgeMode="Aliased">

      <Ellipse x:Name="Shade" Canvas.Left="26" Canvas.Top="92" Width="60" Height="10"
               Fill="#000000" Opacity="0.15" RenderTransformOrigin="0.5,0.5">
        <Ellipse.RenderTransform><ScaleTransform x:Name="ShadeScale"/></Ellipse.RenderTransform>
      </Ellipse>

      <Canvas x:Name="BotGroup" Width="112" Height="108" RenderTransformOrigin="0.5,0.9">
        <Canvas.RenderTransform>
          <TransformGroup>
            <ScaleTransform x:Name="BotFlip" ScaleX="1" ScaleY="1"/>
            <RotateTransform x:Name="BotTilt" Angle="0"/>
            <TranslateTransform x:Name="BotHop" Y="0"/>
          </TransformGroup>
        </Canvas.RenderTransform>

        <!-- legs -->
        <Rectangle x:Name="Leg0" Canvas.Left="23" Canvas.Top="77" Width="5" Height="13">
          <Rectangle.RenderTransform><TranslateTransform x:Name="LegT0"/></Rectangle.RenderTransform>
        </Rectangle>
        <Rectangle x:Name="Leg1" Canvas.Left="31" Canvas.Top="77" Width="5" Height="13">
          <Rectangle.RenderTransform><TranslateTransform x:Name="LegT1"/></Rectangle.RenderTransform>
        </Rectangle>
        <Rectangle x:Name="Leg2" Canvas.Left="67" Canvas.Top="77" Width="5" Height="13">
          <Rectangle.RenderTransform><TranslateTransform x:Name="LegT2"/></Rectangle.RenderTransform>
        </Rectangle>
        <Rectangle x:Name="Leg3" Canvas.Left="75" Canvas.Top="77" Width="5" Height="13">
          <Rectangle.RenderTransform><TranslateTransform x:Name="LegT3"/></Rectangle.RenderTransform>
        </Rectangle>

        <!-- side arms -->
        <Rectangle x:Name="ArmL" Canvas.Left="4"  Canvas.Top="53" Width="13" Height="13">
          <Rectangle.RenderTransform><TranslateTransform x:Name="ArmLT"/></Rectangle.RenderTransform>
        </Rectangle>
        <Rectangle x:Name="ArmR" Canvas.Left="92" Canvas.Top="53" Width="13" Height="13">
          <Rectangle.RenderTransform><TranslateTransform x:Name="ArmRT"/></Rectangle.RenderTransform>
        </Rectangle>

        <!-- body -->
        <Rectangle x:Name="Body" Canvas.Left="17" Canvas.Top="27" Width="75" Height="50"/>

        <!-- eyes: plain bars normally, chevrons when pleased -->
        <Rectangle x:Name="EyeL" Canvas.Left="31" Canvas.Top="39" Width="5" Height="13"
                   Fill="#1A1210" RenderTransformOrigin="0.5,0.5">
          <Rectangle.RenderTransform><ScaleTransform x:Name="EyeLScale"/></Rectangle.RenderTransform>
        </Rectangle>
        <Rectangle x:Name="EyeR" Canvas.Left="73" Canvas.Top="39" Width="5" Height="13"
                   Fill="#1A1210" RenderTransformOrigin="0.5,0.5">
          <Rectangle.RenderTransform><ScaleTransform x:Name="EyeRScale"/></Rectangle.RenderTransform>
        </Rectangle>
        <Path x:Name="HappyL" Canvas.Left="29" Canvas.Top="39" Stroke="#1A1210" StrokeThickness="4"
              StrokeLineJoin="Miter" Data="M 0,0 L 7,6 L 0,12" Visibility="Collapsed"/>
        <Path x:Name="HappyR" Canvas.Left="71" Canvas.Top="39" Stroke="#1A1210" StrokeThickness="4"
              StrokeLineJoin="Miter" Data="M 7,0 L 0,6 L 7,12" Visibility="Collapsed"/>

        <TextBlock x:Name="Zzz" Canvas.Left="94" Canvas.Top="14" Text="z" FontSize="13"
                   FontWeight="SemiBold" Opacity="0" Foreground="#B08F7E"/>
      </Canvas>
    </Canvas>

    <!-- ================= the dashboard ================= -->
    <Border x:Name="Card" CornerRadius="16" Padding="16,14,16,14" Width="380" Visibility="Collapsed"
            BorderThickness="1" BorderBrush="#EADFD8" Margin="0,-4,0,0">
      <Border.Effect>
        <DropShadowEffect BlurRadius="22" ShadowDepth="4" Direction="270" Opacity="0.20" Color="#6B4A38"/>
      </Border.Effect>
      <Grid>
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
          <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>

        <Grid Grid.Row="0">
          <StackPanel HorizontalAlignment="Left">
            <TextBlock x:Name="Title" Text="claude code" FontSize="14.5" FontWeight="SemiBold" Foreground="#241612"/>
            <TextBlock x:Name="Subtitle" Text="waiting for collector" FontSize="11"
                       Foreground="#8B7B72" Margin="0,1,0,0" TextTrimming="CharacterEllipsis"/>
          </StackPanel>
          <Border x:Name="Pill" CornerRadius="10" Padding="9,4" HorizontalAlignment="Right"
                  VerticalAlignment="Center" Background="#F4EDE8">
            <StackPanel Orientation="Horizontal">
              <Ellipse x:Name="StatusDot" Width="7" Height="7" VerticalAlignment="Center" Margin="0,0,6,0"/>
              <TextBlock x:Name="StatusText" Text="..." FontSize="11" FontWeight="SemiBold" Foreground="#4A3A32"/>
            </StackPanel>
          </Border>
        </Grid>

        <StackPanel Grid.Row="1" Margin="0,14,0,0">
          <Grid Margin="0,0,0,8">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="50"/><ColumnDefinition Width="*"/>
              <ColumnDefinition Width="42"/><ColumnDefinition Width="62"/>
            </Grid.ColumnDefinitions>
            <TextBlock x:Name="G0Label" Grid.Column="0" Text="session" FontSize="10.5" Foreground="#8B7B72" VerticalAlignment="Center"/>
            <Border x:Name="G0Track" Grid.Column="1" CornerRadius="5" Height="10" Background="#EFE7E2" ClipToBounds="True" VerticalAlignment="Center">
              <Border x:Name="G0Fill" CornerRadius="5" HorizontalAlignment="Left" Width="0"/>
            </Border>
            <TextBlock x:Name="G0Pct" Grid.Column="2" FontSize="10.5" FontWeight="SemiBold" HorizontalAlignment="Right" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBlock x:Name="G0Reset" Grid.Column="3" FontSize="9.5" Foreground="#A99B93" HorizontalAlignment="Right" VerticalAlignment="Center"/>
          </Grid>
          <Grid Margin="0,0,0,8">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="50"/><ColumnDefinition Width="*"/>
              <ColumnDefinition Width="42"/><ColumnDefinition Width="62"/>
            </Grid.ColumnDefinitions>
            <TextBlock x:Name="G1Label" Grid.Column="0" Text="week" FontSize="10.5" Foreground="#8B7B72" VerticalAlignment="Center"/>
            <Border x:Name="G1Track" Grid.Column="1" CornerRadius="5" Height="10" Background="#EFE7E2" ClipToBounds="True" VerticalAlignment="Center">
              <Border x:Name="G1Fill" CornerRadius="5" HorizontalAlignment="Left" Width="0"/>
            </Border>
            <TextBlock x:Name="G1Pct" Grid.Column="2" FontSize="10.5" FontWeight="SemiBold" HorizontalAlignment="Right" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBlock x:Name="G1Reset" Grid.Column="3" FontSize="9.5" Foreground="#A99B93" HorizontalAlignment="Right" VerticalAlignment="Center"/>
          </Grid>
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="50"/><ColumnDefinition Width="*"/>
              <ColumnDefinition Width="42"/><ColumnDefinition Width="62"/>
            </Grid.ColumnDefinitions>
            <TextBlock x:Name="G2Label" Grid.Column="0" Text="fable" FontSize="10.5" Foreground="#8B7B72" VerticalAlignment="Center"/>
            <Border x:Name="G2Track" Grid.Column="1" CornerRadius="5" Height="10" Background="#EFE7E2" ClipToBounds="True" VerticalAlignment="Center">
              <Border x:Name="G2Fill" CornerRadius="5" HorizontalAlignment="Left" Width="0"/>
            </Border>
            <TextBlock x:Name="G2Pct" Grid.Column="2" FontSize="10.5" FontWeight="SemiBold" HorizontalAlignment="Right" VerticalAlignment="Center" Margin="0,0,6,0"/>
            <TextBlock x:Name="G2Reset" Grid.Column="3" FontSize="9.5" Foreground="#A99B93" HorizontalAlignment="Right" VerticalAlignment="Center"/>
          </Grid>
          <TextBlock x:Name="UsageDetail" FontSize="9.5" Foreground="#A99B93" Margin="1,8,0,0"/>
        </StackPanel>

        <StackPanel Grid.Row="2" Margin="0,10,0,0">
          <StackPanel x:Name="Spark" Orientation="Horizontal" Height="26" VerticalAlignment="Bottom"/>
        </StackPanel>

        <Grid Grid.Row="3" Margin="1,9,1,0">
          <TextBlock x:Name="FooterLeft" FontSize="10.5" Foreground="#6E6058" HorizontalAlignment="Left"/>
          <TextBlock x:Name="FooterRight" FontSize="10.5" Foreground="#A99B93" HorizontalAlignment="Right"/>
        </Grid>
      </Grid>
    </Border>
  </StackPanel>
</Window>
'@

$reader = New-Object System.Xml.XmlNodeReader ([xml]$xaml)
$window = [Windows.Markup.XamlReader]::Load($reader)

$ui = @{}
foreach ($n in 'Root','BotHost','BotGroup','Shade','ShadeScale','BotFlip','BotTilt','BotHop',
               'Leg0','Leg1','Leg2','Leg3','LegT0','LegT1','LegT2','LegT3',
               'ArmL','ArmR','ArmLT','ArmRT','Body',
               'EyeL','EyeR','EyeLScale','EyeRScale','HappyL','HappyR','Zzz',
               'Card','Title','Subtitle','Pill','StatusDot','StatusText',
               'G0Label','G0Track','G0Fill','G0Pct','G0Reset',
               'G1Label','G1Track','G1Fill','G1Pct','G1Reset',
               'G2Label','G2Track','G2Fill','G2Pct','G2Reset',
               'UsageDetail','Spark','FooterLeft','FooterRight') {
    $ui[$n] = $window.FindName($n)
}
$script:BodyParts = @($ui.Body, $ui.ArmL, $ui.ArmR, $ui.Leg0, $ui.Leg1, $ui.Leg2, $ui.Leg3)

# SystemParameters.WorkArea only describes the primary display. Convert the
# WinForms work area for the monitor containing this WPF window back into WPF
# device-independent coordinates so wandering and clamping stay on that screen.
function Get-CurrentWorkArea {
    $fallback = [System.Windows.SystemParameters]::WorkArea
    try {
        $helper = New-Object System.Windows.Interop.WindowInteropHelper $window
        if ($helper.Handle -eq [IntPtr]::Zero) { return $fallback }

        $bounds = [System.Windows.Forms.Screen]::FromHandle($helper.Handle).WorkingArea
        $source = [System.Windows.PresentationSource]::FromVisual($window)
        if (-not $source -or -not $source.CompositionTarget) { return $fallback }

        $toDip = $source.CompositionTarget.TransformFromDevice
        $topLeft = $toDip.Transform((New-Object System.Windows.Point $bounds.Left, $bounds.Top))
        $bottomRight = $toDip.Transform((New-Object System.Windows.Point $bounds.Right, $bounds.Bottom))
        return New-Object System.Windows.Rect (
            $topLeft.X, $topLeft.Y,
            ($bottomRight.X - $topLeft.X), ($bottomRight.Y - $topLeft.Y))
    } catch {
        return $fallback
    }
}

# ---------------------------------------------------------------------------
# Helpers.  Brushes are cached and frozen -- Update-Widget touches a few dozen
# of them every refresh, and re-allocating each time showed up while dragging.
# ---------------------------------------------------------------------------
$script:BrushCache = @{}
function ConvertTo-Brush([string]$hex) {
    if (-not $script:BrushCache.ContainsKey($hex)) {
        $b = New-Object System.Windows.Media.SolidColorBrush (
            [System.Windows.Media.ColorConverter]::ConvertFromString($hex))
        $b.Freeze()
        $script:BrushCache[$hex] = $b
    }
    $script:BrushCache[$hex]
}
$script:GradCache = @{}
function New-Gradient([string]$a, [string]$b) {
    $key = "$a|$b"
    if (-not $script:GradCache.ContainsKey($key)) {
        $br = New-Object System.Windows.Media.LinearGradientBrush
        $br.StartPoint = New-Object System.Windows.Point 0, 0
        $br.EndPoint   = New-Object System.Windows.Point 1, 1
        $br.GradientStops.Add((New-Object System.Windows.Media.GradientStop(
            [System.Windows.Media.ColorConverter]::ConvertFromString($a), 0)))
        $br.GradientStops.Add((New-Object System.Windows.Media.GradientStop(
            [System.Windows.Media.ColorConverter]::ConvertFromString($b), 1)))
        $br.Freeze()
        $script:GradCache[$key] = $br
    }
    $script:GradCache[$key]
}
function Format-Tokens([double]$n) {
    if ($n -ge 1e9) { return ('{0:N2}B' -f ($n / 1e9)) }
    if ($n -ge 1e6) { return ('{0:N1}M' -f ($n / 1e6)) }
    if ($n -ge 1e3) { return ('{0:N1}k' -f ($n / 1e3)) }
    return ('{0:N0}' -f $n)
}
function Format-Duration([double]$s) {
    if ($s -lt 0) { $s = 0 }
    $t = [TimeSpan]::FromSeconds($s)
    # Use TimeSpan components, never [int]$t.TotalHours -- PowerShell's [int]
    # rounds rather than truncates, which rendered 1h45m as "2h 45m".
    if ($t.TotalDays -ge 1)    { return ('{0}d {1}h' -f $t.Days, $t.Hours) }
    if ($t.TotalHours -ge 1)   { return ('{0}h {1:00}m' -f $t.Hours, $t.Minutes) }
    if ($t.TotalMinutes -ge 1) { return ('{0}m' -f $t.Minutes) }
    return ('{0}s' -f $t.Seconds)
}
function Get-Level([double]$p) {
    foreach ($l in $script:Levels) { if ($p -lt $l.Max) { return $l } }
    return $script:Levels[-1]
}

# ---------------------------------------------------------------------------
# Bot look
# ---------------------------------------------------------------------------
function Set-Eyes([string]$kind) {
    if ($script:EyeKind -eq $kind) { return }
    $script:EyeKind = $kind
    $prop = [System.Windows.Media.ScaleTransform]::ScaleYProperty

    if ($kind -eq 'happy') {
        $ui.EyeL.Visibility = 'Collapsed'; $ui.EyeR.Visibility = 'Collapsed'
        $ui.HappyL.Visibility = 'Visible'; $ui.HappyR.Visibility = 'Visible'
        $ui.EyeLScale.BeginAnimation($prop, $null)
        $ui.EyeRScale.BeginAnimation($prop, $null)
        return
    }

    $ui.HappyL.Visibility = 'Collapsed'; $ui.HappyR.Visibility = 'Collapsed'
    $ui.EyeL.Visibility = 'Visible';     $ui.EyeR.Visibility = 'Visible'

    if ($kind -eq 'shut') {
        $ui.EyeLScale.BeginAnimation($prop, $null)
        $ui.EyeRScale.BeginAnimation($prop, $null)
        $ui.EyeLScale.ScaleY = 0.16
        $ui.EyeRScale.ScaleY = 0.16
        return
    }

    # 'open' -- blink on a slow loop
    $period = 4.4
    $make = {
        $a = New-Object System.Windows.Media.Animation.DoubleAnimationUsingKeyFrames
        $a.Duration = [Windows.Duration][TimeSpan]::FromSeconds($period)
        $a.RepeatBehavior = [System.Windows.Media.Animation.RepeatBehavior]::Forever
        # Parenthesise every subtraction: in PowerShell the comma binds tighter
        # than "-", so @($period - 0.2, 1) parses as $period - (0.2, 1).
        foreach ($kf in @(@(0.0, 1.0), @(($period - 0.22), 1.0),
                          @(($period - 0.14), 0.1), @(($period - 0.04), 1.0))) {
            $f = New-Object System.Windows.Media.Animation.LinearDoubleKeyFrame
            $f.KeyTime = [System.Windows.Media.Animation.KeyTime]::FromTimeSpan(
                [TimeSpan]::FromSeconds($kf[0]))
            $f.Value = $kf[1]
            $a.KeyFrames.Add($f) | Out-Null
        }
        $a
    }
    $ui.EyeLScale.BeginAnimation($prop, (& $make))
    $ui.EyeRScale.BeginAnimation($prop, (& $make))
}

function Set-Mood([string]$mood, [string]$hex) {
    $previousMood = $script:Mood
    $script:Mood = $mood
    if ($script:BodyHex -ne $hex) {          # only repaint when the colour moves
        $script:BodyHex = $hex
        $solid = ConvertTo-Brush $hex
        foreach ($p in $script:BodyParts) { $p.Fill = $solid }
    }
    $ui.Zzz.Opacity = $(if ($mood -eq 'sleep') { 0.9 } else { 0.0 })

    # Sleep/offline are real motion states, not just a palette swap.  Without
    # this, a bot that had gone to sleep could finish its walk or jump while
    # showing shut eyes and a z.
    if (-not $script:Dragging -and $mood -in @('sleep', 'lost')) {
        $script:State = 'idle'
        Reset-Pose
    } elseif ($previousMood -in @('sleep', 'lost') -and $mood -notin @('sleep', 'lost')) {
        # Wake into a calm idle instead of immediately consuming an overdue
        # wander deadline accumulated during a long sleep.
        $script:State = 'idle'
        Reset-Pose
        $script:NextAt = $script:Tick + 30
    }
    Sync-EyesToMood
}

function Sync-EyesToMood {
    if ($script:Dragging) { Set-Eyes 'happy'; return }
    switch ($script:Mood) {
        'sleep' { Set-Eyes 'shut' }
        'lost'  { Set-Eyes 'shut' }
        'happy' { Set-Eyes 'open' }
        default { Set-Eyes 'open' }
    }
}

# sparkline bars
$script:SparkBars = @()
for ($i = 0; $i -lt 24; $i++) {
    $b = New-Object System.Windows.Controls.Border
    $b.Width = 11; $b.Height = 2
    $b.CornerRadius = New-Object System.Windows.CornerRadius 3
    $b.Margin = New-Object System.Windows.Thickness 1, 0, 1, 0
    $b.VerticalAlignment = 'Bottom'
    $b.Background = (ConvertTo-Brush $SPARK_0)
    $ui.Spark.Children.Add($b) | Out-Null
    $script:SparkBars += $b
}

function Read-Snapshot {
    if (-not (Test-Path -LiteralPath $script:DataPath)) { return $null }
    for ($i = 0; $i -lt 3; $i++) {
        try {
            $raw = Get-Content -LiteralPath $script:DataPath -Raw -ErrorAction Stop
            if ($raw) { return $raw | ConvertFrom-Json }
        } catch { Start-Sleep -Milliseconds 40 }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Dashboard render
# ---------------------------------------------------------------------------
function Set-Gauge([int]$i, $g, $accent, [double]$elapsed = 0) {
    $track = $ui["G$($i)Track"]; $fill = $ui["G$($i)Fill"]
    $pctTb = $ui["G$($i)Pct"];   $resTb = $ui["G$($i)Reset"]; $lbl = $ui["G$($i)Label"]
    if (-not $g) {
        $fill.Width = 0; $pctTb.Text = ''; $resTb.Text = ''; $lbl.Text = ''
        return
    }

    $lbl.Text = [string]$g.label
    $pct = [double]$g.pct
    $w = $track.ActualWidth
    if ($w -le 0) { $w = 180 }
    $fill.Background = $accent
    $fill.Width = [Math]::Max(([Math]::Min($pct, 1.0) * $w), 0)

    # "~" is a local estimate; "*" is an exact server reading that is stale.
    $prefix = ''
    $suffix = ''
    if ($g.source -eq 'estimated') { $prefix = '~' }
    if ($g.source -eq 'last_live') { $suffix = '*' }
    $pctTb.Text = ('{0}{1:N0}%{2}' -f $prefix, ($pct * 100), $suffix)
    $pctTb.Foreground = ConvertTo-Brush (Get-Level $pct).B

    if ($null -ne $g.resets_in)  { $resTb.Text = Format-Duration ([double]$g.resets_in - $elapsed) }
    elseif ($g.id -eq 'session') { $resTb.Text = 'ready' }
    else                         { $resTb.Text = 'rolling' }
}

function Update-Widget {
    $snap = Read-Snapshot
    if ($snap) { $script:Snapshot = $snap }
    $snap = $script:Snapshot

    if (-not $snap) {
        $lvl = $script:DeadLevel
        $script:Level = $lvl
        $ui.Card.Background = New-Gradient $CARD_A $CARD_B
        Set-Mood 'lost' $lvl.B
        $ui.Subtitle.Text = 'no data yet - start the collector'
        $ui.StatusText.Text = 'offline'
        $ui.StatusDot.Fill = ConvertTo-Brush $lvl.B
        return
    }

    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    $age = $now - [double]$snap.generated_at
    if ($age -lt 0) { $age = 0 }
    $stale = $age -gt 45

    $gauges = @($snap.gauges)
    $session = $gauges | Where-Object { $_.id -eq 'session' } | Select-Object -First 1
    $top = 0.0
    foreach ($g in $gauges) { if ([double]$g.pct -gt $top) { $top = [double]$g.pct } }
    $interactionAwake = $script:Tick -lt $script:WakeUntil

    if ($stale) { $lvl = $script:DeadLevel }
    elseif ($snap.status -eq 'idle' -and -not $interactionAwake) { $lvl = $script:IdleLevel }
    else { $lvl = Get-Level $top }
    $script:Level = $lvl

    $accent = New-Gradient $lvl.A $lvl.B
    $ui.Card.Background = New-Gradient $CARD_A $CARD_B
    Set-Mood $lvl.Mood $lvl.B

    $ui.StatusDot.Fill = ConvertTo-Brush $lvl.B
    if ($stale) {
        $ui.StatusText.Text = 'offline'
        $ui.Subtitle.Text = ('collector silent for {0}' -f (Format-Duration $age))
    } elseif ($snap.status -eq 'idle') {
        if ($interactionAwake) {
            $ui.StatusText.Text = 'awake'
            $ui.Subtitle.Text = 'woken up by you'
        } elseif ($null -ne $snap.idle_seconds) {
            $ui.StatusText.Text = 'idle'
            $ui.Subtitle.Text = ('resting for {0}' -f (Format-Duration ([double]$snap.idle_seconds)))
        } else {
            $ui.StatusText.Text = 'idle'
            $ui.Subtitle.Text = 'waiting for first activity'
        }
    } else {
        $ui.StatusText.Text = 'active'
        $model = 'claude'
        if ($snap.models -and @($snap.models).Count -gt 0) {
            $model = (@($snap.models)[0].model -replace '^claude-', '')
            if (@($snap.models)[0].speed -eq 'fast') { $model += ' fast' }
        }
        $ui.Subtitle.Text = ('{0} - {1}/min' -f $model, (Format-Tokens ([double]$snap.burn.tokens_per_min)))
    }

    for ($i = 0; $i -lt 3; $i++) {
        $g = $null
        if ($i -lt $gauges.Count) { $g = $gauges[$i] }
        $el = 0
        if ($g -and $g.id -eq 'session' -and -not $stale) { $el = $age }
        Set-Gauge $i $g $accent $el
    }

    if ($session) {
        if ($session.source -eq 'live') {
            $d = ('live from your account  -  {0} tokens counted locally this session' -f $session.used_label)
        } elseif ($session.source -eq 'last_live') {
            $syncAge = Format-Duration ([double]$snap.live.age_seconds)
            $d = ('last account sync {0} ago (*)  -  {1} tokens counted locally' -f $syncAge, $session.used_label)
        } elseif ($session.source -eq 'configured') {
            $d = ('{0} / {1} tokens this session  -  calibrated' -f $session.used_label, $session.limit_label)
        } else {
            $d = ('{0} / {1} tokens this session  -  estimated' -f $session.used_label, $session.limit_label)
        }
        if ($session.source -notin @('live', 'last_live') -and
            $snap.live -and -not $snap.live.ok -and $snap.live.error) {
            $d = ('local estimate  -  live sync: {0}' -f $snap.live.error)
        }
        $ui.UsageDetail.Text = $d
    }

    $values = @($snap.sparkline)
    $max = 0.0
    foreach ($v in $values) { if ([double]$v -gt $max) { $max = [double]$v } }
    for ($i = 0; $i -lt $script:SparkBars.Count; $i++) {
        $v = 0.0
        if ($i -lt $values.Count) { $v = [double]$values[$i] }
        if ($max -gt 0) { $h = [Math]::Max(2.0, (($v / $max) * 24.0)) } else { $h = 2.0 }
        $script:SparkBars[$i].Height = $h
        if ($i -eq $script:SparkBars.Count - 1) { $script:SparkBars[$i].Background = $accent }
        elseif ($v -gt 0) { $script:SparkBars[$i].Background = ConvertTo-Brush $SPARK_1 }
        else { $script:SparkBars[$i].Background = ConvertTo-Brush $SPARK_0 }
    }

    $n = [int]$snap.today.sessions
    $plural = 's'; if ($n -eq 1) { $plural = '' }
    $ui.FooterLeft.Text  = ('today {0} - {1} session{2}' -f $snap.today.tokens_label, $n, $plural)
    $ui.FooterRight.Text = ('~${0:N2} api-equiv' -f [double]$snap.today.cost_usd)
}

# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------
function Reset-Pose {
    $ui.LegT0.Y = 0; $ui.LegT1.Y = 0; $ui.LegT2.Y = 0; $ui.LegT3.Y = 0
    $ui.ArmLT.Y = 0; $ui.ArmRT.Y = 0
    $ui.BotTilt.Angle = 0
    $ui.BotHop.Y = 0
    $script:LastHop = 0
    $ui.ShadeScale.ScaleX = 1; $ui.ShadeScale.ScaleY = 1
    $ui.Shade.Opacity = 0.15
}

function Test-IsDormant {
    return ($script:Mood -in @('sleep', 'lost'))
}

function Wake-BotFromDrag {
    if ($script:Mood -ne 'sleep') { return }
    # About twelve seconds: long enough for the reaction to read clearly, but
    # short enough that account inactivity still returns it to sleep naturally.
    $script:WakeUntil = $script:Tick + 360
    Update-Widget
}

function Start-Jump {
    if ($script:State -eq 'jump' -or $script:Dragging -or (Test-IsDormant)) { return }
    $script:State = 'jump'
    $script:JumpT = 0
    Set-Eyes 'happy'
}

function Step-Motion {
    $script:Tick++

    # --- dragging: the OS moves the window; we just animate the flail ---
    if ($script:Dragging) {
        $dx = $window.Left - $script:LastX
        $script:LastX = $window.Left
        $ui.BotTilt.Angle = [Math]::Max(-14, [Math]::Min(14, (-$dx * 1.2)))
        $wig = [Math]::Sin($script:Tick * 1.3) * 4
        $ui.ArmLT.Y = $wig
        $ui.ArmRT.Y = -$wig
        $ui.LegT0.Y = $wig;  $ui.LegT1.Y = -$wig
        $ui.LegT2.Y = -$wig; $ui.LegT3.Y = $wig
        $ui.ShadeScale.ScaleX = 0.5; $ui.ShadeScale.ScaleY = 0.5
        $ui.Shade.Opacity = 0.06
        return
    }

    # A sleeping/offline bot is completely still. The dashboard and wander
    # toggle stop autonomous motion too, but still allow an explicit hover/menu
    # jump to finish.
    if (Test-IsDormant) {
        if ($script:State -ne 'idle' -or $script:LastHop -ne 0) {
            $script:State = 'idle'
            Reset-Pose
        }
        return
    }
    if (($script:Open -or -not $script:Wander) -and $script:State -ne 'jump') {
        if ($script:State -ne 'idle' -or $script:LastHop -ne 0) {
            $script:State = 'idle'
            Reset-Pose
        }
        return
    }

    switch ($script:State) {
        'walk' {
            $work = Get-CurrentWorkArea
            $minX = $work.Left + 4
            $maxX = $work.Right - $window.ActualWidth - 4
            $window.Left = $window.Left + ($script:Dir * 2.4)
            if ($window.Left -lt $minX) { $window.Left = $minX; $script:Dir = 1; $ui.BotFlip.ScaleX = 1 }
            if ($window.Left -gt $maxX) { $window.Left = $maxX; $script:Dir = -1; $ui.BotFlip.ScaleX = -1 }

            $phase = $script:Tick * 0.45
            $s = [Math]::Sin($phase) * 3
            $ui.LegT0.Y = $s;  $ui.LegT1.Y = -$s
            $ui.LegT2.Y = -$s; $ui.LegT3.Y = $s
            $ui.BotHop.Y = -[Math]::Abs([Math]::Sin($phase)) * 1.6

            if (($null -ne $script:TargetX -and
                 [Math]::Abs(($window.Left - $script:TargetX)) -lt 5) -or $script:Tick -ge $script:NextAt) {
                $script:State = 'idle'; Reset-Pose
                $script:NextAt = $script:Tick + (Get-Random -Minimum 30 -Maximum 90)
            }
        }
        'jump' {
            $script:JumpT++
            $prog = $script:JumpT / $script:JumpLen
            $arc = [Math]::Sin([Math]::PI * $prog)
            $ui.BotHop.Y = -26 * $arc
            $ui.LegT0.Y = 3 * $arc; $ui.LegT1.Y = 3 * $arc
            $ui.LegT2.Y = 3 * $arc; $ui.LegT3.Y = 3 * $arc
            $ui.ArmLT.Y = -3 * $arc; $ui.ArmRT.Y = -3 * $arc
            $ui.ShadeScale.ScaleX = 1 - (0.4 * $arc)
            $ui.ShadeScale.ScaleY = 1 - (0.4 * $arc)
            $ui.Shade.Opacity = 0.15 - (0.09 * $arc)
            if ($script:JumpT -ge $script:JumpLen) {
                $script:State = 'idle'
                Reset-Pose
                Sync-EyesToMood
                $script:NextAt = $script:Tick + (Get-Random -Minimum 25 -Maximum 80)
            }
        }
        default {
            # Quantise the idle breathe to whole pixels. Assigning a fresh
            # sub-pixel value every frame repainted the layered window 30x a
            # second for a change nobody can see.
            $y = [Math]::Round(([Math]::Sin($script:Tick * 0.09) * 1.5))
            if ($y -ne $script:LastHop) { $script:LastHop = $y; $ui.BotHop.Y = $y }
            if ($script:Tick -ge $script:NextAt) {
                $work = Get-CurrentWorkArea
                $minX = $work.Left + 4
                $maxX = $work.Right - $window.ActualWidth - 4
                $roll = Get-Random -Minimum 0 -Maximum 100
                if ($roll -lt 25) {
                    Start-Jump
                } elseif ($roll -lt 80) {
                    $span = Get-Random -Minimum 70 -Maximum 280
                    $dir = 1
                    if ((Get-Random -Minimum 0 -Maximum 2) -eq 0) { $dir = -1 }
                    $script:TargetX = [Math]::Max($minX, [Math]::Min($maxX, ($window.Left + ($dir * $span))))
                    if ($script:TargetX -lt $window.Left) { $script:Dir = -1; $ui.BotFlip.ScaleX = -1 }
                    else { $script:Dir = 1; $ui.BotFlip.ScaleX = 1 }
                    $script:State = 'walk'
                    $script:NextAt = $script:Tick + 130
                } else {
                    $script:NextAt = $script:Tick + (Get-Random -Minimum 40 -Maximum 120)
                }
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Dashboard open/close
# ---------------------------------------------------------------------------
function Set-Dashboard([bool]$open) {
    $script:Open = $open
    if ($open) {
        $ui.Card.Visibility = 'Visible'
        $script:State = 'idle'
        Reset-Pose
        Update-Widget
        $window.UpdateLayout()
        $work = Get-CurrentWorkArea
        if ($window.Left + $window.ActualWidth -gt $work.Right) { $window.Left = $work.Right - $window.ActualWidth - 4 }
        if ($window.Left -lt $work.Left) { $window.Left = $work.Left + 4 }
        if ($window.Top + $window.ActualHeight -gt $work.Bottom) { $window.Top = $work.Bottom - $window.ActualHeight - 4 }
        if ($window.Top -lt $work.Top) { $window.Top = $work.Top + 4 }
    } else {
        $ui.Card.Visibility = 'Collapsed'
        $window.UpdateLayout()
    }
    Save-Settings
}

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
function Save-Settings {
    try {
        $dir = Split-Path $script:SettingsPath -Parent
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        [pscustomobject]@{
            Left = $window.Left; Top = $window.Top
            Open = $script:Open; Wander = $script:Wander
        } | ConvertTo-Json | Set-Content -LiteralPath $script:SettingsPath -Encoding UTF8
    } catch { $script:LastError = $_ }
}
function Restore-Settings {
    try {
        if ((-not (Test-Path -LiteralPath $script:SettingsPath)) -and
            (Test-Path -LiteralPath $script:LegacySettingsPath)) {
            $dir = Split-Path $script:SettingsPath -Parent
            if (-not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Path $dir -Force | Out-Null
            }
            Copy-Item -LiteralPath $script:LegacySettingsPath -Destination $script:SettingsPath
        }
        if (Test-Path -LiteralPath $script:SettingsPath) {
            $s = Get-Content -LiteralPath $script:SettingsPath -Raw | ConvertFrom-Json
            # Saved WPF coordinates use the virtual desktop, which includes
            # monitors to the left/above the primary display (negative values).
            $vLeft = [System.Windows.SystemParameters]::VirtualScreenLeft
            $vTop = [System.Windows.SystemParameters]::VirtualScreenTop
            $vRight = $vLeft + [System.Windows.SystemParameters]::VirtualScreenWidth
            $vBottom = $vTop + [System.Windows.SystemParameters]::VirtualScreenHeight
            if ($null -ne $s.Left) {
                $left = [double]$s.Left
                if (-not [double]::IsNaN($left) -and -not [double]::IsInfinity($left) -and
                    $left -ge ($vLeft - 40) -and $left -lt $vRight) {
                    $window.Left = $left
                }
            }
            if ($null -ne $s.Top) {
                $top = [double]$s.Top
                if (-not [double]::IsNaN($top) -and -not [double]::IsInfinity($top) -and
                    $top -ge ($vTop - 40) -and $top -lt $vBottom) {
                    $window.Top = $top
                }
            }
            if ($null -ne $s.Wander) { $script:Wander = [bool]$s.Wander }
            if ($s.Open) { Set-Dashboard $true }
            return $true
        }
    } catch { $script:LastError = $_ }
    return $false
}

# ---------------------------------------------------------------------------
# Mouse.  DragMove hands the drag to the OS move loop, which tracks the cursor
# at full input rate; polling the cursor from a 33ms timer lagged badly.
# DispatcherTimers keep firing inside that loop, so the flail still animates.
# ---------------------------------------------------------------------------
$ui.Root.Add_MouseLeftButtonDown({
    $startL = $window.Left
    $startT = $window.Top
    $script:LastX = $window.Left
    $script:Dragging = $true
    Set-Eyes 'happy'
    try { $window.DragMove() } catch { $script:LastError = $_ }
    $script:Dragging = $false

    Reset-Pose
    $moved = [Math]::Abs(($window.Left - $startL)) + [Math]::Abs(($window.Top - $startT))
    if ($moved -lt 3) {
        Set-Dashboard (-not $script:Open)    # a click, not a drag
        if ($script:Mood -in @('sleep', 'lost')) { Sync-EyesToMood }
        else { Set-Eyes 'happy' }
        $script:HoverAt = $script:Tick
    } else {
        Wake-BotFromDrag
        $script:State = 'idle'
        $script:NextAt = $script:Tick + 30
        Sync-EyesToMood
        Save-Settings
    }
})

# hover -> hop (with a cooldown so brushing past doesn't make it pogo)
$ui.BotGroup.Add_MouseEnter({
    if ($script:Dragging) { return }
    if ($script:Tick - $script:HoverAt -lt 40) { return }
    $script:HoverAt = $script:Tick
    Start-Jump
})

# ---------------------------------------------------------------------------
# Context menu
# ---------------------------------------------------------------------------
$menu = New-Object System.Windows.Controls.ContextMenu
function Add-MenuItem([string]$h, [scriptblock]$a) {
    $i = New-Object System.Windows.Controls.MenuItem
    $i.Header = $h; $i.Add_Click($a); $menu.Items.Add($i) | Out-Null; $i
}
Add-MenuItem 'Show / hide dashboard' { Set-Dashboard (-not $script:Open) } | Out-Null
$wanderItem = Add-MenuItem 'Let it wander' {
    $script:Wander = -not $script:Wander
    if (-not $script:Wander) { $script:State = 'idle'; Reset-Pose }
    Save-Settings
}
$wanderItem.IsCheckable = $true
Add-MenuItem 'Jump!' { Start-Jump } | Out-Null
Add-MenuItem 'Refresh now' { Update-Widget } | Out-Null
if ($NativeMode) {
    Add-MenuItem 'Open collector settings' {
        if ($CollectorDataDir -and (Test-Path -LiteralPath $CollectorDataDir)) {
            Start-Process $script:ExplorerPath $CollectorDataDir
        } else {
            [System.Windows.MessageBox]::Show(
                'The settings folder is created after the collector starts.',
                'Claude Usage Bot'
            ) | Out-Null
        }
    } | Out-Null
} else {
    Add-MenuItem 'Calibrate limits...' {
        Start-Process $script:WslPath -ArgumentList @('--', 'bash', '-lc',
            'if [ -d ~/claude-usage-bot ]; then cd ~/claude-usage-bot; else cd ~/cute.app; fi && python3 collector.py --calibrate; echo; read -p "press enter to close"')
    } | Out-Null
}
Add-MenuItem 'Open data folder' { Start-Process $script:ExplorerPath (Split-Path $script:DataPath -Parent) } | Out-Null
$menu.Items.Add((New-Object System.Windows.Controls.Separator)) | Out-Null
Add-MenuItem 'Quit' { $window.Close() } | Out-Null
$menu.Add_Opened({ $wanderItem.IsChecked = $script:Wander })
$ui.Root.ContextMenu = $menu

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
$window.Add_Loaded({
    if (-not (Restore-Settings)) {
        $work = [System.Windows.SystemParameters]::WorkArea
        $window.Left = $work.Right - $window.ActualWidth - 60
        $window.Top  = $work.Bottom - $window.ActualHeight - 60
    }
    Set-Eyes 'open'
    Update-Widget
})
$window.Add_Closing({ Save-Settings })

$motionTimer = New-Object System.Windows.Threading.DispatcherTimer
$motionTimer.Interval = [TimeSpan]::FromMilliseconds(33)
$motionTimer.Add_Tick({ try { Step-Motion } catch { $script:LastError = $_ } })
$motionTimer.Start()

$dataTimer = New-Object System.Windows.Threading.DispatcherTimer
$dataTimer.Interval = [TimeSpan]::FromSeconds(2)
$dataTimer.Add_Tick({
    # One bad refresh must not kill the timer and freeze the card.
    try { if (-not $script:Dragging) { Update-Widget } } catch { $script:LastError = $_ }
})
$dataTimer.Start()

$window.ShowDialog() | Out-Null
