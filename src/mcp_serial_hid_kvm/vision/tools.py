"""MCP schemas; algorithms remain in state.py without MCP dependencies."""

from mcp.types import Tool


def state_tools():
    common = {
        "threshold": {"type": "number", "minimum": 0, "maximum": 1,
                      "description": "Retained changed-pixel fraction in the ROI (0..1)."},
        "region": {"type": "array", "items": {"type": "integer"},
                   "minItems": 4, "maxItems": 4,
                   "description": "Optional [x,y,w,h] in original capture pixels. Must be inside the frame."},
        "min_changed_area": {"type": "number", "minimum": 0, "maximum": 1,
                             "description": "Minimum connected component area as a fraction of ROI; default 0.001."},
    }
    wait = {
        **common,
        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 600,
                            "description": "Default 30; capped by runtime max_wait_seconds (60)."},
        "poll_ms": {"type": "integer", "minimum": 10, "maximum": 60000,
                    "description": "Delay between fresh captures, default 200 ms."},
    }
    change = {
        **wait,
        "auto_baseline": {"type": "boolean", "default": True,
                          "description": "Seed only if absent. Capture before acting to detect immediate changes."},
        "update_baseline_on_change": {"type": "boolean", "default": False},
    }

    def tool(name, description, properties):
        return Tool(name=name, description=description,
                    inputSchema={"type": "object", "properties": properties, "required": []},
                    annotations={"readOnlyHint": True, "destructiveHint": False})

    return [
        tool("set_screen_baseline", "Store a full-resolution frame and optional default ROI before an action. capture_screen also stores a baseline.",
             {"region": common["region"]}),
        tool("get_changed_regions", "Compare current frame with the saved baseline. Returns score and filtered boxes in original capture pixels. No text or UI interpretation.",
             {**common, "auto_baseline": {"type": "boolean", "default": False}}),
        tool("wait_for_change", "Wait for filtered image changes against a fixed pre-action baseline. Default threshold 0.02. No text or UI interpretation.", change),
        tool("wait_for_stable", "Wait for N consecutive frame differences at or below threshold (default 0.001, N=4). Needs N+1 captures; stability does not verify UI or command success.",
             {**wait, "stable_frames": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 4}}),
        tool("screen_changed", "DEPRECATED compatibility alias of get_changed_regions; uses the same OpenCV engine.",
             {**common, "auto_baseline": {"type": "boolean", "default": False}}),
        tool("wait_for_screen_change", "DEPRECATED compatibility alias of wait_for_change; uses the same OpenCV engine.", change),
    ]
