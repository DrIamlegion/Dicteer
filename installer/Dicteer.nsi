; Dicteer - Windows installer (NSIS 3).
;
; Per-user install: no administrator prompt, no UAC, no machine-wide files.
; The program goes to %LOCALAPPDATA%\Programs\Dicteer, the app's own
; data stays in %APPDATA%\Dicteer and is only removed when the user
; asks for that during uninstall.
;
; Build with build-installer.ps1, or:
;   makensis /DAPP_VERSION=2.0 /DAPP_FILEVERSION=2.0.0.0 installer\Dicteer.nsi

Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64
Name "Dicteer"
BrandingText "Dicteer"

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "2.0.3"
!endif
!ifndef APP_FILEVERSION
  !define APP_FILEVERSION "2.0.3.0"
!endif

!define APP_NAME "Dicteer"
!define APP_EXE "Dicteer.exe"
!define APP_PUBLISHER "Dicteer"
!define APP_DATA_DIR "$APPDATA\Dicteer"
!define APP_REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Dicteer"
; build-installer.ps1 passes an absolute project root; the default works when
; makensis is started from the project folder.
!ifndef PROJECT_ROOT
  !define PROJECT_ROOT ".."
!endif
!define ROOT "${PROJECT_ROOT}"
!define SOURCE_DIR "${ROOT}\dist\Dicteer"

; Set when a 1.x install (then named "Freebuff Dictate") started with Windows.
Var HadLegacyAutostart

VIProductVersion "${APP_FILEVERSION}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright" "${APP_PUBLISHER}"

OutFile "${ROOT}\dist\Dicteer-Setup-${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel user
XPStyle on

!define MUI_ICON "${ROOT}\assets\icon.ico"
!define MUI_UNICON "${ROOT}\assets\icon.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "$(RunApp)"

!define MUI_LANGDLL_REGISTRY_ROOT "HKCU"
!define MUI_LANGDLL_REGISTRY_KEY "Software\${APP_NAME}"
!define MUI_LANGDLL_REGISTRY_VALUENAME "InstallerLanguage"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Dutch"
!insertmacro MUI_LANGUAGE "English"

LangString RunApp ${LANG_DUTCH} "Dicteer nu starten"
LangString RunApp ${LANG_ENGLISH} "Start Dicteer now"
LangString SecApp ${LANG_DUTCH} "Dicteer (vereist)"
LangString SecApp ${LANG_ENGLISH} "Dicteer (required)"
LangString SecAppDesc ${LANG_DUTCH} "De app zelf: dicteren, instellingen, tray-pictogram. Ongeveer 300 MB."
LangString SecAppDesc ${LANG_ENGLISH} "The app itself: dictation, settings, tray icon. About 300 MB."
LangString SecDesktop ${LANG_DUTCH} "Snelkoppeling op het bureaublad"
LangString SecDesktop ${LANG_ENGLISH} "Desktop shortcut"
LangString SecDesktopDesc ${LANG_DUTCH} "Een pictogram op het bureaublad."
LangString SecDesktopDesc ${LANG_ENGLISH} "An icon on the desktop."
LangString SecStartup ${LANG_DUTCH} "Starten wanneer ik inlog"
LangString SecStartup ${LANG_ENGLISH} "Start when I sign in"
LangString SecStartupDesc ${LANG_DUTCH} "Start de app verborgen in het systeemvak bij het inloggen. Later aan of uit te zetten in de app."
LangString SecStartupDesc ${LANG_ENGLISH} "Starts the app hidden in the notification area at sign-in. Can be changed later in the app."
LangString UninstallLink ${LANG_DUTCH} "Dicteer verwijderen"
LangString UninstallLink ${LANG_ENGLISH} "Uninstall Dicteer"
LangString Need64 ${LANG_DUTCH} "Dicteer vereist 64-bits Windows."
LangString Need64 ${LANG_ENGLISH} "Dicteer requires 64-bit Windows."
LangString NeedWebView2 ${LANG_DUTCH} "Dicteer gebruikt de Microsoft Edge WebView2-runtime voor zijn venster, en die lijkt niet aanwezig te zijn.$\r$\n$\r$\nWilt u de downloadpagina openen? De app kan zonder die runtime niet starten."

LangString NeedWebView2 ${LANG_ENGLISH} "Dicteer uses the Microsoft Edge WebView2 runtime for its window, and it does not appear to be installed.$\r$\n$\r$\nDo you want to open the download page? The app cannot start without it."

LangString AskDeleteData ${LANG_DUTCH} "Wilt u ook uw dictaten, geschiedenis en instellingen verwijderen?$\r$\n$\r$\nJa verwijdert %APPDATA%\Dicteer definitief (geschiedenis, leerresultaten, opgeslagen Groq-sleutel, Whisper-modellen).$\r$\nNee laat ze staan, zodat een volgende installatie ze weer gebruikt."
LangString AskDeleteData ${LANG_ENGLISH} "Do you also want to delete your dictations, history and settings?$\r$\n$\r$\nYes permanently deletes %APPDATA%\Dicteer (history, learned corrections, stored Groq key, Whisper models).$\r$\nNo keeps them, so a later installation can reuse them."
LangString UninstallRunning ${LANG_DUTCH} "Dicteer wordt afgesloten zodat de bestanden vervangen kunnen worden."
LangString UninstallRunning ${LANG_ENGLISH} "Closing Dicteer so its files can be replaced."
LangString RemovingLegacy ${LANG_DUTCH} "De oude versie (Freebuff Dictate) wordt verwijderd; uw gegevens blijven bewaard."
LangString RemovingLegacy ${LANG_ENGLISH} "Removing the old Freebuff Dictate install; your data is kept."

; --------------------------------------------------------------------------
; install
; --------------------------------------------------------------------------
Function .onInit
  !insertmacro MUI_LANGDLL_DISPLAY

  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "$(Need64)"
    Abort
  ${EndIf}

  ; Soft check: Windows 11 and recent Windows 10 already have WebView2. Asking
  ; beats a window that never appears on an older machine.
  IfSilent webview_done
  ; WebView2 is registered per machine (32- or 64-bit view) or per user.
  SetRegView 32
  ReadRegStr $0 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  SetRegView 64
  ReadRegStr $2 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ReadRegStr $1 HKCU "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $0 == ""
  ${AndIf} $1 == ""
  ${AndIf} $2 == ""
    MessageBox MB_YESNO|MB_ICONINFORMATION "$(NeedWebView2)" IDNO webview_done
    ExecShell "open" "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
  ${EndIf}
  webview_done:
FunctionEnd

Function StopRunningApp
  DetailPrint "$(UninstallRunning)"
  nsExec::Exec '"$SYSDIR\taskkill.exe" /IM "${APP_EXE}" /F'
  Pop $0
  Sleep 600
FunctionEnd

Section "$(SecApp)" SEC_APP
  SectionIn RO
  SetShellVarContext current
  Call StopRunningApp

  ; A 1.x install used the old product name and would otherwise stay in Apps &
  ; Features next to this one. Its silent uninstall keeps the user's data, which
  ; the app moves into %APPDATA%\Dicteer on its first start. Remember whether it
  ; started with Windows so that setting survives the rename too.
  StrCpy $HadLegacyAutostart 0
  IfFileExists "$SMSTARTUP\Freebuff Dictate.lnk" 0 legacy_startup_checked
    StrCpy $HadLegacyAutostart 1
  legacy_startup_checked:
  Delete "$SMSTARTUP\Freebuff Dictate.lnk"
  Delete "$DESKTOP\Freebuff Dictate.lnk"
  IfFileExists "$LOCALAPPDATA\Programs\Freebuff Dictate\uninstall.exe" 0 legacy_done
    DetailPrint "$(RemovingLegacy)"
    nsExec::Exec '"$LOCALAPPDATA\Programs\Freebuff Dictate\uninstall.exe" /S'
    Pop $0
    Sleep 1500
    RMDir /r "$LOCALAPPDATA\Programs\Freebuff Dictate"
  legacy_done:

  SetOutPath "$INSTDIR"
  ; Old builds kept their Python libraries in _internal; removing it prevents
  ; leftovers from a previous version being loaded.
  RMDir /r "$INSTDIR\_internal"
  File /r "${SOURCE_DIR}\*.*"
  File "${ROOT}\installer\default-config.json"

  ; First install only: seed the Dutch defaults the app is delivered with.
  ; An existing config (upgrade, reinstall) is never touched. When 1.x data is
  ; waiting under the old name, leave this folder empty so that the app's own
  ; migration can move everything across instead of finding a fresh config.
  IfFileExists "$APPDATA\Dicteer\config.json" config_ready
  IfFileExists "$APPDATA\Freebuff Dictate\config.json" config_ready
  CreateDirectory "$APPDATA\Dicteer"
  CopyFiles /SILENT "$INSTDIR\default-config.json" "$APPDATA\Dicteer\config.json"
  config_ready:

  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\$(UninstallLink).lnk" "$INSTDIR\uninstall.exe"

  ; Whoever started 1.x with Windows keeps that behaviour without ticking the
  ; box again; the shortcut above was removed with the old install.
  ${If} $HadLegacyAutostart == 1
    CreateShortCut "$SMSTARTUP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--hidden" "$INSTDIR\${APP_EXE}" 0
  ${EndIf}

  WriteRegStr HKCU "Software\${APP_NAME}" "InstallDir" "$INSTDIR"

  WriteRegStr HKCU "${APP_REGKEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${APP_REGKEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${APP_REGKEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${APP_REGKEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${APP_REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${APP_REGKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${APP_REGKEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${APP_REGKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${APP_REGKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${APP_REGKEY}" "EstimatedSize" $0
SectionEnd

Section "$(SecDesktop)" SEC_DESKTOP
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section /o "$(SecStartup)" SEC_STARTUP
  CreateShortCut "$SMSTARTUP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "--hidden" "$INSTDIR\${APP_EXE}" 0
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_APP} "$(SecAppDesc)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} "$(SecDesktopDesc)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_STARTUP} "$(SecStartupDesc)"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; --------------------------------------------------------------------------
; uninstall
; --------------------------------------------------------------------------
Function un.onInit
  !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  nsExec::Exec '"$SYSDIR\taskkill.exe" /IM "${APP_EXE}" /F'
  Pop $0
  Sleep 600

  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMSTARTUP\${APP_NAME}.lnk"
  ; Both shortcuts, whichever language the installer used.
  Delete "$SMPROGRAMS\${APP_NAME}\*.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\uninstall.exe"
  Delete "$INSTDIR\default-config.json"
  RMDir /r "$INSTDIR\_internal"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${APP_REGKEY}"
  DeleteRegKey HKCU "Software\${APP_NAME}"

  ; Silent uninstall (used by package managers) keeps the user's data.
  ${If} ${Silent}
    Goto done
  ${EndIf}
  MessageBox MB_YESNO|MB_ICONQUESTION "$(AskDeleteData)" /SD IDNO IDNO done
  RMDir /r "${APP_DATA_DIR}"

  done:
SectionEnd
