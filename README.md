<p align="center"><img src="docs/banner.png" alt="GOBL(in) Voice: voice control and dictation for AI coding agents on macOS" width="100%"></p>

# GOBL(in) Voice — voice control and dictation for Claude Code, Codex and Orca on macOS

![macOS 14+](https://img.shields.io/badge/macOS-14+-000000?logo=apple&logoColor=white) ![Apple Silicon M1+](https://img.shields.io/badge/Apple%20Silicon-M1+-555555) ![speech Whisper](https://img.shields.io/badge/speech-Whisper-74aa9c) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![GitHub stars](https://img.shields.io/github/stars/goblin-red/Orca-Voice?style=social)](https://github.com/goblin-red/Orca-Voice/stargazers)

Dictate prompts into Claude Code, Codex, OpenCode, and other terminal agents.
Send prompts with voice commands, switch tabs, and hear replies read aloud. Use
local Whisper recognition and macOS voices, or configure your own provider.

## Install

Use an Apple Silicon Mac with macOS 14 or later and an internet connection.
To install the latest version, paste this into Terminal.

```sh
curl -fsSL https://raw.githubusercontent.com/goblin-red/Orca-Voice/main/install.sh | bash
```

If you already downloaded this repository, run its installer from this folder.

```sh
bash utility/scripts/install.sh
```

The installer puts the program and its private Python environment in
`~/.goblin-voice` and downloads the Whisper server and model. It adds the
launcher to `/Applications` or, if that folder is read-only, to
`~/Applications`. Open GOBL(in) Voice there or through Spotlight. Grant
microphone access when macOS asks. Enable Accessibility if you want Voice to
type into other apps or send keyboard shortcuts.

Use this command to install without opening the app.

```sh
GOBLIN_VOICE_NO_LAUNCH=1 bash utility/scripts/install.sh
```

`GOBLIN_VOICE_DIR` and `GOBLIN_VOICE_APPS` override the default directories.

## Use Voice

Open the settings window to choose your microphone, dictation engine, and
speaking voice. Select Whisper for local dictation or System for macOS speech.
For a cloud engine, enter your own provider key or sign in to the supported
subscription, depending on the engine. The terminal integration is optional.

The default command `rex` sends Enter, `switch terminal` moves to the next
terminal, and `shut up` stops speech. Change the command words in settings.

## Source

- `utility/core` contains the Python service and voice processing.
- `utility/ui` contains the settings and floating badge.
- `utility/scripts` contains the installer and startup scripts.
- `utility/tools` and `utility/transcription` contain optional command-line tools.
- `utility/GOBL(in) Voice.app` contains the native launcher used by the installer.
  Use the installed copy to launch Voice.

Settings, recordings, and logs are created locally after installation.
The default settings in the repository have empty provider credentials.

## More GOBL(in) apps

Free and open source, from the makers of [GOBL(in)](https://goblin.red):

| App | What it does |
| --- | --- |
| [GOBL(in) Remote](https://github.com/goblin-red/Goblin-Remote) | self-hosted remote desktop for macOS in any browser, over cheap PHP hosting |
| [GOBL(in) Session Viewer](https://github.com/goblin-red/Session-Viewer) | every Claude Code, Codex, Grok and OpenCode session in one window |
| [GOBL(in) Drag & Taskbar](https://github.com/goblin-red/Drag-and-Taskbar) | move windows with trackpad gestures, a real taskbar and Alt-Tab for macOS |
| [GOBL(in) Convert](https://github.com/goblin-red/Photo-Convert) | fast batch JPEG converter and photo resizer for macOS |
| [GOBL(in) Workflow](https://github.com/goblin-red/Workflows-Canvas) | visual flowchart workflows run by AI coding agents |

Distributed under the [MIT License](LICENSE).
