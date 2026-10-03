# Install and use Pocket Composer

> The repository is now [Pocket-Composer](https://github.com/aaakoako/Pocket-Composer). Older in-app updaters are no longer maintained; download and install manually from the new repository. Existing data directories are unchanged.

**Current Windows stable: 0.5.7** ([release notes](0.5.7-live-sync.md)). This release improves how quickly the desktop follows continuous phone dictation. Download names keep the legacy `DoubaoTypeless_<version>_win_x64_Setup.exe` identifier for updater compatibility.

1. Download from [GitHub Releases](https://github.com/aaakoako/Pocket-Composer/releases). Use the stable release for normal use; preview releases are labeled separately.
2. Run the Windows per-user **Setup.exe**, or extract the **entire portable ZIP** before opening DoubaoTypeless.exe. Do not copy just the executable out of its folder.
3. Open the desktop client, put the phone on a trusted network reachable from the computer, and scan its QR code. Follow the desktop insertion permission controls.
4. Select a target input field on the computer, compose on the phone, and tap **Insert & copy**. Check the result before sending. Jev is optional and off by default.

The installer retains previous version directories and the workspace; uninstalling the program does not intentionally delete your drafts. Normal release data is under `%LOCALAPPDATA%\DoubaoTypeless\workspace-v3`, unless an explicit `--data-dir` is used. Preview shortcuts may deliberately use a separate directory. The portable archive uses the same default workspace unless launched with `--data-dir`.

Version 0.4.2 and older preview data are not silently merged into the new workspace. The framework upgrade replaces the program as a whole. Keep backups and use the upgrade/rollback instructions in the [detailed guide](v3-installation.md) if an update is interrupted. A new preview's startup test is not a fresh guarantee for every legacy updater path.

For a separate trial, launch with an explicit `--data-dir` and `--instance-name`, and exit the other instance to avoid competing hotkeys. Scan the QR code from the running version rather than reusing an old preview URL.

Compatibility depends on the target editor and input method. If insertion cannot be confirmed, the draft remains available for recovery or manual copy/paste. The normal insertion action does not press Enter.
