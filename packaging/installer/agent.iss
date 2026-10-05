; Lab Sentinel Agent installer (Inno Setup 6).
;
; Built by packaging/build.py --installer, which passes AppVersion and DistDir.
;
; Interactive (one file to copy to each lab PC, double-click):
;   UAC prompt -> "Ready to Install", click Install (Inno always shows this page
;   when it is the only one) -> files copied -> the pairing window opens:
;   type the 6-digit code from the dashboard -> paired, service installed and
;   started. Wrong code, closed registration or no dashboard are explained in
;   the window and can be retried there.
; Silent (a whole lab at once, run as Administrator):
;   LabSentinelAgent-Setup-<versi>.exe /VERYSILENT /SUPPRESSMSGBOXES /PAIRCODE=123456
;   ... optionally /DASHBOARD=ws://192.168.1.10:8765 if UDP discovery is blocked.
;   Without /PAIRCODE nothing is paired and the service is not installed yet
;   (disk image preparation); pair each clone later from the Start menu.
; Upgrade: pairing is kept; the service is restarted without being reported
;   as stopped by hand.
; Uninstall: removes the service, firewall rule, tray autostart and this PC's
;   token; browser policy is restored.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef DistDir
  #define DistDir "..\..\build\dist"
#endif

#define ServiceName "LabSentinelAgent"
#define DataDir "{commonappdata}\LabSentinel\agent"

[Setup]
AppId={{8C1F6A52-6B7E-4A57-9F7B-1B2D3C4E5F60}
AppName=Lab Sentinel Agent
AppVersion={#AppVersion}
AppPublisher=Lab Sentinel
DefaultDirName={autopf}\LabSentinel\Agent
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=LabSentinelAgent-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=no
UninstallDisplayName=Lab Sentinel Agent
UninstallDisplayIcon={app}\labsentinel-setup.exe

[Languages]
; Wizard buttons use Inno's English texts; every Lab Sentinel message below is
; in Indonesian. Drop an Indonesian.isl next to this file to translate the rest.
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\LabSentinelAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{#DataDir}"

[Icons]
; Pair again later (new dashboard, cloned PC, registration was closed).
Name: "{autoprograms}\Lab Sentinel\Pasangkan PC ini ke dashboard"; \
  Filename: "{app}\labsentinel-setup.exe"

[Run]
; The participant sees the indicator immediately, not only after the next login.
; (If this PC is not paired yet the service starts the indicator itself later.)
Filename: "{app}\labsentinel-tray.exe"; Parameters: "--data-dir ""{#DataDir}"""; \
  Flags: nowait runasoriginaluser skipifsilent; Check: AlreadyPaired

[UninstallRun]
Filename: "{app}\labsentinel-service.exe"; Parameters: "remove"; \
  Flags: runhidden waituntilterminated; RunOnceId: "RemoveService"
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall delete rule name=""Lab Sentinel Agent (penemuan dashboard)"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "RemoveFirewall"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM labsentinel-tray.exe"; \
  Flags: runhidden waituntilterminated; RunOnceId: "StopTray"

[UninstallDelete]
; This PC's credential goes; agent_id stays so a reinstall maps to the same PC.
Type: files; Name: "{#DataDir}\token"
Type: files; Name: "{#DataDir}\running.json"

[Code]
function AlreadyPaired(): Boolean;
begin
  Result := FileExists(ExpandConstant('{#DataDir}\token'));
end;

function PairCodeParam(): String;
begin
  Result := Trim(ExpandConstant('{param:PAIRCODE|}'));
end;

function DashboardParam(): String;
begin
  Result := Trim(ExpandConstant('{param:DASHBOARD|}'));
end;

function RunHidden(const Exe, Params: String): Integer;
var
  Code: Integer;
begin
  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  Log(Format('%s %s -> %d', [Exe, Params, Code]));
  Result := Code;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Existing: String;
begin
  // Upgrade: stop the running service so its files can be replaced. A stop
  // during an upgrade is deliberate, so it must not be reported as tamper:
  // prepare-upgrade waits until the service has fully stopped (it records the
  // stop on its way out) and only then removes that record.
  Existing := ExpandConstant('{app}\labsentinel-service.exe');
  if FileExists(Existing) then
    RunHidden(Existing, 'prepare-upgrade')
  else
  begin
    RunHidden(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}');
    Sleep(5000);
    DeleteFile(ExpandConstant('{#DataDir}\running.json'));
  end;
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/F /IM labsentinel-tray.exe');
  Result := '';
end;

procedure WriteConfig(const Dashboard: String);
begin
  // agent.toml next to the programs; service, tray, CLI and window all read it.
  if Dashboard <> '' then
    SaveStringToFile(ExpandConstant('{app}\agent.toml'),
      'direct_url = "' + Dashboard + '"' + #13#10, False);
end;

procedure InstallAndStartService();
begin
  RunHidden(ExpandConstant('{app}\labsentinel-service.exe'), 'install');
  RunHidden(ExpandConstant('{sys}\sc.exe'), 'start {#ServiceName}');
end;

procedure PairSilently(const Code: String);
var
  RC: Integer;
  Params: String;
begin
  Params := '--pair ' + Code + ' --pair-only';
  if DashboardParam() <> '' then
    Params := Params + ' --dashboard "' + DashboardParam() + '"';
  RC := RunHidden(ExpandConstant('{app}\labsentinel-agent.exe'), Params);
  if RC = 0 then
    InstallAndStartService()
  else
    // Shown only when not suppressed; the reason is also in the setup log.
    SuppressibleMsgBox(Format('Pemasangan ke dashboard gagal (kode %d). Buka menu Start, ' +
      'Lab Sentinel, "Pasangkan PC ini ke dashboard" untuk mencoba lagi.', [RC]),
      mbError, MB_OK, IDOK);
end;

procedure OpenPairingWindow();
var
  Code: Integer;
begin
  // Already elevated by the installer, so the window does not ask again.
  if not Exec(ExpandConstant('{app}\labsentinel-setup.exe'), '', '', SW_SHOWNORMAL,
              ewWaitUntilTerminated, Code) then
    Code := -1;
  Log(Format('pairing window closed -> %d', [Code]));
  // The window installs and starts the service itself when pairing succeeds.
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  WriteConfig(DashboardParam());
  RunHidden(ExpandConstant('{sys}\netsh.exe'),
    'advfirewall firewall delete rule name="Lab Sentinel Agent (penemuan dashboard)"');
  RunHidden(ExpandConstant('{sys}\netsh.exe'),
    'advfirewall firewall add rule name="Lab Sentinel Agent (penemuan dashboard)" ' +
    'dir=in action=allow protocol=UDP localport=50123 ' +
    'program="' + ExpandConstant('{app}\labsentinel-service.exe') + '"');

  if AlreadyPaired() then
    InstallAndStartService()            // upgrade / reinstall: keep the pairing
  else if PairCodeParam() <> '' then
    PairSilently(PairCodeParam())       // /PAIRCODE=...: no window
  else if not WizardSilent() then
    OpenPairingWindow()                 // double-clicked: ask in a window
  else
    // Silent without a code (disk image): nothing paired, service not
    // installed, so the image carries no identity. Pair each clone from the
    // Start menu entry.
    Log('No pairing code: pairing deferred.');
end;
