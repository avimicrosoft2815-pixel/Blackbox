; BlackBox — Windows installer (Inno Setup 6).
;
; Build (on a Windows machine, after `build_windows.ps1` has staged `win64\`):
;
;     iscc tools\package\windows\blackbox.iss /DAppVersion=0.1.0.dev0
;
; What it installs: a private Python (the embeddable distribution, staged by `build_windows.ps1`), the BlackBox package
; with `pywebview` vendored beside it, a Start Menu entry, an optional desktop icon, and a file association so a
; double-clicked `.bbx` capsule opens in the viewer. No service, no account, no firewall rule: the app only ever listens
; on 127.0.0.1 for the lifetime of one window.
;
; What it deliberately does not do: delete the user's recordings on uninstall. A recorder that erases evidence when it is
; uninstalled is a recorder whose evidence cannot be trusted; the uninstaller says where the data is and leaves it.
;
; THE OWNER MUST SUPPLY, and this file cannot: an Authenticode signing certificate and its password. Without one the
; installer shows a SmartScreen warning. Do not ship this unsigned build as a release.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppName "BlackBox"
#define AppPublisher "BlackBox contributors"

[Setup]
AppId={{7A1E2C6B-5D4C-4F0E-9A2E-3B5C7D9E1F20}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\BlackBox
DefaultGroupName=BlackBox
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=BlackBox-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; A signed build sets these two; leaving them commented is the honest default until a certificate exists.
; SignTool=signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 $f
; SignedUninstaller=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesAssociations=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\blackbox.ico
; No LicenseFile: the licence choice is the owner's and no LICENCE file exists yet.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "associate"; Description: "Open .bbx capsules with BlackBox"; GroupDescription: "File types:"

[Files]
Source: "win64\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "blackbox.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\..\docs\desktop.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\BlackBox"; Filename: "{app}\blackbox.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\blackbox.ico"
Name: "{group}\Uninstall BlackBox"; Filename: "{uninstallexe}"
Name: "{autodesktop}\BlackBox"; Filename: "{app}\blackbox.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\blackbox.ico"; Tasks: desktopicon

[Registry]
; The association is written under HKCU because the installer runs without elevation by default.
Root: HKCU; Subkey: "Software\Classes\.bbx"; ValueType: string; ValueName: ""; ValueData: "BlackBox.Capsule"; Flags: uninsdeletevalue; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\BlackBox.Capsule"; ValueType: string; ValueName: ""; ValueData: "BlackBox capsule"; Flags: uninsdeletekey; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\BlackBox.Capsule\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\blackbox.ico"; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\BlackBox.Capsule\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\blackbox.cmd"" desktop ""%1"""; Tasks: associate
Root: HKCU; Subkey: "Software\Classes\BlackBox.Capsule"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "BlackBox capsule"; Tasks: associate

[Run]
Filename: "{app}\blackbox.cmd"; Parameters: "desktop"; Description: "Open BlackBox now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only files this installer created in its own directory. Recordings live in %LOCALAPPDATA%\BlackBox and are kept.
Type: filesandordirs; Name: "{app}\win64"

[Code]
procedure InitializeWizard();
begin
  MsgBox('BlackBox stores everything on this computer: no account, no cloud, no telemetry.' + #13#10 +
         'Your recordings are kept when you uninstall. Delete them yourself, deliberately, with:' + #13#10 +
         '    blackbox ls   then   blackbox gc --dry-run', mbInformation, MB_OK);
end;
