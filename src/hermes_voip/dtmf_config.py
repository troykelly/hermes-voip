"""DTMF send + receive mode resolution — makes the DTMF config keys drive behaviour.

The ``HERMES_SIP_DTMF_*`` keys (parsed into :class:`hermes_voip.config.MediaConfig`)
select the DTMF backend per call. This module is the reconciliation that turns the
config + the per-call negotiation into a concrete backend (ADR-0010/0034):

* :func:`resolve_dtmf_receive_mode` -> :class:`DtmfReceiveMode` (which inbound-DTMF
  backend to wire), and
* :func:`resolve_dtmf_send_mode` -> :class:`DtmfSendMode` (which outbound backend
  ``send_dtmf`` uses).

Both take the call's negotiated telephone-event payload type and audio codec. ``auto``
prefers RFC 4733 when telephone-event was negotiated, else falls to the in-band last
resort on a G.711 call (in-band is trusted ONLY on clean G.711 — ADR-0005). A forced
``sip_info`` always resolves to SIP INFO (in-dialog signalling is always available); a
forced ``inband`` resolves to in-band on G.711 else UNAVAILABLE; a forced ``rfc4733``
needs a negotiated telephone-event or it is UNAVAILABLE. All four ADR-0010 modes are now
implemented (ADR-0036), so the config no longer rejects any of them at load.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from hermes_voip.dtmf import DtmfSendMode

if TYPE_CHECKING:
    from hermes_voip.config import MediaConfig

__all__ = [
    "DtmfReceiveMode",
    "DtmfSendMode",
    "is_g711_codec",
    "resolve_dtmf_receive_mode",
    "resolve_dtmf_send_mode",
]

# In-band is trusted only on clean G.711 (ADR-0005/0034): a lossy/wideband codec
# distorts the dual-tone waveform. The negotiated audio codec is identified by its
# encoding name; G.711 is PCMU (mu-law) / PCMA (a-law).
_G711_ENCODINGS = frozenset({"pcmu", "pcma"})


def is_g711_codec(codec: str) -> bool:
    """Whether ``codec`` (an RTP encoding name) is G.711 (in-band is trusted here)."""
    return codec.lower() in _G711_ENCODINGS


class DtmfReceiveMode(enum.Enum):
    """The resolved inbound-DTMF behaviour for one call (ADR-0010/0034).

    * ``RFC4733`` — decode inbound telephone-event RTP at the negotiated payload type.
    * ``SIP_INFO`` — surface digits from inbound in-dialog ``INFO`` requests.
    * ``INBAND`` — run the Goertzel detector on the inbound G.711 audio.
    * ``DISABLED`` — DTMF receive is intentionally off for this call (no backend could
      run and the operator did not ask for a fallback): a clean no-DTMF call.
    * ``UNAVAILABLE`` — DTMF receive was WANTED but cannot run (the mode demands a
      backend the negotiation does not support — e.g. ``rfc4733`` with no
      telephone-event, or in-band on a non-G.711 codec). The adapter logs this at
      WARNING so the gap is operator-visible — distinct from a clean ``DISABLED``.
    """

    RFC4733 = "rfc4733"
    SIP_INFO = "sip_info"
    INBAND = "inband"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


def resolve_dtmf_receive_mode(
    config: MediaConfig,
    *,
    telephone_event_payload_type: int | None,
    codec: str,
) -> DtmfReceiveMode:
    """Resolve the inbound-DTMF backend for one call from config + negotiation.

    The decision (``config.dtmf_mode`` is one of ``auto`` | ``rfc4733`` | ``sip_info``
    | ``inband``):

    * ``sip_info`` (forced) ⇒ :attr:`DtmfReceiveMode.SIP_INFO` (always available — it
      is in-dialog signalling, not media);
    * ``inband`` (forced) ⇒ :attr:`DtmfReceiveMode.INBAND` on a G.711 call, else
      :attr:`DtmfReceiveMode.UNAVAILABLE`;
    * a negotiated telephone-event PT (``auto`` / ``rfc4733``) ⇒
      :attr:`DtmfReceiveMode.RFC4733`;
    * ``rfc4733`` forced with no PT ⇒ :attr:`DtmfReceiveMode.UNAVAILABLE`;
    * ``auto`` with no PT: the in-band last resort decides — permitted
      (``dtmf_inband_enabled``) and G.711 ⇒ :attr:`DtmfReceiveMode.INBAND`; permitted
      but non-G.711 ⇒ :attr:`DtmfReceiveMode.UNAVAILABLE` (loud: wanted, can't run);
      forbidden ⇒ :attr:`DtmfReceiveMode.DISABLED` (clean no-DTMF call).

    Args:
        config: The call's media config (``dtmf_mode`` / ``dtmf_inband_enabled``).
        telephone_event_payload_type: The negotiated RFC 4733 payload type, or ``None``.
        codec: The negotiated audio codec's encoding name (e.g. ``"PCMU"``).

    Returns:
        The :class:`DtmfReceiveMode` the adapter acts on for this call.
    """
    mode = config.dtmf_mode
    if mode == "sip_info":
        return DtmfReceiveMode.SIP_INFO
    if mode == "inband":
        return (
            DtmfReceiveMode.INBAND
            if is_g711_codec(codec)
            else DtmfReceiveMode.UNAVAILABLE
        )
    if telephone_event_payload_type is not None:
        return DtmfReceiveMode.RFC4733  # auto / rfc4733 both use it
    if mode == "rfc4733":
        return DtmfReceiveMode.UNAVAILABLE  # forced RFC 4733 but no telephone-event
    # mode == "auto", no telephone-event: the in-band last resort policy decides.
    if not config.dtmf_inband_enabled:
        return DtmfReceiveMode.DISABLED  # operator forbade the fallback: clean no-DTMF
    return (
        DtmfReceiveMode.INBAND if is_g711_codec(codec) else DtmfReceiveMode.UNAVAILABLE
    )


def resolve_dtmf_send_mode(
    config: MediaConfig,
    *,
    telephone_event_payload_type: int | None,
    codec: str,
) -> DtmfSendMode:
    """Resolve the outbound-DTMF backend ``send_dtmf`` uses for one call.

    Mirrors :func:`resolve_dtmf_receive_mode` for the send direction. Unlike receive
    there is no ``DISABLED`` (sending is an explicit agent action, never a no-op): a
    call with no usable send backend resolves to :attr:`DtmfSendMode.UNAVAILABLE`, and
    the send path raises rather than silently dropping (rule 6/37).

    * ``sip_info`` (forced) ⇒ :attr:`DtmfSendMode.SIP_INFO`;
    * ``inband`` (forced) ⇒ :attr:`DtmfSendMode.INBAND` on G.711 else
      :attr:`DtmfSendMode.UNAVAILABLE`;
    * a negotiated telephone-event PT (``auto`` / ``rfc4733``) ⇒
      :attr:`DtmfSendMode.RFC4733`;
    * ``rfc4733`` forced with no PT ⇒ :attr:`DtmfSendMode.UNAVAILABLE`;
    * ``auto`` with no PT ⇒ :attr:`DtmfSendMode.INBAND` on G.711 (the last resort that
      always works on a G.711 wire) else :attr:`DtmfSendMode.UNAVAILABLE`.

    Args:
        config: The call's media config (``dtmf_mode``).
        telephone_event_payload_type: The negotiated RFC 4733 payload type, or ``None``.
        codec: The negotiated audio codec's encoding name (e.g. ``"PCMU"``).

    Returns:
        The :class:`DtmfSendMode` the send path uses for this call.
    """
    mode = config.dtmf_mode
    if mode == "sip_info":
        return DtmfSendMode.SIP_INFO
    if mode == "inband":
        return DtmfSendMode.INBAND if is_g711_codec(codec) else DtmfSendMode.UNAVAILABLE
    if telephone_event_payload_type is not None:
        return DtmfSendMode.RFC4733  # auto / rfc4733 both use it
    if mode == "rfc4733":
        return DtmfSendMode.UNAVAILABLE  # forced RFC 4733 but no telephone-event
    # mode == "auto", no telephone-event: fall to in-band on a G.711 wire.
    return DtmfSendMode.INBAND if is_g711_codec(codec) else DtmfSendMode.UNAVAILABLE
