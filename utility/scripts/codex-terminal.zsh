# Voice: keep local interactive Codex tools under their own terminal's process tree.
# Loaded by ~/.zshrc. The installed Codex executable and updater stay untouched.
function codex() {
  local voice_arg
  for voice_arg in "$@"; do
    case "$voice_arg" in
      --remote|--remote=*)
        "${GOBLIN_VOICE_DIR:-$HOME/.goblin-voice}/scripts/codex-voice-remote" "$@"
        return $?
        ;;
      app-server|remote-control|exec|e|review|login|logout|mcp|mcp-server|app|completion|update|features|debug|sandbox|apply)
        command codex "$@"
        return $?
        ;;
    esac
  done
  command codex --no-daemon "$@"
}
