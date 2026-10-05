; Lab Sentinel Dashboard installer (Inno Setup 6), for the supervisor's PC.
;
; Built by packaging/build.py --installer, which passes AppVersion and DistDir.
; Installs the dashboard, a Start menu shortcut that keeps recording even if
; the window is closed (--keep-running), and the firewall rules agents need:
; TCP 8765 (agent connections) and UDP 50123 (discovery probes), private and
; domain networks only.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef DistDir
  #define DistDir "..\..\build\dist"
#endif

[Setup]
AppId={{2E7B9C14-0D3A-4F2B-8E61-7A5C9D0B3E21}
AppName=Lab Sentinel Dashboard
AppVersion={#AppVersion}
AppPublisher=Lab Sentinel
DefaultDirName={autopf}\LabSentinel\Dashboard
DefaultGroupName=Lab Sentinel
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=LabSentinelDashboard-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
UninstallDisplayName=Lab Sentinel Dashboard
UninstallDisplayIcon={app}\labsentinel-dashboard.exe

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Buat ikon di desktop"; GroupDescription: "Pintasan:"

[Files]
Source: "{#DistDir}\LabSentinelDashboard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{commonappdata}\LabSentinel\dashboard"

[Icons]
Name: "{group}\Lab Sentinel Dashboard"; Filename: "{app}\labsentinel-dashboard.exe"; \
  Parameters: "--keep-running"
Name: "{group}\Folder laporan"; Filename: "{commonappdata}\LabSentinel\dashboard\reports"
Name: "{autodesktop}\Lab Sentinel Dashboard"; Filename: "{app}\labsentinel-dashboard.exe"; \
  Parameters: "--keep-running"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall add rule name=""Lab Sentinel Dashboard (agent)"" dir=in action=allow protocol=TCP localport=8765 profile=private,domain program=""{app}\labsentinel-dashboard.exe"""; \
  Flags: runhidden waituntilterminated
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall add rule name=""Lab Sentinel Dashboard (penemuan)"" dir=in action=allow protocol=UDP localport=50123 profile=private,domain program=""{app}\labsentinel-dashboard.exe"""; \
  Flags: runhidden waituntilterminated
Filename: "{app}\labsentinel-dashboard.exe"; Parameters: "--keep-running"; \
  Description: "Jalankan Lab Sentinel Dashboard sekarang"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall delete rule name=""Lab Sentinel Dashboard (agent)"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "FwTcp"
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall delete rule name=""Lab Sentinel Dashboard (penemuan)"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "FwUdp"

; The database, evidence screenshots and reports in ProgramData are kept on
; uninstall on purpose: they are records the institution may need later.
