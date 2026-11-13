# Regenerate the README screenshots from the demo (synthetic data, nothing connects anywhere).
#   powershell -ExecutionPolicy Bypass -File assets\readme\capture.ps1
# Needs the dev venv (.venv with flet[all]). Each shot opens the app on one screen and theme,
# captures the window, then closes it. Output: assets\readme\<screen>-<theme>.png
param([string[]]$Shots = @("review-light", "review-dark", "results-light", "results-dark", "connect-dark"))
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\..\.."
Add-Type @"
using System; using System.Runtime.InteropServices;
public class Cap { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  public struct R { public int L, T, Rt, B; } }
"@
[Cap]::SetProcessDPIAware() | Out-Null
Add-Type -AssemblyName System.Drawing

foreach ($shot in $Shots) {
  $screen, $theme = $shot.Split("-")
  $before = @(Get-Process | Where-Object { $_.MainWindowTitle -eq "Binday" } | ForEach-Object Id)
  $proc = Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "gui.py", "--demo", "--demo-screen=$screen", "--theme=$theme" -WorkingDirectory $root -PassThru -WindowStyle Hidden
  $win = $null
  for ($i = 0; $i -lt 60 -and -not $win; $i++) {
    Start-Sleep -Milliseconds 500
    $win = Get-Process | Where-Object { $_.MainWindowTitle -eq "Binday" -and $before -notcontains $_.Id } | Select-Object -First 1
  }
  if (-not $win) { throw "No Binday window for $shot" }
  Start-Sleep -Seconds 9   # let the autoplay reach the screen and fonts settle
  $r = New-Object Cap+R; [Cap]::GetWindowRect($win.MainWindowHandle, [ref]$r) | Out-Null
  $w = $r.Rt - $r.L; $h = $r.B - $r.T
  $bmp = New-Object System.Drawing.Bitmap $w, $h
  $g = [System.Drawing.Graphics]::FromImage($bmp); $dc = $g.GetHdc()
  [Cap]::PrintWindow($win.MainWindowHandle, $dc, 2) | Out-Null; $g.ReleaseHdc($dc); $g.Dispose()
  $out = "$PSScriptRoot\$shot.png"; $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
  "$shot -> ${w}x${h}"
  Stop-Process -Id $win.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
}
