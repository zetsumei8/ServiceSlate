!define PRODUCT_NAME "ServiceSlate"
!ifndef VERSION
  !define VERSION "0.9.0"
!endif
OutFile "..\release\ServiceSlate-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\ServiceSlate"
RequestExecutionLevel user
Name "${PRODUCT_NAME} ${VERSION}"
Icon "ServiceSlate.ico"
UninstallIcon "ServiceSlate.ico"
SetCompressor /SOLID lzma

Page directory
Page instfiles

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "..\dist\ServiceSlate\*.*"
  CreateDirectory "$LOCALAPPDATA\ServiceSlateData"
  CreateShortCut "$DESKTOP\ServiceSlate.lnk" "$INSTDIR\ServiceSlate.exe"
  CreateDirectory "$SMPROGRAMS\ServiceSlate"
  CreateShortCut "$SMPROGRAMS\ServiceSlate\ServiceSlate.lnk" "$INSTDIR\ServiceSlate.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\ServiceSlate.lnk"
  Delete "$SMPROGRAMS\ServiceSlate\ServiceSlate.lnk"
  RMDir "$SMPROGRAMS\ServiceSlate"
  RMDir /r "$INSTDIR"
  ; Company data intentionally remains under %LOCALAPPDATA%\ServiceSlateData.
SectionEnd
