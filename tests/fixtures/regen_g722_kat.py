"""Regenerate ``tests/g722_kat_vectors.py`` from the public-domain G.722 reference.

This is a developer tool, NOT part of the test suite (it is never imported by a
test and requires a C toolchain + network to fetch the reference). It documents
the exact, reproducible procedure that produced the committed known-answer
vectors so a future session can verify or regenerate them.

Provenance / licence
--------------------
The expected G.722 bytes and decoded samples in ``tests/g722_kat_vectors.py``
are produced by the **public-domain** ITU G.722 reference codec — Steve
Underwood's public-domain dedication plus the CMU-1993 "completely unrestricted"
notice, as packaged permissively (Public-Domain + BSD-2) by the
``sippy/libg722`` project. They are deliberately NOT the ITU-T STL conformance
vectors, which ship under the copyleft "ITU-T General Public License" and must
not be vendored into this permissive, public repository.

The input is a deterministic synthetic 16 kHz signal defined in code
(``tests/g722_kat_vectors.py::_build_input``): a 1 kHz + 3 kHz tone mix (which
exercises both G.722 sub-bands) with a leading full-scale impulse. The reference
is run in the standard **64 kbit/s mode 1** (8 bits/sample, unpacked,
non-test-mode) — exactly RFC 3551 RTP G.722.

Procedure
---------
1. Fetch the public-domain reference C from ``github.com/sippy/libg722`` (files
   ``g722_encode.c``, ``g722_decode.c``, ``g722_common.h``, ``g722_private.h``,
   ``g722_encoder.h``, ``g722_decoder.h``, ``g722.h``, ``g722_codec.h``).
2. Build a tiny oracle that reads PCM16-LE on stdin, encodes at 64000/mode-1,
   decodes the result, and writes the G.722 bytes + decoded PCM16::

       gcc -O2 -o oracle oracle.c g722_encode.c g722_decode.c

   where ``oracle.c`` calls ``g722_encoder_new(64000, 0)`` / ``g722_encode`` then
   ``g722_decoder_new(64000, 0)`` / ``g722_decode``.
3. Generate the input via ``_build_input`` (this module mirrors it), pipe it
   through the oracle, and capture the G.722 bytes (hex) + decoded samples.
4. Emit ``tests/g722_kat_vectors.py`` with ``kat_g722_bytes`` and
   ``kat_decoded_samples`` set to the captured values.

The pure-Python codec ``hermes_voip.media.g722`` is then asserted **bit-exact**
against these vectors in ``tests/test_g722_codec.py`` (encode → identical bytes,
decode → identical samples), which is what proves the port matches the reference
without vendoring any copyleft code.
"""

from __future__ import annotations
