param([Parameter(Mandatory=$true)][string]$Path)

# 强制 UTF-8 输出。父进程如果在 GBK 控制台（cmd 936）里启动，PowerShell 默认
# 会按控制台代码页写 stdout，Python 侧再按 UTF-8 解码就会把中文整段丢掉，
# 只剩 A/B 这类 ASCII 字母——2026-10-29 两题漏抓就是这个原因。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null

# 不走 StorageFile（某些目录会被 WinRT 拒之门外），直接把文件字节读进内存流再解码
$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $Path).Path)
$stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
$writer = [Windows.Storage.Streams.DataWriter]::new($stream)
$writer.WriteBytes($bytes)
Await ($writer.StoreAsync()) ([uint32]) | Out-Null
$writer.DetachStream() | Out-Null
$stream.Seek(0)
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $lang = [Windows.Globalization.Language]::new("zh-Hans-CN")
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
}
if ($null -eq $engine) { Write-Output "__NO_OCR_ENGINE__"; exit 3 }
Write-Output ("__ENGINE__" + $engine.RecognizerLanguage.LanguageTag)

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
# 逐词输出：行号<TAB>x<TAB>y<TAB>宽<TAB>高<TAB>文字
# 给出词级坐标，Python 侧才能按间距判断「L oa d」这些其实是一个单词
$li = 0
foreach ($line in $result.Lines) {
    foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        $t = ($w.Text -replace "[`t`r`n]", " ")
        Write-Output ("{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $li, [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $t)
    }
    $li++
}
