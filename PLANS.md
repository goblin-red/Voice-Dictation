# Early design notes

These are design notes from the first public release.

## Voice input from a phone

A browser could send text through an HTTPS relay to Voice on the Mac. Voice
would open the outbound WebSocket. The phone could use keyboard dictation,
select an agent or terminal tab, send keys, see agent state, and read or hear
replies.

The original design called for password or QR pairing, per-device revocation,
an explicit remote access switch, reconnects, and delivery receipts. It also
considered a Telegram bot, a private network such as Tailscale, and the
existing Orca pairing features.

## Voice and terminal controls

The original roadmap included microphone echo cancellation, filtering quiet
and noisy audio before Whisper, identifying the correct Codex session when
several share a project folder, spoken terminal selection, and launching
Voice at login.

## Intel Macs

The installer supports Apple Silicon. An Intel whisper.cpp build ran under
Rosetta with Metal disabled. It used x86_64 with SSE4.2, AVX, AVX2, FMA,
F16C, and BMI2 enabled. The smaller `ggml-base-q5_1.bin` model was faster
in those checks.

Some pinned packages for the OpenAI realtime mode lacked ready-made Intel
builds, so a possible Intel distribution would need a separate dependency
set or omit that mode. The window and microphone still need verification
on Intel hardware.
