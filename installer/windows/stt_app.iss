#define MyAppName "Voice Dictation App"
#define MyAppPublisher "stt_app"
#define MyAppURL "https://github.com/qwertz92/stt_app"
#define MyAppExeName "stt_app.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

#ifndef MyReleaseDir
  #error MyReleaseDir must be passed on the ISCC command line.
#endif

#ifndef MyOutputDir
  #define MyOutputDir "release"
#endif

#ifndef MyOutputBaseFilename
  #define MyOutputBaseFilename "stt_app-win-x64-setup"
#endif

[Setup]
AppId={{5E851C8F-C8C4-4C0F-AF59-76D253DA6689}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir={#MyOutputDir}
OutputBaseFilename={#MyOutputBaseFilename}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupLogging=yes
ChangesAssociations=no
ChangesEnvironment=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#MyReleaseDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent
