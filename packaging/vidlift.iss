#ifndef MyAppVersion
#define MyAppVersion "0.4.2"
#endif

[Setup]
AppId={{30F295B4-8B22-4C3B-9A2F-F722D0868978}
AppName=VidLift
AppVersion={#MyAppVersion}
AppPublisher=YetiWurks LLC
DefaultDirName={localappdata}\Programs\VidLift
DefaultGroupName=VidLift
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\output
OutputBaseFilename=VidLift-{#MyAppVersion}-Windows-x64-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\VidLift.exe
SetupIconFile=app_icon.ico

[Files]
Source: "..\dist\VidLift\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\vidlift-cli.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\VidLift"; Filename: "{app}\VidLift.exe"
Name: "{autodesktop}\VidLift"; Filename: "{app}\VidLift.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\VidLift.exe"; Description: "Launch VidLift"; Flags: nowait postinstall skipifsilent
