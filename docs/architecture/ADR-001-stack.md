# ADR-001: Runtime stack for the first milestones

Date: 2026-10-20 · Status: accepted

## Context
Target is Omarchy 3.8.3 (Arch, Hyprland 0.56.2, Waybar 0.15, PipeWire) on an i5-1235U laptop, 1920×1080 @1.5.
Available: Python 3.14 + uv, Node 26, system Chromium 154, GNOME Keyring (secret-tool), grim, wtype, wpctl.
Not installed: Quickshell, Electron, Tauri toolchain deps. No existing JARVIS code (the earlier assistant
described in `~/omarchy-nightly-report.md` had been removed). No Gemini credentials present.

## Decision
- **Core:** one Python process (modular monolith), aiohttp on 127.0.0.1 only. SQLite (WAL) with ordered migrations.
- **UI:** no-build web UI shown in a Chromium `--app` window with its own profile; launched with Hyprland
  one-shot exec rules (float/size/center), so the user's Hyprland config is not modified.
- **Top bar:** Waybar custom module (`jarvis waybar`, SSE) — Waybar is what Omarchy 3.8 ships. Quickshell is not
  installed; a QML companion is deferred until it is part of the target system.
- **AI:** official `google-genai` SDK; manual function calling so the policy gateway sees every tool call.
  Model IDs are configuration, checked against `models.list` at runtime (docs on 2026-10-20 list
  gemini-3.8-flash, gemini-3.5-flash-lite, gemini-4-argon, gemini-3.8-flash-tts).
- **Secrets:** GNOME Keyring via `secret-tool`; env var accepted for development.
- **Spreadsheets:** openpyxl, with our own evaluation of the formula patterns we support; others are flagged.

## Alternatives considered
- Electron/Tauri: extra runtime or build chain for little gain over Chromium already on the system.
- Reusing hermes-agent/openclaw/OpenJarvis as the orchestrator: large surface, overlapping responsibilities;
  the spec asks for one primary approach. Patterns (tool registry, approvals) are implemented directly.

## Consequences
- Window app id is `chrome-localhost__-Default` (Chromium ignores `--class` on Wayland); detection also checks the title.
- Chromium window costs ~380 MB PSS; idle CPU ~1.9% of one core after removing idle animations.
- Voice capture uses the browser mic (push-to-talk) and Gemini transcription; local wake word is a later milestone.
