<#
  Read-only hardware audit for local LLM (Ollama) suitability.
  Run on the physical laptop in Windows PowerShell:
      powershell -ExecutionPolicy Bypass -File .\Get-LaptopAIAudit.ps1
  It installs nothing, changes no settings and runs no benchmarks.
  Its only write is a temporary dxdiag text report, which it deletes afterwards.
  It does not collect serial numbers.
#>

$ErrorActionPreference = 'SilentlyContinue'
function H($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

H 'Execution context (is this the physical host?)'
$cs  = Get-CimInstance Win32_ComputerSystem
$csp = Get-CimInstance Win32_ComputerSystemProduct
[pscustomobject]@{
  Manufacturer       = $cs.Manufacturer
  ModelMTM           = $cs.Model            # Lenovo machine type, e.g. 81EU
  FriendlyModel      = $csp.Version         # Lenovo puts e.g. "Lenovo IdeaPad 530S-14IKB" here
  SystemFamily       = $cs.SystemFamily
  HypervisorPresent  = $cs.HypervisorPresent  # True also when Hyper-V/WSL2 is enabled on bare metal
  IsVirtualModel     = ($cs.Model -match 'Virtual|VMware|KVM|HVM')
} | Format-List

H 'Operating system'
Get-CimInstance Win32_OperatingSystem |
  Select-Object Caption, Version, BuildNumber, OSArchitecture | Format-List

H 'CPU'
Get-CimInstance Win32_Processor |
  Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed, L3CacheSize | Format-List
# AVX2 matters for llama.cpp/Ollama CPU speed; 8th-gen Intel Core and Ryzen 2000U both have it.

H 'Physical RAM'
"Total usable (GB): {0:N1}" -f ($cs.TotalPhysicalMemory / 1GB)
Get-CimInstance Win32_PhysicalMemory |
  Select-Object DeviceLocator, BankLabel, @{n='GB';e={$_.Capacity/1GB}}, Speed, ConfiguredClockSpeed,
                Manufacturer, PartNumber, FormFactor | Format-Table -AutoSize
Get-CimInstance Win32_PhysicalMemoryArray |
  Select-Object @{n='SlotsReported';e={$_.MemoryDevices}}, @{n='MaxCapacityGB';e={$_.MaxCapacityEx/1MB}} | Format-List
"Note: soldered vs socketed RAM cannot be reliably read from WMI. Confirm against Lenovo PSREF using the MTM above."

H 'GPU(s) and dedicated VRAM'
Get-CimInstance Win32_VideoController |
  Select-Object Name, DriverVersion, @{n='AdapterRAM_GB(capped at 4, unreliable)';e={[math]::Round($_.AdapterRAM/1GB,2)}} | Format-Table -AutoSize
$class = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
Get-ChildItem $class | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {
  $p = Get-ItemProperty $_.PSPath
  if ($p.DriverDesc) {
    $q = $p.'HardwareInformation.qwMemorySize'
    "{0}: dedicated VRAM = {1}" -f $p.DriverDesc, $(if ($q) { '{0:N2} GB' -f ($q/1GB) } else { 'not reported (typical for integrated GPUs)' })
  }
}
if (Get-Command nvidia-smi) { nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv }

H 'Shared GPU memory (from dxdiag)'
$dx = Join-Path $env:TEMP 'dxdiag_audit.txt'
Start-Process dxdiag -ArgumentList "/t `"$dx`"" -Wait
if (Test-Path $dx) {
  Select-String -Path $dx -Pattern 'Card name','Dedicated Memory','Shared Memory','Display Memory' | ForEach-Object { $_.Line.Trim() }
  Remove-Item $dx
}

H 'Storage'
Get-PhysicalDisk | Select-Object FriendlyName, MediaType, BusType, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | Format-Table -AutoSize
Get-Volume | Where-Object DriveLetter |
  Select-Object DriveLetter, FileSystemLabel, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}}, @{n='FreeGB';e={[math]::Round($_.SizeRemaining/1GB,1)}} | Format-Table -AutoSize

H 'Ports relevant to external GPUs'
Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match 'Thunderbolt|USB4' } | Select-Object -ExpandProperty Name
"(No output above = no Thunderbolt/USB4 controller detected, so no practical eGPU option.)"

H 'Existing AI tooling (detect only)'
foreach ($c in 'ollama','wsl','docker') { "{0,-8} {1}" -f $c, $(if (Get-Command $c) { 'present' } else { 'not found' }) }

H 'Power / thermal context'
Get-CimInstance Win32_Battery | Select-Object Name, EstimatedChargeRemaining, BatteryStatus | Format-List
"Done. Paste the full output back to Claude."
