"""DTMF receive-mode resolution — makes the DTMF config keys drive behaviour (ADR-0010).

The ``HERMES_SIP_DTMF_*`` keys (parsed into :class:`hermes_voip.config.MediaConfig`)
were inert: the config advertised three mechanisms but no code consumed the keys (a
rule-27 drift — the config promised behaviour the code lacked). This module is the
reconciliation:

* The validation in :class:`MediaConfig` now **rejects** ``sip_info`` and ``inband``
  at load (those receive backends are not implemented), so no mode value silently does
  nothing — an operator who sets an unsupported mode gets a loud
  :class:`~hermes_voip.config.ConfigError`.
* :func:`resolve_dtmf_receive_mode` maps the surviving config (``auto`` | ``rfc4733``,
  the ``dtmf_inband_enabled`` policy flag) plus the per-call negotiated telephone-event
  payload type to a concrete :class:`DtmfReceiveMode`. The adapter calls it once per
  call to decide whether to wire the RFC 4733 receiver, and logs the result — so every
  surviving key changes a real, observable outcome.

RFC 4733 is the shipped receive path (this lane). SIP INFO and in-band remain DEFERRED
(ADR-0010 alternatives) — see the ADR for the receive/transfer status.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_voip.config import MediaConfig

__all__ = ["DtmfReceiveMode", "resolve_dtmf_receive_mode"]


class DtmfReceiveMode(enum.Enum):
    """The resolved inbound-DTMF behaviour for one call.

    * ``RFC4733`` — decode inbound telephone-event RTP at the negotiated payload type
      (the shipped path). The engine wires a ``DtmfReceiver`` for the call.
    * ``DISABLED`` — DTMF receive is intentionally off for this call: no
      telephone-event PT was negotiated and the operator did not ask for a fallback
      (``dtmf_inband_enabled`` false). A clean, expected no-DTMF call.
    * ``UNAVAILABLE`` — DTMF receive was WANTED but cannot run: the mode demands it
      (``rfc4733``) or the operator permitted the in-band fallback (``auto`` +
      ``dtmf_inband_enabled``), yet no telephone-event PT was negotiated and the
      in-band detector is not implemented. The adapter logs this at WARNING so the gap
      is operator-visible — distinct from a clean ``DISABLED``.
    """

    RFC4733 = "rfc4733"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


def resolve_dtmf_receive_mode(
    config: MediaConfig, *, telephone_event_payload_type: int | None
) -> DtmfReceiveMode:
    """Resolve the inbound-DTMF behaviour for one call from config + negotiation.

    ``config.dtmf_mode`` is one of ``auto`` | ``rfc4733`` here — ``sip_info`` and
    ``inband`` are rejected at config load, so they never reach this function. The
    decision:

    * a negotiated telephone-event PT ⇒ :attr:`DtmfReceiveMode.RFC4733` (both
      ``auto`` and the forced ``rfc4733`` use it);
    * no PT, mode ``rfc4733`` (forced) ⇒ :attr:`DtmfReceiveMode.UNAVAILABLE`
      (the operator demanded RFC 4733 but the peer offered no telephone-event);
    * no PT, mode ``auto``: the ``dtmf_inband_enabled`` flag decides — ``True`` (the
      default; the in-band last resort is permitted but not implemented) ⇒
      :attr:`DtmfReceiveMode.UNAVAILABLE`; ``False`` (in-band forbidden) ⇒
      :attr:`DtmfReceiveMode.DISABLED` (a clean no-DTMF call).

    Args:
        config: The call's media config (its ``dtmf_mode`` / ``dtmf_inband_enabled``).
        telephone_event_payload_type: The negotiated RFC 4733 telephone-event RTP
            payload type for this call, or ``None`` when the peer offered none.

    Returns:
        The :class:`DtmfReceiveMode` the adapter acts on for this call.
    """
    if telephone_event_payload_type is not None:
        return DtmfReceiveMode.RFC4733
    # No telephone-event was negotiated. Whether that is a clean DISABLED or a loud
    # UNAVAILABLE depends on whether DTMF receive was WANTED on this call.
    if config.dtmf_mode == "rfc4733":
        # The operator forced RFC 4733 but the peer offered no telephone-event.
        return DtmfReceiveMode.UNAVAILABLE
    # mode == "auto": the in-band flag is the operator's fallback policy. in-band is
    # not implemented, so a permitted-but-unbuildable fallback is UNAVAILABLE (loud);
    # an explicitly-forbidden fallback with no PT is a clean DISABLED.
    if config.dtmf_inband_enabled:
        return DtmfReceiveMode.UNAVAILABLE
    return DtmfReceiveMode.DISABLED
