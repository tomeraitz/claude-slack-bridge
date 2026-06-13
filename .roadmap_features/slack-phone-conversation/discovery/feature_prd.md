# PRD: Slack Phone Conversation

## Summary

Enable live, real-time voice conversations with Claude inside a Slack channel. Users initiate a phone-call-like session directly from Slack; both the user's voice and Claude's spoken responses are streamed in real time — creating a continuous, two-way voice call experience without leaving Slack.

## Problem Statement

The current bridge only supports text. Users who want a more natural, hands-free interaction have no way to hold a live spoken conversation with Claude within Slack. A round-trip of recorded audio clips (voice memo → reply clip) is too slow and disjointed to feel like a real conversation.

## Goals

- Allow a user to start a live voice call session from a Slack channel or DM.
- Stream the user's speech to Claude in real time (continuous speech-to-text).
- Stream Claude's spoken reply back to the user in real time (text-to-speech with low latency).
- Maintain conversational context for the full duration of the live call.
- Allow the user to end the call from Slack and receive a brief text summary in the channel.

## Non-Goals (for this iteration)

- Asynchronous voice-memo / audio-clip exchange (that is the old model; this feature replaces it).
- Text-alongside-audio replies during the call.
- Specific voice/accent preferences (use any clear default TTS voice).
- Multi-user concurrent calls (deferred — no queuing or "busy" behaviour required now).
- Video or screen-share support.

## User Flow

1. User types `/call` (or clicks a "Start Voice Call" shortcut) in a bot-enabled channel.
2. The bridge opens a live audio session and signals to the user that the call is active.
3. The user speaks; audio is streamed in real time to a speech-to-text service.
4. Transcribed text is forwarded to Claude; Claude's reply is streamed back.
5. Claude's reply is converted to speech and played to the user with minimal latency, creating a natural back-and-forth.
6. Steps 3–5 repeat continuously for the duration of the call.
7. User says "end call" or types `/end-call`; the session closes and the bot posts a brief text summary of the conversation to the channel thread.

## Scope

- **In scope:** Channels and DMs where the bot is already present; live bidirectional audio streaming; session summary posted on call end.
- **Out of scope:** Multi-user concurrency handling, asynchronous voice-clip exchange, DM-only vs. channel distinction (both are supported).

## Open Questions / Deferred Decisions

- WebRTC vs. telephony (e.g. Twilio) vs. WebSocket-based audio streaming — to be decided in design.
- Concurrency behaviour (what happens if a second user starts a call while one is in progress) — deferred.
- Choice of real-time STT provider (Deepgram streaming, Whisper live, etc.) — to be decided in design.
- Choice of low-latency TTS provider (OpenAI TTS streaming, ElevenLabs, etc.) — to be decided in design.
- How Slack surfaces the live audio stream to the user (Slack call API, external link, embedded widget) — to be confirmed in design.
