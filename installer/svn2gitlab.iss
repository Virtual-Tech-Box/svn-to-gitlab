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
#define AppVersion     "1.0.0"
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

procedure CurStepChanged(CurStep: TSetupStep);
var
  Message: string;
  GitPath, SvnPath, SvnDir: string;
begin
  if CurStep <> ssPostInstall then
    exit;

  GitPath := FindOnPath('git.exe');
  SvnPath := FindOnPath('svn.exe');
  SvnDir := VisualSvnBin();
  Message := '';

  if GitPath = '' then
    Message := Message +
      '- Git for Windows was not found.' + #13#10 +
      '  Install it from https://git-scm.com/download/win using the STANDARD installer.' + #13#10 +
      '  The minimal "MinGit" build omits Perl, and git-svn cannot run without Perl.' + #13#10#13#10;

  if SvnPath = '' then
  begin
    if SvnDir <> '' then
      Message := Message +
        '- A Subversion client was found in the VisualSVN Server folder but is not on PATH:' + #13#10 +
        '    ' + SvnDir + #13#10 +
        '  Either add that folder to the system PATH, or list it under `tool_dirs:`' + #13#10 +
        '  in your migration configuration.' + #13#10#13#10
    else
      Message := Message +
        '- No Subversion command-line client was found.' + #13#10 +
        '  On a VisualSVN Server host it normally lives in' + #13#10 +
        '    C:\Program Files\VisualSVN Server\bin' + #13#10 +
        '  Otherwise install SlikSVN, or TortoiseSVN with the command-line tools' + #13#10 +
        '  component enabled.' + #13#10#13#10;
  end;

  if Message <> '' then
    MsgBox('svn2gitlab is installed, but it needs these tools before it can migrate '
      + 'anything:' + #13#10#13#10 + Message
      + 'Run "svn2gitlab doctor" after installing them to confirm.', mbInformation, MB_OK);
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
