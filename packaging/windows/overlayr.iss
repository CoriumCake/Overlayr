; Inno Setup script for the Windows installer. The release workflow builds
; it with: ISCC /DVersion=<version> /DSource=<folder with overlayr.exe>
; packaging\windows\overlayr.iss
;
; It installs for the current user only, so no administrator rights are
; needed, and registers overlayr:// so the website's login reaches the app.

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Source
  #define Source "..\..\rust\target\release"
#endif

[Setup]
AppId={{6C1F2A51-7E64-4B2B-9C0A-0F3D7B6E2A91}
AppName=Overlayr
AppVersion={#Version}
AppPublisher=Overlayr
AppPublisherURL=https://overlayr.app
AppSupportURL=https://overlayr.app/support
DefaultDirName={localappdata}\Programs\Overlayr
DefaultGroupName=Overlayr
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\rust\target\windows
OutputBaseFilename=Overlayr-{#Version}-windows-x64-setup
SetupIconFile=..\..\Icon.ico
UninstallDisplayIcon={app}\overlayr.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Files]
Source: "{#Source}\overlayr.exe"; DestDir: "{app}"; Flags: ignoreversion
; Tesseract and the neural OCR engine (packaging/fetch-ocr.sh).
Source: "{#Source}\ocr\*"; DestDir: "{app}\ocr"; Excludes: "crt"; Flags: ignoreversion recursesubdirs
; The Visual C++ runtime ONNX Runtime needs, where Windows looks for it.
Source: "{#Source}\ocr\crt\*"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\Icon.ico"; DestDir: "{app}"; DestName: "overlayr.ico"; Flags: ignoreversion

[InstallDelete]
; Files from an older version's OCR bundle.
Type: filesandordirs; Name: "{app}\ocr"

[Icons]
Name: "{autoprograms}\Overlayr"; Filename: "{app}\overlayr.exe"; IconFilename: "{app}\overlayr.ico"
Name: "{autodesktop}\Overlayr"; Filename: "{app}\overlayr.exe"; IconFilename: "{app}\overlayr.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Registry]
; The overlayr:// login link, removed again on uninstall.
Root: HKCU; Subkey: "Software\Classes\overlayr"; ValueType: string; ValueName: ""; ValueData: "URL:Overlayr Protocol"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\overlayr"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\overlayr\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\overlayr.ico"""
Root: HKCU; Subkey: "Software\Classes\overlayr\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\overlayr.exe"" ""%1"""

[Run]
Filename: "{app}\overlayr.exe"; Description: "{cm:LaunchProgram,Overlayr}"; Flags: nowait postinstall skipifsilent
; Updates from inside Overlayr (rust/src/updater.rs) install silently
; with /RELAUNCH=1, and start Overlayr again afterwards.
Filename: "{app}\overlayr.exe"; Flags: nowait; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
