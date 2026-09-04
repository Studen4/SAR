[Setup]
AppName=SAR - Stalzone Analitic Review
AppVersion=1.0
DefaultDirName={autopf}\SAR
DefaultGroupName=SAR
OutputDir=installer_output
OutputBaseFilename=SAR_Setup
Compression=lzma2
SolidCompression=yes
; Іконка для самого файлу інсталятора Setup.exe
SetupIconFile=SAR.ico

[Languages]
; Опції вибору мови при старті та прив'язка відповідного текстового файлу
Name: "ua"; MessagesFile: "compiler:Languages\Ukrainian.isl"; InfoBeforeFile: "disclaimer_ua.txt"
Name: "en"; MessagesFile: "compiler:Default.isl"; InfoBeforeFile: "disclaimer_en.txt"

[Tasks]
; Створює чекбокс "Створити ярлик на робочому столі" під час встановлення
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Перенос файлів програми
Source: "dist\SAR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Перенос файлу іконки в папку програми, щоб ярлик міг на неї посилатися
Source: "SAR.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Ярлик у меню Пуск (створюється завжди)
Name: "{autoprograms}\SAR - Stalzone Analitic Review"; Filename: "{app}\SAR.exe"; IconFilename: "{app}\SAR.ico"
; Ярлик на робочому столі (створюється ТІЛЬКИ якщо обрано чекбокс)
Name: "{userdesktop}\SAR - Stalzone Analitic Review"; Filename: "{app}\SAR.exe"; Tasks: desktopicon; IconFilename: "{app}\SAR.ico"

[Run]
; ЕКРАН 3: Ініціалізація та скачування браузера Camoufox під час підготовки (невидимо)
Filename: "{app}\SAR.exe"; Parameters: "--fetch-only"; StatusMsg: "Встановлення та налаштування Camoufox браузера..."; Flags: runhidden

; Фінальний екран: Чекбокс "Запустити програму"
Filename: "{app}\SAR.exe"; Description: "Запустити SAR - Stalzone Analitic Review"; Flags: postinstall nowait skipifsilent
