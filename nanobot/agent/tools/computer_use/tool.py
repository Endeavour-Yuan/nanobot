"""Computer Use tool — single `computer_use` tool with action routing.

Model-agnostic OpenAI function-calling schema. Safety: destructive system
shortcuts are hard-blocked. LLM behavioral constraints are in the skill.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    NumberSchema,
    StringSchema,
)
from nanobot.config.schema import ComputerUseToolConfig

logger = logging.getLogger(__name__)

# ── Hard-blocked key combinations (destructive system shortcuts) ──
_BLOCKED_KEY_COMBOS: set[frozenset[str]] = {
    frozenset({"cmd", "shift", "q"}),  # log out
    frozenset({"cmd", "ctrl", "q"}),  # lock screen
    frozenset({"cmd", "option", "shift", "q"}),  # force log out
    frozenset({"cmd", "shift", "backspace"}),  # empty trash
    frozenset({"cmd", "option", "backspace"}),  # force delete
}

_KEY_ALIASES = {"command": "cmd", "control": "ctrl", "alt": "option"}

# ── Dangerous text patterns for type action ──
_BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"sudo\s+rm\s+-[rf]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}", re.IGNORECASE),  # fork bomb
]





@tool_parameters({
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "capture", "click", "double_click", "right_click",
                "middle_click", "drag", "scroll", "type", "key",
                "set_value", "wait", "list_apps", "focus_app",
            ],
            "description": (
                "Which action to perform. capture is free (no side effects). "
                "All other actions are safety-checked."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["som", "vision", "ax"],
            "description": (
                "Capture mode for action=capture. som (default)=screenshot with "
                "numbered element overlays + AX tree. vision=plain screenshot. "
                "ax=accessibility tree only (no image)."
            ),
        },
        "app": StringSchema(
            "Optional. Limit capture/action to a specific app by name "
            "(e.g. 'Safari') or bundle ID (e.g. 'com.apple.Safari'). "
            "If omitted, operates on the frontmost window."
        ),
        "max_elements": IntegerSchema(
            100,
            description=(
                "Optional cap on AX elements from capture. "
                "Default 100, max 1000."
            ),
            minimum=1,
            maximum=1000,
        ),
        "element": IntegerSchema(
            description=(
                "1-based SOM index from last capture(mode='som'). "
                "Preferred over raw coordinates."
            ),
            minimum=1,
        ),
        "coordinate": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
            "description": "Pixel coords [x,y]. Use only when no element index.",
        },
        "button": {
            "type": "string",
            "enum": ["left", "right", "middle"],
            "description": "Mouse button. Default left.",
        },
        "modifiers": ArraySchema(
            StringSchema(
                enum=["cmd", "shift", "option", "alt", "ctrl", "fn"],
            ),
            description="Modifier keys held during action.",
        ),
        "from_element": IntegerSchema(description="Source element index (drag).", minimum=1),
        "to_element": IntegerSchema(description="Target element index (drag).", minimum=1),
        "from_coordinate": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
            "description": "Source [x,y] (drag fallback).",
        },
        "to_coordinate": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
            "description": "Target [x,y] (drag fallback).",
        },
        "direction": {
            "type": "string",
            "enum": ["up", "down", "left", "right"],
            "description": "Scroll direction.",
        },
        "amount": IntegerSchema(3, description="Scroll wheel ticks. Default 3.", minimum=1, maximum=50),
        "text": StringSchema("Text to type."),
        "keys": StringSchema(
            "Key combo, e.g. 'cmd+s', 'return', 'escape', 'tab'. Use '+' to combine."
        ),
        "value": StringSchema(
            "For set_value: value to set on element (e.g. dropdown option label)."
        ),
        "seconds": NumberSchema(1.0, description="Seconds to wait. Max 30.", minimum=0, maximum=30),
        "raise_window": BooleanSchema(
            description=(
                "Only for focus_app. If true, brings window to front (DISRUPTS user). "
                "Default false."
            ),
            default=False,
            nullable=True,
        ),
        "capture_after": BooleanSchema(
            description=(
                "If true, take follow-up capture after the action. "
                "Saves a round-trip when verifying effect."
            ),
            default=False,
            nullable=True,
        ),
    },
    "required": ["action"],
})
class ComputerUseTool(Tool):
    """macOS Computer Use — drive the desktop via cua-driver.

    Background operation: does NOT steal user's cursor, focus, or Space.
    Requires: macOS + cua-driver binary installed.
    """

    config_key = "computer_use"

    @classmethod
    def config_cls(cls):
        return ComputerUseToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.computer_use.enabled

    @classmethod
    def create(cls, ctx: Any) -> ComputerUseTool:
        cfg = getattr(ctx.config, "computer_use", None) or ComputerUseToolConfig()
        return cls(cfg=cfg)

    def __init__(self, cfg: ComputerUseToolConfig | None = None) -> None:
        self._cfg = cfg or ComputerUseToolConfig()

    @property
    def name(self) -> str:
        return "computer_use"

    @property
    def description(self) -> str:
        return (
            "Drive the macOS desktop in the background — screenshots, mouse, "
            "keyboard, scroll, drag — without stealing the user's cursor, "
            "keyboard focus, or Space. Preferred workflow: action=capture "
            "(mode=som gives numbered element overlays), then click by element "
            "index. Works on any window — hidden, minimized, on another Space. "
            "macOS only; requires cua-driver installed."
        )

    @property
    def read_only(self) -> bool:
        return False  # destructive: click/type/key/scroll/drag modify desktop state

    # ── Backend lifecycle (lazy init, per-process singleton) ──
    _backend: Any = None
    _backend_lock = threading.Lock()

    @classmethod
    def _get_backend(cls) -> Any:
        if cls._backend is not None:
            return cls._backend
        with cls._backend_lock:
            if cls._backend is not None:
                return cls._backend
            from nanobot.agent.tools.computer_use.cua_backend import CuaDriverBackend
            cls._backend = CuaDriverBackend()
            cls._backend.start()
            return cls._backend

    # ── Safety helpers ──
    @staticmethod
    def _canon_key_combo(keys: str) -> frozenset[str]:
        parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys) if p.strip()]
        parts = [_KEY_ALIASES.get(p, p) for p in parts]
        return frozenset(parts)

    @classmethod
    def _check_blocked_key(cls, keys: str) -> str | None:
        combo = cls._canon_key_combo(keys)
        for blocked in _BLOCKED_KEY_COMBOS:
            if blocked.issubset(combo):
                return f"blocked key combo: {sorted(blocked)}"
        return None

    @classmethod
    def _check_blocked_type(cls, text: str) -> str | None:
        for pat in _BLOCKED_TYPE_PATTERNS:
            if pat.search(text):
                return f"blocked pattern in type text: {pat.pattern!r}"
        return None

    # ── Execute ──
    async def execute(self, action: str, **kwargs: Any) -> Any:
        action = action.strip().lower()

        # Safety checks
        if action == "key":
            keys = kwargs.get("keys", "")
            err = self._check_blocked_key(keys)
            if err:
                return json.dumps(
                    {"error": err, "hint": "Destructive system shortcuts are hard-blocked."}
                )

        if action == "type":
            text = kwargs.get("text", "")
            err = self._check_blocked_type(text)
            if err:
                return json.dumps(
                    {"error": err, "hint": "Dangerous shell patterns cannot be typed via computer_use."}
                )

        # Action validation (before backend check to avoid connecting for invalid actions)
        valid_actions = {
            "capture", "click", "double_click", "right_click",
            "middle_click", "drag", "scroll", "type", "key",
            "set_value", "wait", "list_apps", "focus_app",
        }
        if action not in valid_actions:
            return json.dumps({"error": f"unknown action {action!r}"})

        # Platform check
        if sys.platform != "darwin":
            return json.dumps({"error": "computer_use is macOS only."})

        # Backend check
        try:
            backend = self._get_backend()
        except Exception as e:
            return json.dumps({
                "error": f"cua-driver backend unavailable: {e}",
                "hint": "Install cua-driver: https://github.com/trycua/cua",
            })

        if not backend.is_available():
            return json.dumps({
                "error": "cua-driver not found on PATH.",
                "hint": "Install cua-driver: https://github.com/trycua/cua",
            })

        try:
            return await self._dispatch(backend, action, **kwargs)
        except Exception as e:
            logger.exception("computer_use %s failed", action)
            return json.dumps({"error": f"{action} failed: {e}"})

    async def _dispatch(self, backend: Any, action: str, **kwargs: Any) -> Any:
        capture_after = bool(kwargs.get("capture_after"))

        if action == "capture":
            mode = str(kwargs.get("mode", "som"))
            if mode not in ("som", "vision", "ax"):
                return json.dumps({"error": f"bad mode {mode!r}; use som|vision|ax"})
            cap = backend.capture(mode=mode, app=kwargs.get("app"))
            return await self._format_capture(
                cap,
                max_elements=int(kwargs.get("max_elements", 100)),
            )

        if action == "wait":
            seconds = float(kwargs.get("seconds", 1.0))
            res = backend.wait(seconds)
            return self._format_result(res)

        if action == "list_apps":
            apps = backend.list_apps()
            return json.dumps({"apps": apps, "count": len(apps)})

        if action == "focus_app":
            app = kwargs.get("app")
            if not app:
                return json.dumps({"error": "focus_app requires app parameter."})
            res = backend.focus_app(app, raise_window=bool(kwargs.get("raise_window")))
            return await self._maybe_follow_capture(backend, res, capture_after)

        if action in ("click", "double_click", "right_click", "middle_click"):
            button = kwargs.get("button", "left")
            click_count = 1
            if action == "double_click":
                click_count = 2
            elif action == "right_click":
                button = "right"
            elif action == "middle_click":
                button = "middle"

            element = kwargs.get("element")
            coord = kwargs.get("coordinate")
            x = coord[0] if coord and len(coord) >= 2 else None
            y = coord[1] if coord and len(coord) >= 2 else None

            res = backend.click(
                element=element, x=x, y=y,
                button=button, click_count=click_count,
                modifiers=kwargs.get("modifiers"),
            )
            return await self._maybe_follow_capture(backend, res, capture_after)

        if action == "drag":
            from_el = kwargs.get("from_element")
            to_el = kwargs.get("to_element")
            from_coord = tuple(kwargs["from_coordinate"]) if kwargs.get("from_coordinate") else None
            to_coord = tuple(kwargs["to_coordinate"]) if kwargs.get("to_coordinate") else None
            if from_el is None and from_coord is None:
                return json.dumps({"error": "drag requires from_element or from_coordinate."})
            res = backend.drag(
                from_element=from_el, to_element=to_el,
                from_xy=from_coord, to_xy=to_coord,
                button=kwargs.get("button", "left"),
                modifiers=kwargs.get("modifiers"),
            )
            return await self._maybe_follow_capture(backend, res, capture_after)

        if action == "scroll":
            coord = kwargs.get("coordinate")
            x = coord[0] if coord and len(coord) >= 2 else None
            y = coord[1] if coord and len(coord) >= 2 else None
            res = backend.scroll(
                direction=kwargs.get("direction", "down"),
                amount=int(kwargs.get("amount", 3)),
                element=kwargs.get("element"), x=x, y=y,
                modifiers=kwargs.get("modifiers"),
            )
            return await self._maybe_follow_capture(backend, res, capture_after)

        if action == "type":
            res = backend.type_text(kwargs.get("text", ""))
            return await self._maybe_follow_capture(backend, res, capture_after)

        if action == "key":
            res = backend.key(kwargs.get("keys", ""))
            return await self._maybe_follow_capture(backend, res, capture_after)

        if action == "set_value":
            value = kwargs.get("value")
            if value is None:
                return json.dumps({"error": "set_value requires value parameter."})
            res = backend.set_value(str(value), element=kwargs.get("element"))
            return await self._maybe_follow_capture(backend, res, capture_after)

        return json.dumps({"error": f"unknown action {action!r}"})

    async def _format_capture(self, cap: Any, max_elements: int = 100) -> str:
        total = len(cap.elements)
        visible = cap.elements[:max_elements]
        truncated = max(0, total - len(visible))

        summary_lines = [
            f"capture mode={cap.mode} {cap.width}x{cap.height}"
            + (f" app={cap.app}" if cap.app else "")
            + (f" window={cap.window_title!r}" if cap.window_title else ""),
            f"{total} interactable element(s):",
        ]
        summary_lines.extend(self._format_elements(visible))
        element_summary = "\n".join(summary_lines)

        # Vision routing: if a vision provider is configured and we have a
        # screenshot, send it through the aux vision model so the main model
        # (which may be text-only) gets a text description.
        if cap.png_b64 and cap.mode != "ax" and self._cfg.vision_provider:
            vision_desc = await self._describe_with_vision(
                png_b64=cap.png_b64,
                element_summary=element_summary,
            )
            payload: dict[str, Any] = {
                "mode": cap.mode, "width": cap.width, "height": cap.height,
                "app": cap.app, "window_title": cap.window_title,
                "elements": [self._element_to_dict(e) for e in visible],
                "total_elements": total,
                "summary": element_summary,
            }
            if vision_desc:
                payload["vision_analysis"] = vision_desc
                payload["summary"] += f"\n\nVision description:\n{vision_desc}"
            if truncated:
                payload["truncated_elements"] = truncated
            return json.dumps(payload)

        if cap.png_b64 and cap.mode != "ax":
            _b64_prefix = cap.png_b64[:8]
            _mime = "image/jpeg" if _b64_prefix.startswith("/9j/") else "image/png"
            payload = {
                "mode": cap.mode, "width": cap.width, "height": cap.height,
                "app": cap.app, "window_title": cap.window_title,
                "elements": [self._element_to_dict(e) for e in visible],
                "total_elements": total,
                "summary": element_summary,
                "image_b64": cap.png_b64,
                "image_mime": _mime,
            }
            if truncated:
                payload["truncated_elements"] = truncated
            return json.dumps(payload)
        else:
            payload = {
                "mode": cap.mode, "width": cap.width, "height": cap.height,
                "app": cap.app, "window_title": cap.window_title,
                "elements": [self._element_to_dict(e) for e in visible],
                "total_elements": total,
                "summary": element_summary,
            }
            if truncated:
                payload["truncated_elements"] = truncated
                payload["summary"] += (
                    f"\n(response truncated to {len(visible)} of {total} elements; "
                    f"raise max_elements or pass app= to narrow)"
                )
            return json.dumps(payload)

    async def _describe_with_vision(
        self, png_b64: str, element_summary: str,
    ) -> str | None:
        """Call the configured aux vision model to describe a screenshot."""
        from nanobot.agent.tools.computer_use.vision import describe_screenshot
        return await describe_screenshot(
            png_b64=png_b64,
            provider=self._cfg.vision_provider,
            model=self._cfg.vision_model,
            base_url=self._cfg.vision_base_url,
            api_key=self._cfg.vision_api_key,
            element_summary=element_summary,
        )

    def _format_result(self, res: Any) -> str:
        payload: dict[str, Any] = {"ok": res.ok, "action": res.action}
        if res.message:
            payload["message"] = res.message
        return json.dumps(payload)

    async def _maybe_follow_capture(self, backend: Any, res: Any, do_capture: bool) -> str:
        if not do_capture or not res.ok:
            return self._format_result(res)
        try:
            cap = backend.capture(mode="som")
        except Exception as e:
            logger.warning("follow-up capture failed: %s", e)
            return self._format_result(res)
        capture_json = await self._format_capture(cap)
        try:
            data = json.loads(capture_json)
        except (TypeError, json.JSONDecodeError):
            data = {"capture": capture_json}
        data["action"] = res.action
        data["ok"] = res.ok
        if res.message:
            data["message"] = res.message
        return json.dumps(data)

    @staticmethod
    def _format_elements(elements: list[Any], max_lines: int = 40) -> list[str]:
        out: list[str] = []
        for e in elements[:max_lines]:
            label = e.label.replace("\n", " ")[:60]
            out.append(
                f"  #{e.index} {e.role} {label!r} @ {e.bounds}"
                + (f" [{e.app}]" if e.app else "")
            )
        if len(elements) > max_lines:
            out.append(f"  ... +{len(elements) - max_lines} more")
        return out

    @staticmethod
    def _element_to_dict(e: Any) -> dict[str, Any]:
        return {
            "index": e.index,
            "role": e.role,
            "label": e.label,
            "bounds": list(e.bounds),
            "app": e.app,
        }
