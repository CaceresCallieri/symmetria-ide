"""The IDE-owned Google Chrome process: profile, Wayland display, CDP.

This replaces the embedded QtWebEngine pool. The trade it made: we gave up
rendering the browser inside our own window, and in exchange the agentic
browser became a browser that actually works — `Target.createTarget` (the
`new_page` every chrome-devtools-mcp workflow starts from) is unsupported on
QtWebEngine, screenshots stall when the IDE is off-workspace, and there are no
extensions and no real logins.

**Chrome renders INSIDE the IDE again, without giving any of that up.** The IDE
hosts a nested Wayland compositor (qml/browser/), and Chrome connects to it as
an ordinary Wayland client — a real, unmodified browser whose surfaces happen
to land in our scene graph. Containment is now categorical rather than
enforced: `hyprctl clients` does not list the browser AT ALL, so there is no
window to escape, no map-time race to lose, and no workspace rule to maintain
(the whole `hyprland_ipc` module went away with it).

Measured before committing to it: with the IDE on an INACTIVE workspace and the
browser surface hidden four different ways, `Page.captureScreenshot` stayed at
~60ms and the page kept a full 60Hz of requestAnimationFrame. The obvious fear —
that an unrendered surface stops getting frame callbacks and reintroduces the
QtWebEngine stall by another door — did not materialise.

Three constraints shape everything here, and all three trace back to one fact:
**Chrome is a singleton per `--user-data-dir`.**

1. **One Chrome process per IDE instance.** A second IDE pointed at the same
   profile would not start its own process — it would hand the request to the
   first one over IPC, and the resulting window would render in the FIRST
   IDE's compositor. Verified live under the pinned backend, where the same
   handoff put windows on the wrong workspace; a Chrome process also connects
   to exactly ONE Wayland display, so nesting only sharpens the constraint.
2. **Therefore one profile per project.** Not a preference; the process
   identity chain is `profile → process → Wayland display → our window`.
3. **Therefore logins are seeded, not shared.** A template profile
   (`<data>/browser/_template`) is where the user logs into the dashboards
   once; each project profile is cloned from it on first use. Cookies survive
   the copy because Chrome's encryption key lives in the OS keyring, shared
   across profiles of the same user — but `Local State` must come along, since
   it carries the wrapped key reference.

This profile model was designed under the pinned backend and carried over
unchanged, exactly as its original note predicted it would.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading

from PySide6.QtCore import QObject, Signal, Slot

from .cdp_client import CdpClient

log = logging.getLogger(__name__)

# Chrome package names, most specific first.
_CHROME_EXECUTABLES = ("google-chrome-stable", "google-chrome", "chromium")

# Files copied from the template into a fresh project profile. A seeded profile
# can be hundreds of MB (cache, GPU shaders, site data); these carry the logins
# and nothing else. `Local State` is profile-ROOT, not inside Default, and is
# non-optional: it holds the OS-crypt wrapped key without which every copied
# cookie decrypts to garbage.
_TEMPLATE_ROOT_FILES = ("Local State",)
_TEMPLATE_PROFILE_FILES = (
    "Cookies",
    "Login Data",
    "Login Data For Account",
    "Preferences",
    "Secure Preferences",
    "Web Data",
)
_TEMPLATE_PROFILE_DIRS = ("Local Storage", "IndexedDB")

_SLUG_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _machine_state() -> str:
    """Available memory and load, for the log line at an unexpected Chrome exit.

    Read straight from procfs rather than through psutil: this runs on the path
    where Chrome has just died, so it must not depend on an optional package,
    and it must not raise — a diagnostic that can break the handler it is
    diagnosing is worse than no diagnostic. Everything is best-effort and any
    failure degrades to "unknown".

    MemAvailable rather than MemFree, deliberately: free memory is routinely
    near zero on a healthy machine because the page cache uses it, and reading
    the wrong one is how "out of memory" gets diagnosed where there is none.
    """
    memory = "mem unknown"
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    memory = f"{int(line.split()[1]) / 1024 / 1024:.1f}GiB available"
                    break
    # ValueError/IndexError as well as OSError: a malformed or unexpectedly
    # shaped `MemAvailable:` line would otherwise raise out of a handler that
    # runs when Chrome has ALREADY died — and taking `browserGone.emit()` down
    # with it would turn a diagnostic into an outage.
    except (OSError, ValueError, IndexError):
        pass

    load = "load unknown"
    try:
        one, five, fifteen = os.getloadavg()
        load = f"load {one:.2f} {five:.2f} {fifteen:.2f}"
    except OSError:
        pass

    return f"{memory}, {load}"


def data_home() -> str:
    """`$XDG_DATA_HOME/symmetria-ide` (never writes outside XDG — §9)."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "symmetria-ide")


def browser_root() -> str:
    return os.path.join(data_home(), "browser")


def template_profile_dir() -> str:
    """Where the user logs in once. Cloned into every project profile."""
    return os.path.join(browser_root(), "_template")


def project_slug(project_root: str) -> str:
    """A filesystem-safe, collision-resistant name for a project's profile.

    Basename for legibility (so a human can tell whose profile is whose) plus
    a short path digest, because two checkouts of the same repo — a worktree,
    or dev vs stable — share a basename but must not share a profile: they run
    as separate IDE instances, and a shared profile would collapse them onto
    one Chrome process.
    """
    root = (project_root or "").rstrip("/") or "default"
    base = _SLUG_SAFE.sub("-", os.path.basename(root)) or "project"
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def profile_dir_for(project_root: str) -> str:
    return os.path.join(browser_root(), project_slug(project_root))


def chrome_executable() -> str:
    """The Chrome binary to launch, or "" when there isn't a usable one.

    `SYMMETRIA_IDE_CHROME_BIN` overrides the PATH search — for a non-standard
    install, and as the suite's hard off switch (tests/conftest.py points it at
    a nonexistent path so no test can ever spawn a real browser). When the
    override is set but unusable we return "" rather than falling back: a typo
    silently launching some other browser is worse than a logged no-browser.
    """
    override = os.environ.get("SYMMETRIA_IDE_CHROME_BIN")
    if override is not None:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        log.warning("SYMMETRIA_IDE_CHROME_BIN=%r is not executable", override)
        return ""
    for name in _CHROME_EXECUTABLES:
        path = shutil.which(name)
        if path:
            return path
    return ""


def browser_identity(pid: int) -> str:
    """This IDE's browser identity, used for two things at once.

    It is both the Wayland `app_id` Chrome's toplevels carry and the name of
    the nested compositor's socket. One string because it is genuinely one
    identity, expressed in two protocols. (The pane does NOT currently filter
    on the app_id — anything that can reach the socket gets a surface. That is
    acceptable only because reaching it requires being the same user and
    knowing a per-pid name; do not read the shared string as a guarantee.)

    Per-PID rather than per-project: it must address ONE running IDE, and two
    IDEs can legitimately have the same project open (dev and stable, or two
    worktrees) — and two compositors cannot share a socket name.
    """
    return f"symmetria-browser-{pid}"


def wayland_socket_path(socket_name: str) -> str:
    """Where the nested compositor's socket lives, per the Wayland spec."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime_dir, socket_name)


def seed_profile_from_template(profile: str, template: str) -> bool:
    """Clone the login-bearing subset of `template` into a fresh `profile`.

    Returns True when a seed actually happened. No template, or a profile that
    already exists, is a normal no-op — the user simply logs in inside the
    project, and nothing is ever overwritten (an overwrite would silently
    destroy sessions established in that project).
    """
    if os.path.exists(profile):
        return False
    if not os.path.isdir(template):
        log.info("no browser template at %s — starting with a blank profile", template)
        return False
    # Seed into a staging dir and rename into place, so a failure mid-copy
    # leaves NOTHING behind. Copying straight into `profile` would leave a
    # half-populated directory that this function's own existence check then
    # treats as "already seeded" forever — the user would be silently logged
    # out of every dashboard, permanently, from one transient OSError.
    staging = f"{profile}.seeding"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        os.makedirs(os.path.join(staging, "Default"), exist_ok=True)
        for name in _TEMPLATE_ROOT_FILES:
            _copy_if_present(os.path.join(template, name), os.path.join(staging, name))
        for name in _TEMPLATE_PROFILE_FILES:
            _copy_if_present(
                os.path.join(template, "Default", name),
                os.path.join(staging, "Default", name),
            )
        for name in _TEMPLATE_PROFILE_DIRS:
            source = os.path.join(template, "Default", name)
            if os.path.isdir(source):
                shutil.copytree(
                    source, os.path.join(staging, "Default", name), dirs_exist_ok=True
                )
        os.rename(staging, profile)
    except OSError:
        log.exception("browser profile seed from %s failed", template)
        shutil.rmtree(staging, ignore_errors=True)
        return False
    log.info("seeded browser profile %s from template", profile)
    return True


def _copy_if_present(source: str, destination: str) -> None:
    if os.path.isfile(source):
        shutil.copy2(source, destination)


def build_chrome_argv(
    executable: str, window_class: str, profile: str, cdp_port: int, url: str = ""
) -> list[str]:
    """Chrome's launch argv.

    Deliberately NOT `--app=<url>`: the user wants the full browser — tabs,
    omnibox, extensions — because this browser doubles as something they show
    to other people, not just an agent's viewport.

    `--ozone-platform=wayland` is not an optimisation, it is the containment.
    It forces Chrome onto the Wayland socket we hand it in the environment;
    without it Chrome may pick the X11/XWayland backend, which talks to the
    HOST display — the browser would then open a loose window on whatever
    workspace the user is looking at, which is the one thing this must never
    do. The caller additionally strips `DISPLAY`, so there is no X11 path to
    fall back to even if the flag were dropped.

    `url` is load-bearing on a COLD start. Chrome always opens a window when it
    launches, so starting it bare and then asking for a window yields TWO: a
    stray `chrome://newtab/` plus the one that was wanted. The registry adopts
    whichever target CDP discovers first, which is the stray — leaving the real
    window unattributed and un-closable. Launching straight at the target url
    makes the startup window BE the requested window. (Observed live, not
    theorised: the newtab target arrived first every time.)
    """
    argv = [
        executable,
        "--ozone-platform=wayland",
        f"--class={window_class}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # We terminate Chrome on IDE quit (SIGTERM), and Chrome scores that as
        # an unclean exit — so without this every single launch opens with a
        # "Restore pages?" bubble. It is pure noise here: an agent's browsing
        # session is not something anyone wants restored, and the bubble is an
        # xdg-popup that lands over the page in a pane that has no room for it.
        # Suppressing the bubble is the fix rather than chasing a cleaner exit,
        # because the exit is not fully ours to control.
        "--hide-crash-restore-bubble",
    ]
    if cdp_port > 0:
        argv.append(f"--remote-debugging-port={cdp_port}")
    if url:
        argv.append(url)
    return argv


class ChromeHost(QObject):
    """Owns the Chrome process, its window rule, and the CDP session."""

    #: (target_id, url, title) — a window appeared or changed.
    windowUpdated = Signal(str, str, str)
    #: (target_id) — a window went away (user closed it, or we did).
    windowGone = Signal(str)
    #: Chrome itself is gone (quit, killed, crashed) — every window with it.
    #: The owner must drop its whole registry: otherwise browser_list_windows
    #: keeps reporting windows that no longer exist and agents act on the lie.
    browserGone = Signal()

    def __init__(self, project_root: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._project_root = project_root
        self._window_class = browser_identity(os.getpid())
        self._profile = profile_dir_for(project_root)
        self._proc: subprocess.Popen | None = None
        self._stopping = False
        self._cdp = CdpClient(self)
        self._cdp.targetUpdated.connect(self.windowUpdated)
        self._cdp.targetGone.connect(self.windowGone)
        self._cdp.disconnected.connect(self._on_cdp_disconnected)

    @property
    def window_class(self) -> str:
        return self._window_class

    @property
    def wayland_socket(self) -> str:
        """The nested compositor's socket name — same string as the app_id."""
        return self._window_class

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def cdp_port(self) -> int:
        """The port reserved at startup in `app.run()`.

        Read from the environment rather than passed in: it is the single
        source of truth that `browser_mcp` also reads when building each
        agent's chrome-devtools-mcp entry, so a second copy could drift.
        """
        try:
            return int(os.environ.get("SYMMETRIA_IDE_CDP_PORT", "") or 0)
        except ValueError:
            return 0

    # -- lifecycle ------------------------------------------------------

    def ensure_running(self, initial_url: str = "") -> str:
        """Start Chrome if needed. Returns "" on success, else an error code.

        Lazy on purpose: a project that never opens a browser never pays for
        one. Called on every window open, so it doubles as the reconnect path
        after a user-killed Chrome.

        `initial_url` is used only on a cold start — see `build_chrome_argv`
        for why the startup window must be the requested one.
        """
        if self.is_running:
            self._cdp.connect_to(self.cdp_port())
            return ""
        executable = chrome_executable()
        if not executable:
            log.warning("no Chrome on PATH — agent browser tools unavailable")
            return "chrome-not-installed"
        # The nested compositor is created with the QML engine, long before any
        # browser request, so this normally passes. It is checked anyway rather
        # than assumed because the failure is otherwise silent AND harmful:
        # Chrome given a socket that does not exist falls back to whatever
        # display it can find, which means a loose window on the user's
        # workspace — the precise outcome the nested backend exists to make
        # impossible.
        socket_path = wayland_socket_path(self.wayland_socket)
        if not os.path.exists(socket_path):
            log.warning("nested compositor socket missing at %s", socket_path)
            return "compositor-not-ready"
        seed_profile_from_template(self._profile, template_profile_dir())
        try:
            os.makedirs(self._profile, exist_ok=True)
        except OSError:
            log.exception("could not create browser profile dir %s", self._profile)
            return "profile-unavailable"
        argv = build_chrome_argv(
            executable, self._window_class, self._profile, self.cdp_port(), initial_url
        )
        try:
            self._proc = subprocess.Popen(
                argv,
                env=self.chrome_env(),
                stdout=subprocess.DEVNULL,
                # NOT DEVNULL: this is the ONLY channel that explains a Chrome
                # crash. Chrome dies on a Wayland protocol error or a failed
                # CHECK by printing the reason here and then raising SIGTRAP —
                # and the core it leaves is a stripped binary, so the backtrace
                # is bare addresses. Discarding this makes a crash look like
                # "the browser silently vanished", which is exactly what it
                # looked like the first time (chased through coredumpctl for
                # nothing). Cheap: Chrome is quiet in normal operation.
                stderr=subprocess.PIPE,
            )
        except OSError:
            log.exception("failed to spawn Chrome")
            return "chrome-spawn-failed"
        log.info(
            "Chrome spawned (display=%s, profile=%s)",
            self.wayland_socket,
            self._profile,
        )
        # Reap in the background so a user-closed Chrome doesn't linger as a
        # zombie for the life of the IDE. Draining stderr is part of reaping,
        # not a separate nicety: a PIPE nobody reads fills its buffer and
        # BLOCKS Chrome the moment it becomes chatty.
        #
        # The process is HANDED to the thread rather than re-read from
        # `self._proc` there: `stop()` sets that to None and a respawn after a
        # crash rebinds it, so a thread that read it later would either skip
        # its own process (leaking the fd and leaving a zombie) or end up
        # draining a DIFFERENT process's pipe alongside its real reader.
        self._start_stderr_drain(self._proc, "chrome-reap")
        self._cdp.connect_to(self.cdp_port())
        return ""

    def chrome_env(self) -> dict[str, str]:
        """The environment that binds Chrome to OUR compositor.

        `DISPLAY` is REMOVED, not just overridden. Chrome's Ozone layer will
        happily use XWayland when a Wayland connection is unavailable, and an
        X11 Chrome talks to the host — producing exactly the loose window on
        the user's workspace that the nested backend exists to prevent. With no
        `DISPLAY` there is nothing to fall back to, so a broken nested socket
        fails LOUDLY (Chrome exits) instead of quietly escaping.
        """
        env = dict(os.environ)
        env["WAYLAND_DISPLAY"] = self.wayland_socket
        env["XDG_SESSION_TYPE"] = "wayland"
        env.pop("DISPLAY", None)
        # `WAYLAND_SOCKET` is the fd-passing form of a Wayland connection and
        # libwayland prefers it OVER `WAYLAND_DISPLAY`. If it is ever present
        # in the IDE's own environment (inherited from a launcher), Chrome
        # would connect to the HOST compositor while our socket sat unused —
        # the same escape `DISPLAY` is popped to prevent, through a door that
        # setting WAYLAND_DISPLAY does not close.
        env.pop("WAYLAND_SOCKET", None)
        return env

    def _start_stderr_drain(self, proc: subprocess.Popen, name: str) -> None:
        """Reap `proc` on a daemon thread, logging whatever it says on the way.

        Every Chrome spawn goes through here — including the warm
        `--new-window` handoff, which can win the race and BECOME the primary
        process, and whose death would otherwise be as unexplainable as the
        cold path's was.
        """
        threading.Thread(
            target=self._drain_chrome_stderr, args=(proc,), daemon=True, name=name
        ).start()

    def _drain_chrome_stderr(self, proc: subprocess.Popen) -> None:
        """Forward Chrome's stderr into the app log, then reap the process.

        Runs on a daemon thread and only ever logs — no Qt signal crosses out
        of here, so it is outside gotcha #10's blast radius.

        Chrome is noisy about things that do not matter (font, GPU and dbus
        warnings on every launch), so the routine lines go to debug. The lines
        that matter announce themselves loudly enough to grep for, and those
        are re-logged at error: a Wayland protocol error or a failed CHECK is
        the last thing Chrome says before SIGTRAP, and it is the only readable
        account of why — the core is a stripped binary.

        Takes `proc` as an argument rather than reading `self._proc`, which
        moves underneath it — see the call site.
        """
        # getattr, not attribute access: `Popen.stderr` is None whenever the
        # pipe was not requested, and this must degrade to "just reap" rather
        # than take the reaper down with it.
        stream = getattr(proc, "stderr", None)
        try:
            if stream is not None:
                for raw in stream:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if not line:
                        continue
                    lowered = line.lower()
                    if (
                        "protocol error" in lowered
                        or "check failed" in lowered
                        or "fatal" in lowered
                    ):
                        log.error("chrome: %s", line)
                    else:
                        log.debug("chrome: %s", line)
        except (OSError, ValueError):
            # The pipe went away with the process — nothing left to report.
            pass
        finally:
            proc.wait()

    @Slot()
    def _on_cdp_disconnected(self) -> None:
        """An established CDP session dropped — Chrome is gone.

        Only fires for a session that WAS attached (see `CdpClient`), so a
        failed initial attach can't be mistaken for Chrome dying.
        """
        if self._stopping:
            return  # our own teardown closed the socket; the owner knows
        # Logged at WARNING with the machine's state, because an UNEXPECTED
        # disconnect is Chrome dying and the interesting question is always
        # "what else was happening". Chrome's own reason is already on the
        # stderr drain (`FATAL: GPU process isn't usable. Goodbye.` is the one
        # seen in the wild), but the reason alone does not say why the GPU
        # process failed THAT time and not the hundred before it.
        #
        # Worth the two extra lines: this crash could not be reproduced on
        # demand. CPU load was ruled out under a controlled run (load 19.6, no
        # failure) and so was a deliberately broken GPU launcher, which leaves
        # AVAILABLE MEMORY as the leading hypothesis on the strength of a single
        # observation (~1.5GiB free when it died, 4.2GiB when it would not
        # reproduce). One line of context at the moment of death settles that on
        # the next occurrence instead of costing another investigation.
        log.warning("Chrome disconnected unexpectedly — %s", _machine_state())
        self.browserGone.emit()

    def stop(self) -> None:
        """Tear down at IDE shutdown: CDP, then the process.

        Nothing compositor-side to undo: the nested compositor dies with the
        QML engine, and its surfaces with it. (The pinned backend had to
        release a Hyprland window rule here — a rule lives in the RUNNING
        compositor, so an IDE that exited without releasing its class left one
        behind forever. Nesting removed that whole class of leak.)
        """
        self._stopping = True
        self._cdp.close()
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.exception("Chrome terminate failed")

    # -- windows --------------------------------------------------------

    def open_window(self, url: str, callback) -> str:
        """Ask Chrome for a new window at `url`.

        Returns "" when the request was dispatched (the `callback` receives the
        CDP result carrying `targetId`), or an error code when it could not be.

        The CDP attach is asynchronous, so the FIRST open after a cold Chrome
        start always races it. Rather than queue commands behind the socket —
        which hides the failure and makes the agent wait on nothing — we let
        Chrome itself open the url: as its startup window when cold, or via a
        `--new-window` handoff when warm-but-unattached. Either way the window
        lands in our compositor — it is the running process's Wayland
        connection that decides that, not anything we pass per-window — and
        joins the registry through target discovery once CDP attaches.
        """
        target_url = url or "about:blank"
        cold_start = not self.is_running
        error = self.ensure_running(initial_url=target_url if cold_start else "")
        if error:
            return error
        if cold_start:
            # Chrome's startup window IS this window — asking for another
            # would open a second, unwanted one.
            return ""
        if self._cdp.is_connected:
            return (
                ""
                if self._cdp.create_window(target_url, callback)
                else "cdp-send-failed"
            )
        return self._open_window_via_cli(target_url)

    def _open_window_via_cli(self, url: str) -> str:
        """Fallback open: `chrome --new-window <url>` against the live process.

        This is the Chrome singleton working FOR us — a second invocation with
        the same --user-data-dir does not start a browser, it asks the running
        one to open a window, which is exactly what we want. That window is
        drawn by the running process, so it lands in our compositor.

        ⚠ The argv and the ENVIRONMENT must both be the full ones. This path
        runs seconds after a cold spawn (session restore replays several saved
        URLs), so it can lose the race and become the PRIMARY process instead
        of handing off — and a primary process without our `WAYLAND_DISPLAY`
        would connect to the HOST compositor, putting a loose browser window on
        whatever workspace the user is looking at. That is the exact failure
        this whole mechanism exists to prevent, and it only shows up under a
        race, so it will not appear in casual testing.
        """
        executable = chrome_executable()
        if not executable:
            return "chrome-not-installed"
        argv = build_chrome_argv(
            executable, self._window_class, self._profile, self.cdp_port()
        )
        argv += ["--new-window", url]
        try:
            proc = subprocess.Popen(
                argv,
                env=self.chrome_env(),
                stdout=subprocess.DEVNULL,
                # PIPE for the same reason as the cold path, and with more
                # force here: the docstring above says this invocation can lose
                # the race and become the PRIMARY Chrome. Discarding stderr on
                # the one path most likely to fail in a way nobody expects is
                # exactly backwards.
                stderr=subprocess.PIPE,
            )
        except OSError:
            log.exception("chrome --new-window failed")
            return "chrome-spawn-failed"
        # Drains AND waits, so it replaces the bare reaper rather than adding
        # a second thread.
        self._start_stderr_drain(proc, "chrome-open-reap")
        return ""

    def close_window(self, target_id: str) -> None:
        """Close one of our windows by CDP target id."""
        if target_id:
            self._cdp.close_target(target_id)
