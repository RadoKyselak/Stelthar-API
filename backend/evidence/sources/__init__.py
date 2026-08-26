"""Source adapters.

`SourceUnavailable` exists because an outage and an absence are different
answers. An adapter that returns an empty list for both makes them
indistinguishable downstream, and the pipeline then reports a rate-limit error
as "no figure is published for that period" — a statement about the world,
made on the basis of our own failure.
"""


class SourceUnavailable(Exception):
    """The source could not be reached or refused the request."""

    def __init__(self, source: str, reason: str):
        self.source = source
        self.reason = reason
        super().__init__(f"{source} unavailable: {reason}")
