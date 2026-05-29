"""Class-based handler — not currently supported by the inference engine.

The publish_inference engine looks for a top-level function named `handler`
(or the only public function). A callable class is intentionally outside
that contract; the spec must surface `missing` so the caller asks for
the fields manually.
"""


class Handler:
    def __call__(self, payload: dict) -> dict:
        return {"result": payload}
