"""The plugin's real-time media layer (ADR-0005).

Owns everything below the PCM16 provider boundary (ADR-0004): the G.711 wire
codec, 8<->16 kHz resampling, RTP/SRTP, jitter buffering, and DTMF. Providers
above this line only ever see ``PcmFrame`` at a declared rate.
"""
