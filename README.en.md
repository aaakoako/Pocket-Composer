<div align="center">

# Pocket Composer
### Speak it. Sketch it. Bring it to your computer.
**A phone-to-desktop text and image companion · Windows stable / macOS and Linux previews**

[简体中文](README.md) · [Stable download](https://github.com/aaakoako/Pocket-Composer/releases/latest) · [Installation](docs/release/INSTALL.en.md) · [Feedback](https://github.com/aaakoako/Pocket-Composer/issues)

![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-5b5ce2) [![MIT](https://img.shields.io/badge/License-MIT-6254e8)](LICENSE)

</div>

Explaining a UI problem, describing a coding task or sketching an idea can be easier on your phone. Pocket Composer combines your phone keyboard, screenshot annotation and a whiteboard, then inserts prepared images and text into a Windows input field.

**Windows stable: 0.5.6. macOS/Linux remain previews.** [Release notes](docs/release/0.5.6-reconnect.md)

<table><tr><th>Express on your phone</th><th>Continue on your computer</th></tr><tr><td align="center"><img src="docs/images/product/phone.png" width="270" alt="0.5.5 preview phone composer and sync status" /></td><td align="center"><img src="docs/images/product/hud.png" width="430" alt="0.5.5 preview input-first desktop overlay" /><br/><br/>Read, edit, copy and insert.<br/>Expand assistance only when wanted.</td></tr></table>

## Speak it. Sketch it.

| What you need | Where to go |
|---|---|
| Dictate a request | Your phone keyboard's voice input; edit normally |
| Point out a desktop issue | **Capture desktop** → annotate, number and caption |
| Add reference images | **Photos** → edit, reorder and describe |
| Explain a layout | **Whiteboard** → drawing, shapes and text |
| Move it to a desktop field | **Insert & copy** → images first, then text; no automatic send |
| Start again or recover content | Clear/undo the current draft; restore recent bundles |

<div align="center"><img src="docs/images/product/phone-workflow.gif" width="300" alt="Actual phone page recording: typing, whiteboard and sync" /></div>

*Actual UI with demo content. This demonstrates editing and synchronization, not receipt by a target composer. Dictation is provided by your phone keyboard.*

## Get started

1. **Open the Windows client**, connect both devices to a trusted, mutually reachable network and scan the pairing QR code.
2. **Allow insertion and select the desktop input field** you want to use.
3. **Add text/images and tap Insert & copy.** Review the result on your computer, then send it yourself.

No model service is required. A failed insertion keeps the draft available for copying, manual pasting or recovery. The separate phone-send feature is off by default and requires desktop authorization and explicit phone confirmation.

## Optional Jev references

**Off by default; text, images, copying and insertion work without it.** Enable it for possible transcription issues, four independent reference dimensions and wording tone. There is no overall score, success-rate prediction or mandatory clarification. Labeled segmented bars sit beside the draft; disabling Jev removes the sidebar. Full details open on request.

<div align="center"><img src="docs/images/product/input-feedback.gif" width="430" alt="Optional tone icons, segmented reference bars and a flame effect" /></div>

*Actual Qt UI with synthetic reference/tone examples. The flame is a visual easter egg, not a judgment of a person's mental state.*

Jev supports TypeSafe, OpenRouter, Vercel and compatible custom endpoints. Rewriting uses a separately configured model. Bring your own key and credits; provider charges apply. References use the current text only, **not the target agent's history or results**. App animations have their own switch.

## Downloads, data and compatibility

| Desktop | Download and status |
|---|---|
| Windows 10 / 11 | [Stable](https://github.com/aaakoako/Pocket-Composer/releases/latest) / [0.5.5 preview](https://github.com/aaakoako/Pocket-Composer/releases/tag/preview-0.5.5) |
| macOS · Apple Silicon / Intel | [DMG previews](https://github.com/aaakoako/Pocket-Composer/releases/tag/preview-0.5.5-posix); Accessibility permission required; not Apple notarized |
| Linux x64 · X11 | [DEB / tar.gz previews](https://github.com/aaakoako/Pocket-Composer/releases/tag/preview-0.5.5-posix); Ubuntu 22.04+; no automatic insertion on Wayland yet |

[macOS / Linux setup, permissions and limitations](docs/release/CROSS_PLATFORM.md). Cross-platform packages do not imply real Codex/Cursor or physical-phone acceptance.

- [Releases](https://github.com/aaakoako/Pocket-Composer/releases) include a per-user installer and a complete portable archive. Extract the entire portable folder. The repository is now Pocket-Composer. Older in-app updaters are no longer maintained; download manually from the new repository. Installer filenames and existing data paths retain the legacy DoubaoTypeless identifier to preserve existing drafts.
- The app UI is currently primarily Simplified Chinese. A browser on your phone and Windows 10/11 on desktop. Compatibility depends on your browser, input method and target application.
- A user has confirmed sequential image/text insertion in Codex desktop. **Automatic detection is not guaranteed for every composer.** Cursor, multi-monitor setups and physical mobile keyboards need further validation.
- Drafts, images and settings are stored locally. Offline writing and reconnection reconciliation are supported. Upgrades retain the workspace; uninstalling does not intentionally remove drafts.
- Use the bridge on trusted networks. Enabling model features sends relevant text to your selected provider.

[Installation, upgrades and data](docs/release/INSTALL.en.md) · [Privacy](docs/PRIVACY.md)

## Open source and commercial use

[MIT](LICENSE) permits commercial use, modification and redistribution with the required copyright and permission notices. Dependencies retain their own licenses; model services have separate terms and charges.

[Commercial use](docs/legal/COMMERCIAL_USE.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

Pocket Composer is independent and does not claim affiliation with Doubao, ByteDance, OpenAI, Cursor or any model provider.

## Contribute

Include your versions, browser/input method, steps and sanitized screenshots in [issues](https://github.com/aaakoako/Pocket-Composer/issues). Do not include API keys, pairing credentials or private drafts.

[Build and verify](docs/release/v3-build.md) · [Changelog](CHANGELOG.md)

