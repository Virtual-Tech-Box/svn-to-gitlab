; Inno Setup script for svn2gitlab.
;
; Produces svn2gitlab-setup-<version>.exe: a per-machine installer that puts the
; tool in Program Files, adds it to the system PATH, creates a working directory
; under ProgramData, and checks for the external tools the migration needs.
;
; Build (on Windows, after `pyinstaller installer/svn2gitlab.spec`):
;   iscc installer\svn2gitlab.iss
;
; Target: Windows Server 2019 and later, x64.
; Server 2019 is the oldest supported target, so the minimum is set there.

#define AppName        "svn2gitlab"
#define AppURL         "https://github.com/Virtual-Tech-Box/svn-to-gitlab"
#define AppVersion     "1.1.0-rc1"
#define AppPublisher   "Virtual Tech Box"
#define AppExeName     "svn2gitlab.exe"
#define SourceDir      "..\dist\svn2gitlab"

[Setup]
AppId={{8F3A6C21-4E5B-4C7D-9A18-2D5E7B9C0A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=svn2gitlab-setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}
ChangesEnvironment=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "tfs";  Description: "Migrate from TFS / Azure DevOps (TFVC) - needs Git only"
Name: "svn";  Description: "Migrate from Subversion - needs Git and a Subversion client"
Name: "both"; Description: "Both sources"

[Components]
Name: "core"; Description: "svn2gitlab"; Types: tfs svn both; Flags: fixed
Name: "prereq_git"; Description: "Git for Windows (required, installed if missing)"; Types: tfs svn both
Name: "prereq_svn"; Description: "Subversion command-line client (only for SVN migrations)"; Types: svn both

[Tasks]
Name: "addtopath"; Description: "Add svn2gitlab to the system PATH"; GroupDescription: "Integration:"
Name: "desktopicon"; Description: "Create a Start Menu shortcut for the web dashboard"; GroupDescription: "Integration:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}\docs"; DestName: "README.md"; Flags: ignoreversion
Source: "..\docs\RUNBOOK.md"; DestDir: "{app}\docs"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\examples\*"; DestDir: "{app}\examples"; Flags: ignoreversion recursesubdirs skipifsourcedoesntexist

[Dirs]
; Migrations write here by default. Modify permission is granted to local
; Administrators only; migration work directories can hold repository content.
Name: "{commonappdata}\svn2gitlab"; Permissions: admins-modify
Name: "{commonappdata}\svn2gitlab\work"; Permissions: admins-modify
Name: "{commonappdata}\svn2gitlab\logs"; Permissions: admins-modify

[Icons]
Name: "{group}\svn2gitlab dashboard"; Filename: "{app}\{#AppExeName}"; \
  Parameters: "serve"; WorkingDir: "{commonappdata}\svn2gitlab"; Tasks: desktopicon
Name: "{group}\svn2gitlab command prompt"; Filename: "{cmd}"; \
  Parameters: "/K ""cd /d {commonappdata}\svn2gitlab && echo svn2gitlab {#AppVersion} && svn2gitlab --help"""
Name: "{group}\Documentation"; Filename: "{app}\docs\README.md"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Registry]
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; \
  Check: NeedsPathEntry(ExpandConstant('{app}')); Tasks: addtopath

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "doctor"; \
  Description: "Check that Git, git-svn and Subversion are available"; \
  Flags: postinstall runascurrentuser
Filename: "{app}\{#AppExeName}"; Parameters: "init --output ""{commonappdata}\svn2gitlab\migration.yaml"""; \
  Description: "Create a starter migration configuration"; \
  Flags: postinstall runascurrentuser skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
{ ---- PATH handling ---------------------------------------------------------- }

function NeedsPathEntry(Param: string): Boolean;
var
  Existing: string;
begin
  if not RegQueryStringValue(HKEY_LOCAL_MACHINE,
    'SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'Path', Existing) then
  begin
    Result := True;
    exit;
  end;
  { Wrap both sides in separators so a partial match cannot produce a false negative. }
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(Existing) + ';') = 0;
end;

{ ---- Dependency detection --------------------------------------------------- }

function FindOnPath(const ExeName: string): string;
var
  ResultCode: Integer;
  TempFile, Output: string;
  Lines: TArrayOfString;
begin
  Result := '';
  TempFile := ExpandConstant('{tmp}\which.txt');
  if Exec(ExpandConstant('{cmd}'), '/C where ' + ExeName + ' > "' + TempFile + '" 2>&1',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if (ResultCode = 0) and LoadStringsFromFile(TempFile, Lines) and (GetArrayLength(Lines) > 0) then
      Result := Trim(Lines[0]);
  end;
  DeleteFile(TempFile);
end;

function DirExistsAny(const Paths: TArrayOfString): string;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to GetArrayLength(Paths) - 1 do
    if DirExists(Paths[I]) then
    begin
      Result := Paths[I];
      exit;
    end;
end;

function VisualSvnBin(): string;
var
  Candidates: TArrayOfString;
begin
  SetArrayLength(Candidates, 2);
  Candidates[0] := ExpandConstant('{pf}\VisualSVN Server\bin');
  Candidates[1] := ExpandConstant('{pf32}\VisualSVN Server\bin');
  Result := DirExistsAny(Candidates);
end;

{ ---- Prerequisites ---------------------------------------------------------- }

{ Downloading rather than bundling is deliberate. Git and Subversion are separately
  licensed (GPLv2 and Apache-2.0), and redistributing them inside this installer
  would carry obligations we cannot discharge from here - including offering
  matching source. Fetching the official installer on demand also means the user
  always gets a current, security-patched build rather than whatever was current
  when this package was cut. }

const
  { Pinned to a verified release asset. Refresh this when cutting a new installer:
    the URL is checked during the release build, and a stale one fails loudly there
    rather than silently at a customer site. }
  GitInstallerUrl =
    'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/' +
    'Git-2.55.0.5-64-bit.exe';

var
  DownloadPage: TDownloadWizardPage;
  NeedGit, NeedSvn: Boolean;

function OnDownloadProgress(const Url, FileName: string;
                            const Progress, ProgressMax: Int64): Boolean;
begin
  if ProgressMax <> 0 then
    Log(Format('Downloaded %d of %d bytes', [Progress, ProgressMax]));
  Result := True;
end;

procedure InitializeWizard();
begin
  DownloadPage := CreateDownloadPage(
    SetupMessage(msgWizardPreparing),
    'Fetching prerequisites from their official sources',
    @OnDownloadProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID <> wpReady then
    exit;

  NeedGit := IsComponentSelected('prereq_git') and (FindOnPath('git.exe') = '');
  { Subversion is deliberately not auto-installed. There is no single official
    Windows build with a stable download URL - the common ones are third-party
    repackagings - so guessing at one would be less reliable than telling the
    operator exactly what to install. TFS migrations do not need it at all. }
  NeedSvn := False;

  if not NeedGit then
    exit;

  if MsgBox('Git was not found on this machine, and every migration needs it.'
    + #13#10#13#10
    + 'Download and install Git for Windows now?' + #13#10#13#10
    + 'This requires internet access. Choose No to skip and install it yourself'
    + ' later from https://git-scm.com/download/win.',
    mbConfirmation, MB_YESNO) = IDNO then
  begin
    NeedGit := False;
    exit;
  end;

  DownloadPage.Clear;
  DownloadPage.Add(GitInstallerUrl, 'git-setup.exe', '');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      { A failed download must not fail the whole install - svn2gitlab itself is
        already unpacked, and `svn2gitlab doctor` will say exactly what is missing. }
      MsgBox('Git could not be downloaded:' + #13#10#13#10
        + GetExceptionMessage + #13#10#13#10
        + 'Installation will continue. Install Git manually from'
        + ' https://git-scm.com/download/win and run "svn2gitlab doctor".',
        mbInformation, MB_OK);
      NeedGit := False;
    end;
  finally
    DownloadPage.Hide;
  end;
end;

procedure InstallPrerequisites();
var
  ResultCode: Integer;
begin
  if NeedGit then
  begin
    { /VERYSILENT with the components Git for Windows needs for this tool. } 
    if not Exec(ExpandConstant('{tmp}\git-setup.exe'),
                '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS',
                '', SW_SHOW, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      MsgBox('Git for Windows did not install cleanly (code '
        + IntToStr(ResultCode) + '). Install it from https://git-scm.com/download/win'
        + ' and run "svn2gitlab doctor".', mbError, MB_OK);
  end;

end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Message: string;
  GitPath, SvnPath, SvnDir: string;
begin
  if CurStep <> ssPostInstall then
    exit;

  InstallPrerequisites();

  GitPath := FindOnPath('git.exe');
  SvnPath := FindOnPath('svn.exe');
  SvnDir := VisualSvnBin();
  Message := '';

  if GitPath = '' then
    Message := Message +
      '- Git was not found. It is required for every migration.' + #13#10 +
      '  https://git-scm.com/download/win' + #13#10#13#10;

  { Subversion matters only for SVN migrations. Demanding it on a machine that is
    only migrating TFS would report a healthy setup as broken. }
  if IsComponentSelected('prereq_svn') and (SvnPath = '') then
  begin
    if SvnDir <> '' then
      Message := Message +
        '- A Subversion client exists in the VisualSVN Server folder but is not on' + #13#10 +
        '  PATH:  ' + SvnDir + #13#10 +
        '  Add it to PATH, or list it under `tool_dirs:` in your configuration.' + #13#10#13#10
    else
      Message := Message +
        '- No Subversion command-line client was found. This is only needed for' + #13#10 +
        '  Subversion migrations, not for TFS/TFVC.' + #13#10 +
        '  On a VisualSVN Server host it lives in' + #13#10 +
        '    C:\Program Files\VisualSVN Server\bin' + #13#10#13#10;
  end;

  if Message <> '' then
    MsgBox('svn2gitlab is installed. Before migrating, note:'
      + #13#10#13#10 + Message
      + 'Run "svn2gitlab doctor" to see exactly what this machine can migrate.',
      mbInformation, MB_OK);
end;

{ ---- Uninstall guard -------------------------------------------------------- }

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if MsgBox('Remove svn2gitlab?' + #13#10#13#10
    + 'Migration work directories and reports under' + #13#10
    + ExpandConstant('{commonappdata}\svn2gitlab') + #13#10
    + 'are NOT deleted, so an in-progress migration can be resumed after '
    + 'reinstalling.', mbConfirmation, MB_YESNO) = IDNO then
    Result := False;
end;
