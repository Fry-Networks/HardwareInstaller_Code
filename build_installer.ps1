param([string]$Version = "")
$ErrorActionPreference = "Stop"

# 1Password references for retrieving secrets at build time
$OP_BEARER_TOKEN_REF = "op://HardwareAPI/Hardware_API/API_BEARER_TOKEN"
$OP_GUI_GITHUB_REPO_REF = "op://VSCode/hardware_exe/Github_repo_hardware_exe"
$OP_HW_GITHUB_REPO_REF = "op://VSCode/hardware_exe/Github_repo_hardwareinstaller"
$OP_POC_GITHUB_REPO_REF = "op://VSCode/hardware_exe/Github_repo_hardwarepoc"
$EXTERNAL_API_BASE_URL = "https://hardwareapi.frynetworks.com"
$OLOSTEP_BROWSER_URL = "https://olostepbrowser.s3.us-east-1.amazonaws.com/updates/win32/x64/Olostep-Browser-1.0.1+Setup.exe"
$OP_HONEYGAIN_KEY_REF = "op://Bandwidth Miners/Honeygain SDK API/credential"
$OP_BRIGHT_APP_ID_REF = "op://Bandwidth Miners/Bright Data SDK Login/APP_ID"
$OP_MYSTERIUM_PAYOUT_REF = "op://Bandwidth Miners/Mysterium SDK API/MYST_PAYOUT_ADDR"
$OP_MYSTERIUM_REG_TOKEN_REF = "op://Bandwidth Miners/Mysterium SDK API/MYST_REG_TOKEN"
$OP_MYSTERIUM_API_KEY_REF = "op://Bandwidth Miners/Mysterium SDK API/MYST_API_KEY"
# Encryption key references (create these in 1Password before first build)
$OP_ENC_HONEYGAIN_SALT_REF = "op://Bandwidth Miners/Encryption Keys/HONEYGAIN_SALT"
$OP_ENC_HONEYGAIN_PASSWORD_REF = "op://Bandwidth Miners/Encryption Keys/HONEYGAIN_PASSWORD"
$OP_ENC_BRIGHT_SALT_REF = "op://Bandwidth Miners/Encryption Keys/BRIGHT_SALT"
$OP_ENC_BRIGHT_PASSWORD_REF = "op://Bandwidth Miners/Encryption Keys/BRIGHT_PASSWORD"
$OP_ENC_SDK_SALT_REF = "op://Bandwidth Miners/Encryption Keys/SDK_SALT"
$OP_ENC_SDK_PASSWORD_REF = "op://Bandwidth Miners/Encryption Keys/SDK_PASSWORD"

# If no version provided, read from version.py
if ([string]::IsNullOrWhiteSpace($Version)) {
    Write-Host "No version specified, reading from version.py..." -ForegroundColor Gray
    $VersionFile = Join-Path $PSScriptRoot "version.py"
    if (Test-Path $VersionFile) {
        $VersionContent = Get-Content $VersionFile -Raw
        if ($VersionContent -match '__version__\s*=\s*["' + "'" + ']([^"' + "'" + ']+)["' + "'" + ']') {
            $Version = $matches[1]
            Write-Host "  [OK] Version from version.py: $Version" -ForegroundColor Green
        } else {
            Write-Host "  [FAIL] Could not parse version from version.py, using default 1.0.0" -ForegroundColor Yellow
            $Version = "1.0.0"
        }
    } else {
        Write-Host "  [FAIL] version.py not found, using default version 1.0.0" -ForegroundColor Yellow
        $Version = "1.0.0"
    }
}

Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "FryNetworks Installer Build Script" -ForegroundColor Cyan
Write-Host "Version: $Version" -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan
$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $InstallerDir
Write-Host "`n[1/5] Retrieving bearer token from 1Password..." -ForegroundColor Yellow
try {
    $BearerToken = op read $OP_BEARER_TOKEN_REF
    if ([string]::IsNullOrWhiteSpace($BearerToken)) { throw "Bearer token is empty" }
    Write-Host "  [OK] Bearer token retrieved successfully" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve bearer token from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}
Write-Host "`n[1b/5] Retrieving GUI GitHub repo path from 1Password..." -ForegroundColor Yellow
try {
    $GuiGithubPath = op read $OP_GUI_GITHUB_REPO_REF
    if ([string]::IsNullOrWhiteSpace($GuiGithubPath)) { throw "GUI GitHub path is empty" }
    Write-Host "  [OK] GUI GitHub path retrieved: $GuiGithubPath" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve GUI GitHub path from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1c/5] Retrieving PoC GitHub repo path from 1Password..." -ForegroundColor Yellow
try {
    $PocGithubPath = op read $OP_POC_GITHUB_REPO_REF
    if ([string]::IsNullOrWhiteSpace($PocGithubPath)) { throw "PoC GitHub path is empty" }
    Write-Host "  [OK] PoC GitHub path retrieved: $PocGithubPath" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve PoC GitHub path from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1d/5] Retrieving HW Installer GitHub repo path from 1Password..." -ForegroundColor Yellow
try {
    $HwGithubPath = op read $OP_HW_GITHUB_REPO_REF
    if ([string]::IsNullOrWhiteSpace($HwGithubPath)) { throw "HW GitHub path is empty" }
    Write-Host "  [OK] HW GitHub path retrieved: $HwGithubPath" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve HW GitHub path from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

$HoneygainKey = $null

# Honeygain API key: required to be provided via 1Password for builds
if (-not $OP_HONEYGAIN_KEY_REF) {
    Write-Host "`n[1f/5] [FAIL] OP_HONEYGAIN_KEY_REF not set - Honeygain API key must be provided via 1Password" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1f/5] Retrieving Honeygain API key from 1Password..." -ForegroundColor Yellow
try {
    $HoneygainKey = (op read $OP_HONEYGAIN_KEY_REF).Trim()
    if ([string]::IsNullOrWhiteSpace($HoneygainKey)) { throw "Honeygain API key is empty" }
    Write-Host "  [OK] Honeygain API key embedded for BM installs" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve Honeygain API key from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}



$BrightAppId = $null

# Bright app id: required to be provided via 1Password for builds
if (-not $OP_BRIGHT_APP_ID_REF) {
    Write-Host "`n[1g/5] [FAIL] OP_BRIGHT_APP_ID_REF not set - Bright app ID must be provided via 1Password" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1g/5] Retrieving Bright app ID from 1Password..." -ForegroundColor Yellow
try {
    $BrightAppId = (op read $OP_BRIGHT_APP_ID_REF).Trim()
    if ([string]::IsNullOrWhiteSpace($BrightAppId)) { throw "Bright app ID is empty" }
    Write-Host "  [OK] Bright app ID embedded for BM installs" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve Bright app ID from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}


# Mysterium credentials: required to be provided via 1Password for builds
if (-not $OP_MYSTERIUM_PAYOUT_REF -or -not $OP_MYSTERIUM_REG_TOKEN_REF -or -not $OP_MYSTERIUM_API_KEY_REF) {
    Write-Host "`n[1h/5] [FAIL] One or more OP_MYSTERIUM_* refs not set - Mysterium credentials must be provided via 1Password" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1h/5] Retrieving Mysterium credentials from 1Password..." -ForegroundColor Yellow
try {
    $MysteriumPayout = (op read $OP_MYSTERIUM_PAYOUT_REF).Trim()
    $MysteriumReg = (op read $OP_MYSTERIUM_REG_TOKEN_REF).Trim()
    $MysteriumApiKey = (op read $OP_MYSTERIUM_API_KEY_REF).Trim()
    if ([string]::IsNullOrWhiteSpace($MysteriumPayout) -or [string]::IsNullOrWhiteSpace($MysteriumReg) -or [string]::IsNullOrWhiteSpace($MysteriumApiKey)) {
        throw "One or more Mysterium credentials are empty"
    }
    Write-Host "  [OK] Mysterium credentials embedded for BM installs" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve Mysterium credentials from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}



Write-Host "`n[1i/5] Retrieving encryption keys from 1Password..." -ForegroundColor Yellow
try {
    $EncHoneygainSalt = (op read $OP_ENC_HONEYGAIN_SALT_REF).Trim()
    $EncHoneygainPassword = (op read $OP_ENC_HONEYGAIN_PASSWORD_REF).Trim()
    $EncBrightSalt = (op read $OP_ENC_BRIGHT_SALT_REF).Trim()
    $EncBrightPassword = (op read $OP_ENC_BRIGHT_PASSWORD_REF).Trim()
    $EncSdkSalt = (op read $OP_ENC_SDK_SALT_REF).Trim()
    $EncSdkPassword = (op read $OP_ENC_SDK_PASSWORD_REF).Trim()
    if ([string]::IsNullOrWhiteSpace($EncHoneygainSalt) -or [string]::IsNullOrWhiteSpace($EncHoneygainPassword) -or
        [string]::IsNullOrWhiteSpace($EncBrightSalt) -or [string]::IsNullOrWhiteSpace($EncBrightPassword) -or
        [string]::IsNullOrWhiteSpace($EncSdkSalt) -or [string]::IsNullOrWhiteSpace($EncSdkPassword)) {
        throw "One or more encryption keys are empty"
    }
    Write-Host "  [OK] Encryption keys retrieved" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve encryption keys from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[2/5] Creating build_config.json..." -ForegroundColor Yellow
$BuildConfig = @{ 
    external_api = @{ base_url = $EXTERNAL_API_BASE_URL; bearer_token = $BearerToken; timeout = 10.0 }; 
    github = @{
        gui = @{ path = $GuiGithubPath };
        poc = @{ path = $PocGithubPath };
        hw  = @{ path = $HwGithubPath }
    };
    encryption = @{
        honeygain = @{ salt = $EncHoneygainSalt; password = $EncHoneygainPassword };
        bright = @{ salt = $EncBrightSalt; password = $EncBrightPassword };
        sdk = @{ salt = $EncSdkSalt; password = $EncSdkPassword }
    };
    partner_integrations = @{
        honeygain = @{
            enabled = [bool]$HoneygainKey;
            api_key = $HoneygainKey
        };
        bright = @{
            enabled = [bool]$BrightAppId;
            app_id = $BrightAppId
        }
        ;
        mysterium = @{
            enabled = $true;
            payout_addr = $MysteriumPayout;
            reg_token = $MysteriumReg;
            api_key = $MysteriumApiKey
        }
    }; 
    status = "embedded"; 
    source = "1password"; 
    version = $Version; 
    build_date = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") 
} | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText("$PWD\build_config.json", $BuildConfig, (New-Object System.Text.UTF8Encoding $false))
Write-Host "  [OK] build_config.json created" -ForegroundColor Green
Write-Host "`n[3/4] Preparing embedded resources..." -ForegroundColor Yellow

# Copy NSSM (required utility)
# Look for nssm.exe in the repository's tools/ directory (installer root)
$NssmSource = Join-Path $InstallerDir "tools\nssm.exe"
$NssmDest = Join-Path $InstallerDir "resources\embedded\nssm.exe"
if (Test-Path $NssmSource) {
    Copy-Item $NssmSource $NssmDest -Force
    Write-Host "  [OK] Copied NSSM ($(([math]::Round((Get-Item $NssmDest).Length / 1KB, 0))) KB)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] NSSM not found at $NssmSource - NSSM is required for building the installer." -ForegroundColor Red
    Write-Host "  Please place nssm.exe into the tools/ folder and re-run the build." -ForegroundColor Red
    exit 1
}

# NOTE: Service executables (miner binaries) are NOT embedded
# They should be obtained separately or downloaded during installation

Write-Host "`n[4/5] Cleaning previous builds..." -ForegroundColor Yellow
Remove-Item -Force -Recurse build,dist -ErrorAction SilentlyContinue
Write-Host "  [OK] Build directories cleaned" -ForegroundColor Green

# Build updater first so MSI bundling finds it in dist\
Write-Host "`n[5a/5] Building updater with PyInstaller..." -ForegroundColor Yellow
try {
    py -m PyInstaller `
        --onefile `
        --noconsole `
        --paths "." `
        --add-data "build_config.json;." `
        --icon "resources\frynetworks_logo.ico" `
        --name frynetworks_updater `
        tools\updater.py
    if (-not (Test-Path "dist\frynetworks_updater.exe")) { throw "updater.exe not found after build" }
    Write-Host "  [OK] updater built: dist\frynetworks_updater.exe" -ForegroundColor Green
} catch {
    Write-Host "`n[FAIL] Updater build failed" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[5b/5] Building installer with PyInstaller..." -ForegroundColor Yellow
Write-Host "  This may take 30-60 seconds..." -ForegroundColor Gray
$ExeName = "FryNetworks_Installer_v$Version"

# Generate version_info.txt for Windows file properties (right-click > Properties > Details)
$VerParts = $Version.Split('.')
$Major = if ($VerParts.Length -ge 1) { $VerParts[0] } else { "0" }
$Minor = if ($VerParts.Length -ge 2) { $VerParts[1] } else { "0" }
$Patch = if ($VerParts.Length -ge 3) { $VerParts[2] } else { "0" }
$VersionInfoContent = @"
# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($Major, $Minor, $Patch, 0),
    prodvers=($Major, $Minor, $Patch, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
    ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'FryNetworks'),
        StringStruct(u'FileDescription', u'FryNetworks Hardware Installer'),
        StringStruct(u'FileVersion', u'$Version.0'),
        StringStruct(u'InternalName', u'frynetworks_installer'),
        StringStruct(u'LegalCopyright', u'Copyright (c) FryNetworks'),
        StringStruct(u'OriginalFilename', u'frynetworks_installer.exe'),
        StringStruct(u'ProductName', u'FryNetworks Installer'),
        StringStruct(u'ProductVersion', u'$Version.0')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"@
[System.IO.File]::WriteAllText("$PWD\version_info.txt", $VersionInfoContent, (New-Object System.Text.UTF8Encoding $false))
Write-Host "  [OK] Generated version_info.txt for v$Version" -ForegroundColor Green

# Note: Alternative build method using spec file (recommended to prevent duplicate tray icons):
# py -m PyInstaller frynetworks_installer.spec
# This method uses the spec file which includes Windows-specific settings

try {
    py -m PyInstaller `
        --onefile `
        --noconsole `
        --uac-admin `
        --paths "." `
        --paths ".\core" `
        --paths ".\gui" `
        --hidden-import "core.service_manager" `
        --hidden-import "core.config_manager" `
        --hidden-import "core.conflict_detector" `
        --hidden-import "core.naming" `
        --hidden-import "core.key_parser" `
        --hidden-import "tools.external_api" `
        --collect-submodules "core" `
        --collect-submodules "tools" `
        --icon "resources\frynetworks_logo.ico" `
        --version-file "version_info.txt" `
        --add-data "build_config.json;." `
        --add-data "resources\background.png;resources" `
        --add-data "resources\frynetworks_logo.ico;resources" `
        --add-data "resources\embedded;resources\embedded" `
        --add-data "SDK;SDK" `
        --add-data "core;core" `
        --add-data "tools;tools" `
        --name $ExeName `
        installer_main.py
    
    if (Test-Path "dist\$ExeName.exe") {
        Write-Host "`n[OK] Build completed successfully!" -ForegroundColor Green
        Write-Host "`nInstaller location:" -ForegroundColor Cyan
        Write-Host "  $(Join-Path $InstallerDir "dist\$ExeName.exe")" -ForegroundColor White
        $FileSize = (Get-Item "dist\$ExeName.exe").Length
        $FileSizeMB = [math]::Round($FileSize / 1MB, 2)
        Write-Host "`nFile size: $FileSizeMB MB" -ForegroundColor Gray
        Write-Host "`nTo test the installer:" -ForegroundColor Cyan
        Write-Host "  cd dist" -ForegroundColor White
        Write-Host "  .\$ExeName.exe --gui" -ForegroundColor White
    } else { throw "Build completed but executable not found" }
} catch {
    Write-Host "`n[FAIL] Build failed" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
} finally {
    if (Test-Path "build_config.json") {
        Remove-Item "build_config.json" -Force
        Write-Host "`n  Cleaned up build_config.json" -ForegroundColor Gray
    }
    if (Test-Path "version_info.txt") {
        Remove-Item "version_info.txt" -Force
        Write-Host "  Cleaned up version_info.txt" -ForegroundColor Gray
    }
    # Clean up embedded NSSM (it's now in the exe)
    if (Test-Path "resources\embedded\nssm.exe") {
        Remove-Item "resources\embedded\nssm.exe" -Force
    }
}
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "Build process complete!" -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan

