"""Typed provider seams for the VoIP plugin (ADR-0004).

Every external, swappable component (streaming ASR/TTS, the prompt-injection
guard, the SIP/WebRTC media transport) sits behind a typed ``Protocol`` here;
the core depends on these contracts, never on a concrete vendor. Audio crossing
any boundary is linear PCM16 framed at a declared sample rate — codec (G.711)
and 8<->16 kHz resampling are the media layer's job, never a provider's.
"""
