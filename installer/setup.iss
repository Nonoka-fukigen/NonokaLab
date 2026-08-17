; ============================================================
;  Nonoka Lab 安装脚本（Inno Setup）
;  由 GitHub Actions 调用：iscc /DMyAppVersion=<tag> installer\setup.iss
;  产物：installer\out\NonokaLab_Setup_v<版本>.exe
;  特点：桌面 / 开始菜单快捷方式；可选 FFmpeg 组件（默认勾选）；
;        卸载时保留用户数据（用户文档\NonokaLab）。
; ============================================================

#define MyAppName "Nonoka Lab"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Nonoka"
#define MyAppURL "https://github.com/nonoka-lab/nonoka-lab"
#define MyAppExeName "NonokaLab.exe"

[Setup]
; 私有 GUID，避免与其它软件冲突
AppId={{1A2B3C4D-5E6F-7A8B-9C0D-1E2F3A4B5C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer\out
OutputBaseFilename=NonokaLab_Setup_v{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
UninstallDisplayName={#MyAppName}
DirExistsWarning=no
; 安装过程不强制关闭正在运行的实例（单实例由程序自身保证）
CloseApplications=no
SetupMutex="Global\NonokaLabSetupMutex"

[Languages]
Name: "chs"; MessagesFile: "compiler:ChineseSimplified.isl"

[Components]
Name: "main"; Description: "程序主文件"; Types: full compact; Flags: fixed
Name: "ffmpeg"; Description: "FFmpeg（音视频合并所需，建议勾选）"; Types: full; Flags: disablenouninstallwarning
; 默认勾选 ffmpeg：full 类型包含它；compact 不含。下面用 Tasks/SelectTasks 默认选 full。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "额外快捷方式："; Components: main

[Files]
; 主程序（PyInstaller 单文件 exe）
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion; Components: main
; 可选 FFmpeg 组件：源文件不存在时跳过（CI 未预下载则仅安装主程序）
Source: "packaging\ffmpeg\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist; Components: ffmpeg

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Components: main
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Components: main

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent; Components: main

[UninstallDelete]
; 默认不删除用户数据（用户文档\NonokaLab）。仅清理空目录。
Type: filesandordirs; Name: "{app}";
