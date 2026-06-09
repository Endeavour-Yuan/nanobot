"""Abstract backend interface for Computer Use desktop control.

Provides a dataclass-based interface for screen capture, input simulation,
and accessibility-tree querying. Concrete backends (e.g. cua-driver) implement
this ABC to drive the desktop.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class UIElement:
    """A single UI element from an accessibility tree query."""

    index: int
    role: str
    label: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    app: str = ""
    pid: int = 0
    window_id: int = 0
    attributes: dict = field(default_factory=dict)

    def center(self) -> tuple[int, int]:
        """Return the centre (x, y) of this element's bounding box."""
        x, y, w, h = self.bounds
        return (x + w // 2, y + h // 2)


@dataclass
class CaptureResult:
    """Result of a screen capture.

    Fields are populated depending on *mode*:

    - ``screen`` — always populates ``mode``, ``width``, ``height``,
      ``png_b64``, ``png_bytes_len``.  ``app`` / ``window_title`` may also
      be set when a target app is given.
    - ``elements`` or ``som`` — same as above **plus** the ``elements``
      list is filled with the interactive accessibility tree.
    """

    mode: str
    width: int
    height: int
    png_b64: str | None = None
    elements: list[UIElement] = field(default_factory=list)
    app: str = ""
    window_title: str = ""
    png_bytes_len: int = 0


@dataclass
class ActionResult:
    """Result returned by every backend action method."""

    ok: bool
    action: str
    message: str = ""
    capture: CaptureResult | None = None
    meta: dict = field(default_factory=dict)


class ComputerUseBackend(ABC):
    """Abstract interface for a Computer Use desktop-control backend.

    A concrete backend must implement every ``@abstractmethod`` below.
    The ``wait`` method has a sensible default and may be overridden.
    """

    # -- lifecycle -------------------------------------------------------

    @abstractmethod
    def start(self) -> None:
        """Initialise the backend, establishing any required connections."""

    @abstractmethod
    def stop(self) -> None:
        """Shut down the backend and release any held resources."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return ``True`` when the backend is ready to accept commands."""

    # -- capture ---------------------------------------------------------

    @abstractmethod
    def capture(self, mode: str = "som", app: str | None = None) -> CaptureResult:
        """Capture the screen and optionally the accessibility tree.

        Parameters
        ----------
        mode:
            ``"screen"`` — screenshot only.
            ``"som"`` / ``"elements"`` — screenshot + interactive elements.
        app:
            Optional bundle-id or process name to restrict capture to a
            specific application.  When ``None`` the full screen is captured.

        Returns
        -------
        A ``CaptureResult`` whose fields are populated according to *mode*.
        """

    # -- pointer input ---------------------------------------------------

    @abstractmethod
    def click(
        self,
        *,
        element: int | None = None,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: list[str] | None = None,
    ) -> ActionResult:
        """Click on an element or at an absolute coordinate."""

    @abstractmethod
    def drag(
        self,
        *,
        from_element: int | None = None,
        to_element: int | None = None,
        from_xy: tuple[int, int] | None = None,
        to_xy: tuple[int, int] | None = None,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> ActionResult:
        """Drag / swipe from one point or element to another."""

    @abstractmethod
    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: int | None = None,
        x: int | None = None,
        y: int | None = None,
        modifiers: list[str] | None = None,
    ) -> ActionResult:
        """Scroll in *direction* by *amount`` clicks."""

    # -- keyboard input --------------------------------------------------

    @abstractmethod
    def type_text(self, text: str) -> ActionResult:
        """Type *text`` through the keyboard."""

    @abstractmethod
    def key(self, keys: str) -> ActionResult:
        """Press a key chord.

        Examples: ``"return"``, ``"ctrl+c"``, ``"command+shift+4"``.
        """

    # -- application management ------------------------------------------

    @abstractmethod
    def list_apps(self) -> list[str]:
        """Return a list of running application names / bundle-ids."""

    @abstractmethod
    def focus_app(self, app: str, raise_window: bool = True) -> ActionResult:
        """Bring *app`` to the foreground."""

    @abstractmethod
    def set_value(self, value: str, element: int | None = None) -> ActionResult:
        """Set a value on an accessibility element directly."""

    # -- utilities -------------------------------------------------------

    def wait(self, seconds: float) -> ActionResult:
        """Block for *seconds`` (clamped to [0, 30])."""
        seconds = max(0.0, min(seconds, 30.0))
        time.sleep(seconds)
        return ActionResult(ok=True, action="wait", message=f"waited {seconds:.2f}s")
