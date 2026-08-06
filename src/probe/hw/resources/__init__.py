"""Hardware sources. Every resource follows the same two-method contract:
``probe()`` once for static inventory, ``sample(ts)`` per tick — and every
``create()`` is an availability probe returning None when its substrate is
absent, so registration is conditional and absent hardware costs nothing."""
