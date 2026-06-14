"""Pure SIP helpers (RFC 3261).

Standards-only and dependency-free: no transport, runtime, or architecture
decision lives here. This is a minimal real unit so the toolchain gate has a
typed, tested target from day one.
"""


def sip_address_of_record(extension: str, host: str) -> str:
    """Build a SIP Address-of-Record (AOR) URI for an extension on a gateway.

    Callers pass values sourced from the environment; never hard-code the real
    host or extension (the repo is public).

    Args:
        extension: The extension, e.g. from the ``HERMES_SIP_EXTENSION`` env var.
        host: The gateway FQDN, e.g. from ``HERMES_SIP_SERVER_HOST``.

    Returns:
        A ``sip:`` URI of the form ``sip:<extension>@<host>``.

    Raises:
        ValueError: If either argument is empty after trimming.
    """
    ext = extension.strip()
    pbx = host.strip()
    if not ext or not pbx:
        msg = "sip_address_of_record requires a non-empty extension and host"
        raise ValueError(msg)
    return f"sip:{ext}@{pbx}"
