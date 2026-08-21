; NativMix Windows Installer — Inno Setup 6
; https://jrsoftware.org/isinfo.php
;
; The version is injected by build.ps1 via /DMyAppVersion=x.y.z.
; To build manually:
;   ISCC.exe /DMyAppVersion=1.1.0 packaging\win\nativmix.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName      "NativMix"
#define MyAppPublisher "Christian Möllmann (knoelliX)"
#define MyAppURL       "https://github.com/Arthur-D/NativMix"
#define MyAppExeName   "nativmix.exe"
; Fixed GUID — must NOT change between versions (used for upgrade detection)
#define MyAppId        "{{8B4A2E1F-3C7D-4F9A-B256-8E9A0C1D2F3E}"

; ── Setup ─────────────────────────────────────────────────────────────────────
[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

; Per-user install by default; offer system-wide if user has admin rights
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Uninstall
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
Uninstallable=yes
CreateUninstallRegKey=yes

; Output
OutputDir=output
OutputBaseFilename=NativMix-{#MyAppVersion}-Setup
SetupIconFile=..\..\assets\icon.ico

; Compression (lzma2/ultra64 = best ratio, ~30% slower build)
Compression=lzma2/ultra64
SolidCompression=yes

; Visual
WizardStyle=modern

; Version info embedded in the installer .exe
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

; Architecture: 64-bit only (Python wheels are x64 on Windows)
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; License shown in the wizard
LicenseFile=..\..\LICENSE

; ── Languages ─────────────────────────────────────────────────────────────────
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german";  MessagesFile: "compiler:Languages\German.isl"

; ── Tasks (optional checkboxes in the wizard) ─────────────────────────────────
[Tasks]
Name: "desktopicon"; \
  Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; \
  Flags: unchecked

Name: "autostart"; \
  Description: "Start {#MyAppName} automatically when Windows starts (minimized to tray)"; \
  GroupDescription: "Additional tasks:"; \
  Flags: unchecked

; ── Files ─────────────────────────────────────────────────────────────────────
[Files]
; PyInstaller bundle (built by build.ps1 → dist/NativMix/)
Source: "..\..\dist\NativMix\*"; \
  DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

; License and readme placed alongside the exe for easy access
Source: "..\..\LICENSE";   DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion

; ── Icons (shortcuts) ─────────────────────────────────────────────────────────
[Icons]
; Start menu
Name: "{group}\{#MyAppName}"; \
  Filename: "{app}\{#MyAppExeName}"; \
  Parameters: "--hidden"; \
  Comment: "NativMix hardware volume mixer"

Name: "{group}\Uninstall {#MyAppName}"; \
  Filename: "{uninstallexe}"

; Desktop (optional task)
Name: "{autodesktop}\{#MyAppName}"; \
  Filename: "{app}\{#MyAppExeName}"; \
  Tasks: desktopicon

; ── Registry ──────────────────────────────────────────────────────────────────
[Registry]
; Autostart via Windows Run key (only when "autostart" task is checked)
Root: HKCU; \
  Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; \
  ValueName: "NativMix"; \
  ValueData: """{app}\{#MyAppExeName}"" --hidden"; \
  Flags: uninsdeletevalue; \
  Tasks: autostart

; ── Run after install ─────────────────────────────────────────────────────────
[Run]
Filename: "{app}\{#MyAppExeName}"; \
  Description: "Start {#MyAppName} now (minimized to tray)"; \
  Parameters: "--hidden"; \
  Flags: nowait postinstall skipifsilent unchecked

; ── Kill running instance before uninstall / upgrade ─────────────────────────
[UninstallRun]
Filename: "taskkill.exe"; \
  Parameters: "/f /im {#MyAppExeName}"; \
  Flags: runhidden skipifdoesntexist; \
  RunOnceId: "KillNativMix"

; ── Pascal code ───────────────────────────────────────────────────────────────
[Code]

{ Kill any running NativMix before upgrading so files can be overwritten. }
procedure KillRunningInstance();
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/f /im ' + '{#MyAppExeName}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  { Give Windows a moment to release file handles }
  Sleep(800);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  KillRunningInstance();
  Result := '';
end;
