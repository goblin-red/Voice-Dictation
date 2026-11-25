# Changelog

## Unreleased

- Moved the app and source files into `utility/`. Existing install links still work.
- Added the floating badge, voice and media controls, speech engine options, and terminal integrations.
- English is the interface language.

## 2026-10-02 · v1.0

- First public release of GOBL(in) Voice under the MIT License.
- Renamed the app from Orca Voice and added the Goblin logo. The previous repository address continued to work.
- Released English and Russian interface translations.
- Added a voice command to switch to the next terminal tab and default English command words.
- Added the Apple Silicon installer for macOS 14 and later. It sets up private Python, a pinned whisper.cpp 1.9.4 build, and the Whisper model.
- Moved default settings into their own file and excluded personal settings from the repository.
- Improved shutdown and avoided repeating the same Orca polling error in the log.
