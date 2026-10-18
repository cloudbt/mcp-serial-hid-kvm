"""Action observation options, shared with the standalone tool schemas."""

from jsonschema import Draft202012Validator


ACTION_TOOLS = {"mouse_scroll", "mouse_click", "send_key", "type_text",
                "type_japanese", "type_chinese"}


def after_schema(capture, stable):
    return {
        "type": "object", "additionalProperties": False,
        "description": "After sending input: optionally park in HID coordinates, wait, return an image and stage timings. No input retries. Defaults: no parking, stable wait, full screenshot. Caller verifies UI success.",
        "properties": {
            "park_cursor": {
                "type": "object", "additionalProperties": False,
                "required": ["x", "y"],
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                },
                "description": "Absolute HID screen coordinates, not returned-image pixels. Omit to keep the cursor.",
            },
            "wait": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "mode": {"enum": ["none", "delay", "stable"], "default": "stable"},
                    "seconds": {"type": "number", "minimum": 0, "maximum": 600},
                    **stable["properties"],
                },
                "description": "none: no wait; delay: requires seconds; stable: existing wait_for_stable options/defaults. Waits capped by max_wait_seconds.",
            },
            "screenshot": {**capture, "additionalProperties": False},
        },
    }


def validate_options(value, schema):
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error:
        path = ".".join(str(p) for p in error.path)
        raise ValueError(f"after{'.' + path if path else ''}: {error.message}")
    wait = value.get("wait", {})
    mode = wait.get("mode", "stable")
    if mode == "delay" and "seconds" not in wait:
        raise ValueError("after.wait.seconds is required for delay")
    if mode == "none":
        permitted = {"mode"}
    elif mode == "delay":
        permitted = {"mode", "seconds"}
    else:
        permitted = set(schema["properties"]["wait"]["properties"]) - {"seconds"}
    if set(wait) - permitted:
        raise ValueError(f"after.wait: options do not apply to mode {mode}")
