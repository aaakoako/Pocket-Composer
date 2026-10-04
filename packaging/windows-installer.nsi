Unicode True
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "StrFunc.nsh"
!include "FileFunc.nsh"
!include "WordFunc.nsh"
${StrStr}
!ifndef VERSION
  !error "VERSION is required"
!endif
!ifndef PAYLOAD
  !error "PAYLOAD is required"
!endif
!ifndef OUTPUT
  !error "OUTPUT is required"
!endif
!ifndef BUILD_ID
  !define BUILD_ID "${VERSION}"
!endif
!ifndef SOURCE_SHA
  !define SOURCE_SHA "0000000000000000000000000000000000000000"
!endif
Var AutoUpgrade
Var ForwardOnly
Var PreviousExe
Var LaunchExe
Var LaunchSource
Var UpgradeMutex
Var WaitPid
Var NewStarted
!ifdef TEST_INSTALL
  !define PRODUCT "DoubaoTypeless Installer Test"
  !define REGKEY "Software\DoubaoTypelessInstallerTest"
  !define SHORTCUT "DoubaoTypeless Installer Test"
!else
  !define PRODUCT "DoubaoTypeless"
  !define REGKEY "Software\DoubaoTypeless"
  !define SHORTCUT "Pocket Composer"
!endif
Name "${PRODUCT} ${VERSION}"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\${PRODUCT}"
InstallDirRegKey HKCU "${REGKEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${PRODUCT}"
VIAddVersionKey "FileDescription" "${PRODUCT} Installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "DoubaoTypeless contributors"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "安装完成，已创建桌面和开始菜单快捷方式。点击完成即可打开 Pocket Composer。设置、草稿和图片保存在应用目录之外，升级和卸载不会删除它们。"
!define MUI_FINISHPAGE_RUN "$LaunchExe"
!define MUI_FINISHPAGE_RUN_TEXT "打开 Pocket Composer"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
!ifdef TEST_INSTALL
  # Published legacy updaters may use Task Scheduler, which discards inherited
  # environment. Compile-only fixture paths keep that route isolated as well.
!ifdef TEST_INSTALL_ROOT
  StrCpy $INSTDIR "${TEST_INSTALL_ROOT}"
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_V3_DATA_DIR", w "${TEST_INSTALL_DATA}")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_V3_PIPE", w "${TEST_INSTALL_PIPE}")'
!endif
  ReadEnvStr $0 DT_UPGRADE_TEST_ROOT
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
!endif
  StrCpy $AutoUpgrade 0
  StrCpy $ForwardOnly 0
  StrCpy $WaitPid 0
  StrCpy $NewStarted 0
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/UPDATE" $1
  ${IfNot} ${Errors}
    StrCpy $AutoUpgrade 1
  ${EndIf}
  ${GetOptions} $0 "/WAITPID=" $WaitPid
  # 0.4.2 renames the selected release EXE to its own name before starting it.
  # Both published EXEs therefore support this native, non-Python entry path.
  ${StrStr} $1 $EXEFILE "_Setup.exe"
  ${If} $1 == ""
    StrCpy $AutoUpgrade 1
  ${EndIf}
  ${If} $AutoUpgrade == 1
    SetSilent silent
  ${EndIf}
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\${PRODUCT}-Upgrade") p.r0 ?e'
  Pop $1
  StrCpy $UpgradeMutex $0
  ${If} $1 == 183
    MessageBox MB_OK|MB_ICONINFORMATION "另一个升级正在进行，请稍后再打开。" /SD IDOK
    SetErrorLevel 3
    Quit
  ${EndIf}
  ReadEnvStr $PreviousExe DT_UPGRADE_PREVIOUS
  ${If} $WaitPid == ""
    StrCpy $PreviousExe ""
  ${EndIf}
  ${If} $PreviousExe == ""
    ReadRegStr $PreviousExe HKCU "${REGKEY}" "CurrentExecutable"
  ${EndIf}
  ${If} $PreviousExe == ""
    ReadRegStr $PreviousExe HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}" "DisplayIcon"
  ${EndIf}
  StrCpy $LaunchExe "$INSTDIR\versions\${BUILD_ID}\DoubaoTypeless.exe"
  StrCpy $LaunchSource "${SOURCE_SHA}"
  ${If} $AutoUpgrade == 1
  ${AndIf} $WaitPid == ""
    ReadRegStr $0 HKCU "${REGKEY}" "CurrentVersion"
    ${VersionCompare} $0 "${VERSION}" $1
    ${If} $1 != 2
      ReadRegStr $2 HKCU "${REGKEY}" "CurrentSource"
      StrLen $3 $2
      ${If} $3 == 40
        IfFileExists "$PreviousExe" 0 +4
        StrCpy $LaunchExe $PreviousExe
        StrCpy $LaunchSource $2
        StrCpy $ForwardOnly 1
      ${EndIf}
    ${EndIf}
  ${EndIf}
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "/oname=upgrade-launch.ps1" "upgrade-launch.ps1"
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_ROOT", w "$INSTDIR")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_WAIT_PID", w "$WaitPid")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_EXE", w "$LaunchExe")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_SOURCE", w "$LaunchSource")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_PREVIOUS", w "$PreviousExe")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_PACKAGE", w "$EXEPATH")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "PYINSTALLER_RESET_ENVIRONMENT", w "1")'
  ${If} $AutoUpgrade == 1
    nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\upgrade-launch.ps1" -Phase Wait'
    Pop $0
    Pop $1
    ${If} $0 == 4
      SetErrorLevel 4
      Quit
    ${EndIf}
    ${If} $0 != 0
!ifndef TEST_INSTALL
      SetSilent normal
!endif
      MessageBox MB_OK|MB_ICONSTOP "旧程序尚未正常退出，升级未执行。请退出旧程序后再运行这个升级包。" /SD IDOK
      SetErrorLevel 2
      Quit
    ${EndIf}
  ${EndIf}
FunctionEnd

Function LaunchAndVerify
  # Refresh InstallDir after the directory page has been used.
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_ROOT", w "$INSTDIR")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "DT_UPGRADE_EXE", w "$LaunchExe")'
  nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\upgrade-launch.ps1" -Phase Launch'
  Pop $0
  Pop $1
  ${If} $0 != 0
!ifndef TEST_INSTALL
    SetSilent normal
!endif
    MessageBox MB_OK|MB_ICONSTOP "新版没有成功启动，未切换安装入口。已有旧版时已尝试重新打开；旧数据仍保留。可重试此升级包，详情见安装目录 upgrade.log。" /SD IDOK
    SetErrorLevel 2
    Abort
  ${EndIf}
FunctionEnd

Section "Application"
  SetShellVarContext current
  ${If} $ForwardOnly == 1
    Call LaunchAndVerify
    Goto install_done
  ${EndIf}
  IfFileExists "$INSTDIR\*.*" 0 owned
  IfFileExists "$INSTDIR\.typeless-install" owned
  DetailPrint "安装目录包含其他文件，未覆盖。"
  Goto install_failed
  owned:
  ClearErrors
  CreateDirectory "$INSTDIR"
  IfErrors install_failed
  FileOpen $0 "$INSTDIR\.typeless-install" w
  IfErrors install_failed
  FileWrite $0 "DoubaoTypeless installation"
  FileClose $0
  IfErrors install_failed
  SetOutPath "$INSTDIR\versions\${BUILD_ID}"
  IfErrors install_failed
  File /r "${PAYLOAD}\*.*"
  IfErrors install_failed
  StrCpy $LaunchExe "$INSTDIR\versions\${BUILD_ID}\DoubaoTypeless.exe"
  ${If} $AutoUpgrade == 1
    Call LaunchAndVerify
    StrCpy $NewStarted 1
  ${EndIf}
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  IfErrors install_failed
  WriteRegStr HKCU "${REGKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${REGKEY}" "CurrentExecutable" "$LaunchExe"
  WriteRegStr HKCU "${REGKEY}" "CurrentSource" "${SOURCE_SHA}"
  WriteRegStr HKCU "${REGKEY}" "CurrentVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}" "DisplayName" "${PRODUCT}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}" "DisplayIcon" "$LaunchExe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}" "NoRepair" 1
  IfErrors install_failed
  SetOutPath "$INSTDIR\versions\${BUILD_ID}"
  CreateShortcut "$SMPROGRAMS\${SHORTCUT}.lnk" "$LaunchExe"
  IfErrors install_failed
  CreateShortcut "$DESKTOP\${SHORTCUT}.lnk" "$LaunchExe"
  IfErrors install_failed
!ifndef TEST_INSTALL
  Delete "$SMPROGRAMS\DoubaoTypeless.lnk"
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "DoubaoTypelessV3"
  ClearErrors
  ${If} $0 != ""
    ${StrStr} $1 $0 " --minimized"
    ${If} $1 != ""
      WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "DoubaoTypelessV3" '$\"$LaunchExe$\"$1'
    ${EndIf}
  ${EndIf}
!endif
  IfErrors install_failed
  ${If} $AutoUpgrade == 1
    nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\upgrade-launch.ps1" -Phase Retarget'
    Pop $0
    Pop $1
    ${If} $0 != 0
!ifndef TEST_INSTALL
      SetSilent normal
!endif
      MessageBox MB_OK|MB_ICONINFORMATION "新版已打开，但旧便携版入口未能替换。请从开始菜单打开 DoubaoTypeless；旧程序与数据仍保留。" /SD IDOK
    ${EndIf}
  ${EndIf}
  Goto install_done
  install_failed:
  ${If} $AutoUpgrade == 1
  ${AndIf} $NewStarted == 0
    nsExec::ExecToStack '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\upgrade-launch.ps1" -Phase Restore'
    Pop $0
    Pop $1
  ${EndIf}
!ifndef TEST_INSTALL
  SetSilent normal
!endif
  MessageBox MB_OK|MB_ICONSTOP "安装未完成，请检查磁盘空间和目录权限后重试。用户数据未删除。" /SD IDOK
  SetErrorLevel 2
  Abort
  install_done:
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  IfFileExists "$INSTDIR\.typeless-install" 0 refuse
  ReadRegStr $9 HKCU "${REGKEY}" "InstallDir"
  ClearErrors
  RMDir /r "$INSTDIR\versions"
  IfErrors locked
  Delete "$INSTDIR\.typeless-install"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ${If} $9 == "$INSTDIR"
    DeleteRegKey HKCU "${REGKEY}"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT}"
    Delete "$SMPROGRAMS\${SHORTCUT}.lnk"
    Delete "$DESKTOP\${SHORTCUT}.lnk"
!ifndef TEST_INSTALL
    Delete "$SMPROGRAMS\DoubaoTypeless.lnk"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "DoubaoTypelessV3"
!endif
  ${EndIf}
  Goto done
  locked:
  MessageBox MB_OK|MB_ICONSTOP "部分程序文件仍在使用。请退出 DoubaoTypeless 后重新卸载。卸载入口和用户数据已保留。" /SD IDOK
  SetErrorLevel 2
  Abort
  refuse:
  MessageBox MB_OK|MB_ICONSTOP "安装标记缺失，未删除任何文件。" /SD IDOK
  SetErrorLevel 2
  Abort
  done:
SectionEnd
