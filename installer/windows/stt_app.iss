#define MyAppName "Voice Dictation App"
#define MyAppPublisher "stt_app"
#define MyAppURL "https://github.com/qwertz92/stt_app"
#define MyAppExeName "stt_app.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.9.0"
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
; {autopf}, not {localappdata}: PrivilegesRequiredOverridesAllowed=dialog
; below lets the user choose an administrative install, and a 'user'
; constant does not switch with it. An admin picking 'Install for all
; users' therefore installed into that admin's own profile, while the
; {auto*} shortcuts went to C:\ProgramData and C:\Users\Public -- visible
; to every user and openable by none of them, because one user's token
; cannot read another's profile, and only that admin could uninstall.
; Inno's own help says the 'user' form must not be used where an
; administrative mode is reachable; its compiler warning for exactly this
; keys on a static PrivilegesRequired=admin, so it never fired here.
; Measured on a per-user install: {autopf} expands to
; C:\Users\<name>\AppData\Local\Programs, byte-identical to the old
; default, so nothing changes for an ordinary install.
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; A silent install has to name its mode -- '/VERYSILENT /CURRENTUSER' or
; '/VERYSILENT /ALLUSERS'. Without one, this directive still puts the
; install-mode dialog up and the process waits on it.
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir={#MyOutputDir}
OutputBaseFilename={#MyOutputBaseFilename}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\..\src\stt_app\assets\app_icon.ico
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
