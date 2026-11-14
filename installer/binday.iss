; Windows installer for Binday (Inno Setup 6). Packages the folder `flet build windows` makes.
;   iscc /DAppVersion=1.1.0 /DSourceDir=..\build\windows installer\binday.iss
; Installs per user by default (no admin prompt); the first page offers "all users" for admins.
; Uninstalling leaves ~/.binday (saved profiles and logs) and keychain passwords alone.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; Windows file versions are four numbers: drop any pre-release suffix ("1.2.0-rc1" -> "1.2.0")
#define NumericVersion Copy(AppVersion, 1, Pos("-", AppVersion + "-") - 1)
#ifndef SourceDir
  #define SourceDir "..\build\windows"
#endif

[Setup]
AppId={{BE7F4A61-2BE3-4B8D-BAC6-43192399B887}
AppName=Binday
AppVersion={#AppVersion}
AppVerName=Binday {#AppVersion}
AppPublisher=Binday contributors
AppPublisherURL=https://github.com/zaryabmustafa/binday
AppSupportURL=https://github.com/zaryabmustafa/binday/issues
AppUpdatesURL=https://github.com/zaryabmustafa/binday/releases
AppCopyright=Copyright (c) 2026 Zaryab Mustafa
VersionInfoVersion={#NumericVersion}.0
; the Flutter build needs Windows 10 or later
MinVersion=10.0
VersionInfoDescription=Binday for Mattermost setup
DefaultDirName={autopf}\Binday
DefaultGroupName=Binday
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\binday.exe
UninstallDisplayName=Binday for Mattermost
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
OutputDir=..\dist
OutputBaseFilename=Binday-setup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; an upgrade must not leave the previous version's Python packages or app code next to the new ones
Type: filesandordirs; Name: "{app}\site-packages"
Type: filesandordirs; Name: "{app}\Lib"
Type: filesandordirs; Name: "{app}\DLLs"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\data"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Binday"; Filename: "{app}\binday.exe"
Name: "{group}\Uninstall Binday"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Binday"; Filename: "{app}\binday.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\binday.exe"; Description: "{cm:LaunchProgram,Binday}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallDelete]
; files the app writes next to itself at runtime (Python bytecode caches); user data lives in ~/.binday and stays
Type: filesandordirs; Name: "{app}\app\__pycache__"
