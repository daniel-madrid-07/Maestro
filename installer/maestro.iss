; MaestroSetup.exe -- the one-click Windows installer.
;
; A thin wrapper: it carries install.ps1 and runs it. The script does the
; work (Git, uv, Claude Code, Maestro, the sign-in, the VS Code integration)
; in a console window that shows each step. Native Windows: no WSL. Built by
; .github/workflows/release.yml:  ISCC /DAppVersion=0.1.2 installer\maestro.iss

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
; A release build installs its own tag; a dev build, the latest code.
#if AppVersion == "0.0.0-dev"
  #define VersionArg ""
#else
  #define VersionArg " -Version " + AppVersion
#endif

[Setup]
AppId={{6F3B2E9A-5C1D-4E7B-9A8F-2D4C6B1E0F37}
AppName=Maestro
AppVersion={#AppVersion}
AppVerName=Maestro {#AppVersion}
AppPublisher=Daniel Madrid
AppPublisherURL=https://github.com/daniel-madrid-07/Maestro
AppSupportURL=https://github.com/daniel-madrid-07/Maestro/issues
AppUpdatesURL=https://github.com/daniel-madrid-07/Maestro/releases
DefaultDirName={localappdata}\Maestro\setup
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
; Per-user throughout: Git, uv, Claude Code and Maestro all install without admin.
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=MaestroSetup
SetupIconFile=maestro.ico
UninstallDisplayIcon={app}\maestro.ico
UninstallDisplayName=Maestro
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; ConPTY, which hosts each session, arrived in Windows 10 version 1809.
MinVersion=10.0.17763
LicenseFile=..\LICENSE

[Messages]
WelcomeLabel2=This installs Maestro, which runs many Claude Code sessions at once and lets one of them conduct the rest.%n%nIt sets up what it needs by itself: Git for Windows and Claude Code if they are missing, and Maestro. No administrator rights, no restart. You will be asked to sign in to your Claude account once, in your browser.%n%nNothing else on your computer is changed.
FinishedLabel=Maestro is ready. Its panel is open, and the Start menu entry "Maestro" reopens it.%n%nIn Claude Code, say: use Maestro to build <what you want>.

[Files]
Source: "install.ps1";       DestDir: "{app}"; Flags: ignoreversion
Source: "maestro.ico";       DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1""{#VersionArg}"; \
  StatusMsg: "Installing Maestro. A window shows each step; answer it if it asks."; \
  Flags: waituntilterminated; Check: not WizardSilent
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1""{#VersionArg} -Yes -NoOpen"; \
  Flags: waituntilterminated runhidden; Check: WizardSilent

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Uninstall"; \
  Flags: waituntilterminated; RunOnceId: "MaestroUninstall"; Check: not UninstallSilent
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Uninstall -Yes"; \
  Flags: waituntilterminated runhidden; RunOnceId: "MaestroUninstallSilent"; Check: UninstallSilent
