; Lab Sentinel Agent installer (Inno Setup 6).
;
; Built by packaging/build.py --installer, which passes AppVersion and DistDir.
;
; Interactive:  LabSentinelAgent-Setup-<versi>.exe
;               asks for the 6-digit pairing code shown on the dashboard.
; Silent (for deploying a whole lab, run as Administrator):
;   LabSentinelAgent-Setup-<versi>.exe /VERYSILENT /SUPPRESSMSGBOXES /PAIRCODE=123456
;   ... optionally /DASHBOARD=ws://192.168.1.10:8765 if UDP discovery is blocked.
;
; What it does, in order:
;   1. stops an older agent service (upgrade), copies files;
;   2. pairs this PC (labsentinel-agent --pair KODE --pair-only) unless already paired;
;   3. installs and starts the Windows Service (recovery + tray autostart + token ACL);
;   4. opens UDP 50123 inbound for dashboard discovery;
;   5. starts the tray indicator for whoever is logged in now.
; Uninstall reverses 3-4, restores browser policy and removes this PC's token.

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
DisableDirPage=auto
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
UninstallDisplayIcon={app}\labsentinel-agent.exe

[Languages]
; Wizard buttons use Inno's English texts; every Lab Sentinel message below is
; in Indonesian. Drop an Indonesian.isl next to this file to translate the rest.
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\LabSentinelAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{#DataDir}"

[Run]
; The participant sees the indicator immediately, not only after the next login.
Filename: "{app}\labsentinel-tray.exe"; Parameters: "--data-dir ""{#DataDir}"""; \
  Flags: nowait runasoriginaluser skipifsilent

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
var
  PairPage: TInputQueryWizardPage;

function AlreadyPaired(): Boolean;
begin
  Result := FileExists(ExpandConstant('{#DataDir}\token'));
end;

function DigitsOnly(const S: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 1 to Length(S) do
    if (S[I] >= '0') and (S[I] <= '9') then
      Result := Result + S[I];
end;

procedure InitializeWizard();
begin
  PairPage := CreateInputQueryPage(wpSelectDir,
    'Pasangkan ke dashboard',
    'Masukkan kode 6 digit dari dashboard pengawas.',
    'Kode tampil di dashboard: halaman Aturan, kartu "Pemasangan PC baru". ' +
    'Alamat dashboard boleh dikosongkan; agent menemukannya sendiri lewat jaringan lab.');
  PairPage.Add('Kode pemasangan (6 digit):', False);
  PairPage.Add('Alamat dashboard (opsional, contoh ws://192.168.1.10:8765):', False);
  PairPage.Values[0] := ExpandConstant('{param:PAIRCODE|}');
  PairPage.Values[1] := ExpandConstant('{param:DASHBOARD|}');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // An upgrade keeps its pairing; there is nothing to type.
  Result := (PageID = PairPage.ID) and AlreadyPaired();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = PairPage.ID then
    if Length(DigitsOnly(PairPage.Values[0])) <> 6 then
    begin
      MsgBox('Kode pemasangan harus 6 digit angka.', mbError, MB_OK);
      Result := False;
    end;
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
begin
  // Upgrade: stop the running service so its files can be replaced. A stop
  // during an upgrade is deliberate, so do not leave it reported as tamper.
  RunHidden(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}');
  Sleep(3000);
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/F /IM labsentinel-tray.exe');
  DeleteFile(ExpandConstant('{#DataDir}\running.json'));
  Result := '';
end;

procedure WriteConfig(const Dashboard: String);
begin
  // agent.toml next to the programs; service, tray and CLI all read it.
  if Dashboard <> '' then
    SaveStringToFile(ExpandConstant('{app}\agent.toml'),
      'direct_url = "' + Dashboard + '"' + #13#10, False);
end;

procedure PairThisPC();
var
  Code: Integer;
  Params, Reason: String;
begin
  Params := '--pair ' + DigitsOnly(PairPage.Values[0]) + ' --pair-only';
  if Trim(PairPage.Values[1]) <> '' then
    Params := Params + ' --dashboard "' + Trim(PairPage.Values[1]) + '"';
  Code := RunHidden(ExpandConstant('{app}\labsentinel-agent.exe'), Params);
  if Code = 0 then
    Exit;
  case Code of
    3: Reason := 'Dashboard menolak kode ini (kode salah, kedaluwarsa, ' +
                 'atau pendaftaran PC baru sedang ditutup).';
    5: Reason := 'Dashboard tidak ditemukan di jaringan. Pastikan dashboard berjalan, ' +
                 'atau isi alamat dashboard.';
  else
    Reason := Format('Pemasangan gagal (kode keluar %d).', [Code]);
  end;
  SuppressibleMsgBox(Reason + #13#10#13#10 +
    'Agent tetap terpasang tetapi belum aktif. Setelah masalahnya beres, jalankan ' +
    'sebagai Administrator:' + #13#10 +
    '  "' + ExpandConstant('{app}') + '\labsentinel-agent.exe" --pair-only' + #13#10 +
    '  sc start {#ServiceName}', mbError, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  WriteConfig(Trim(PairPage.Values[1]));
  if not AlreadyPaired() then
    PairThisPC();
  RunHidden(ExpandConstant('{app}\labsentinel-service.exe'), 'install');
  RunHidden(ExpandConstant('{sys}\netsh.exe'),
    'advfirewall firewall delete rule name="Lab Sentinel Agent (penemuan dashboard)"');
  RunHidden(ExpandConstant('{sys}\netsh.exe'),
    'advfirewall firewall add rule name="Lab Sentinel Agent (penemuan dashboard)" ' +
    'dir=in action=allow protocol=UDP localport=50123 ' +
    'program="' + ExpandConstant('{app}\labsentinel-service.exe') + '"');
  RunHidden(ExpandConstant('{sys}\sc.exe'), 'start {#ServiceName}');
end;
