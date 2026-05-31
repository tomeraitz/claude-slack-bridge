# PRD: Slack Phone Conversation

## Summary

Enable voice-to-voice conversations with Claude inside a Slack channel. Users send a Slack audio/voice message; Claude transcribes it, generates a reply, converts that reply to audio, and posts the audio file back to the channel — creating a phone-call-like experience without leaving Slack.

## Problem Statement

The current bridge only supports text. Users who want a more natural, hands-free interaction have no way to speak to Claude and hear a response within Slack.

## Goals

- Accept Slack voice/audio messages as input.
- Transcribe the audio to text (speech-to-text).
- Pass the transcript to Claude and get a text reply.
- Convert Claude's text reply to audio (text-to-speech).
- Post the audio file back to the same Slack channel — audio only, no text transcript.

## Non-Goals (for this iteration)

- Text-alongside-audio replies.
- Specific voice/accent preferences (use any clear default TTS voice).
- Multi-user concurrent support (deferred entirely — no queuing or "busy" behaviour required now).
- DM support (channel-only).

## User Flow

1. User presses the Slack mic button and sends a voice message in a bot-enabled channel.
2. The bridge receives the audio file event.
3. Audio is transcribed to text.
4. Transcript is sent to Claude; Claude returns a text response.
5. Text response is converted to an audio file.
6. Bot posts the audio file back to the channel thread.

## Scope

- **In scope:** Channels where the bot is already present.
- **Out of scope:** DMs, multi-user concurrency handling.

## Open Questions / Deferred Decisions

- Concurrency behaviour (what happens if a second user sends audio while the first is being processed) — deferred.
- Choice of STT provider (Whisper, Deepgram, etc.) — to be decided in design.
- Choice of TTS provider (OpenAI TTS, ElevenLabs, etc.) — to be decided in design.
- Audio format Slack accepts for playback — to be confirmed in design.
