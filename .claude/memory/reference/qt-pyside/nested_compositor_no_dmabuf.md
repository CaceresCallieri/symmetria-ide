---
name: nested-compositor-no-dmabuf
description: Chrome in the nested compositor is on wl_shm because Qt 6.11 implements no dmabuf protocol at all — the EGL warning at startup names the wrong layer
metadata: 
  node_type: memory
  type: reference
  originSessionId: 1dc16f14-6524-437a-9b81-8d0fde68876c
  modified: 2026-08-04T20:39:38.693Z
---

The IDE's nested compositor gives Chrome **no GPU buffer path**. Every frame
goes through `wl_shm`: GPU render → readback to CPU → memcpy into shared
memory → the compositor uploads it back as a texture.

**The startup warning names the wrong layer**, and an earlier note in
CLAUDE.md repeated it as the cause:

```
WARNING qt.qml — Failed to initialize EGL display.
                 There is no EGL_WL_bind_wayland_display extension.
```

Read literally that says "install/enable the EGL extension", which points at
Mesa and at EGL configuration. There is nothing to fix there, for two
independent reasons — either one alone is sufficient:

1. **Qt's compositor does not implement `zwp_linux_dmabuf_v1` at all**, so no
   EGL fix could produce a dmabuf path.
2. `EGL_WL_bind_wayland_display` gates the LEGACY `wl_drm` path, which
   **Chrome never used**. Its Ozone/Wayland backend takes GPU buffers only
   over `zwp_linux_dmabuf_v1`.

## Measured — 2026-08-04

Configuration: Arch, Mesa, Hyprland; `qt6-wayland 6.11.1-1`; the dev IDE
running with a live nested compositor socket.

**The nested compositor's globals**, straight from the live socket — this is
the whole list, and it is the primary evidence:

```
$ WAYLAND_DISPLAY=symmetria-browser-<pid> wayland-info | grep interface:
  qt_hardware_integration  v1      wl_output          v2
  wl_compositor            v5      wl_seat            v4
  wl_data_device_manager   v1      wl_shm             v2
  wl_subcompositor         v1      wp_viewporter      v1
  xdg_wm_base              v1      xdg_wm_dialog_v1   v1
```

No `zwp_linux_dmabuf_v1`. `wl_shm` is the only buffer protocol offered.

**Not in Qt either**, which is what makes it structural rather than a
misconfiguration:

```
$ grep -rl "zwp_linux_dmabuf_v1" /usr/lib/libQt6Wayland*.so* \
      /usr/lib/qt6/plugins/wayland-graphics-integration-*/*.so
  (no matches)
```

`libqt-wayland-compositor-dmabuf-server-buffer.so` exists in that directory
and is **not** this: it implements Qt's own `qt_hardware_integration` server-
buffer extension, which only Qt clients speak.

**The host EGL, for completeness** — it has the modern import/export
extensions and not the legacy binding one, which is Mesa having dropped
`wayland-drm`, not a broken install:

```
$ eglinfo | grep -i dma_buf
  EGL_EXT_image_dma_buf_import, EGL_EXT_image_dma_buf_import_modifiers,
  EGL_MESA_image_dma_buf_export
```

## Two other documented facts, confirmed by the same run

Both were recorded from other evidence and are worth having a second,
independent reading of — the globals list gives one for free:

- **`wl_seat` version 4.** Qt pins the seat at v4, so `wl_pointer.frame` (a v5
  event) is never sent by Qt and our compositor sends it by hand. See
  [nested compositor pointer input](./nested_compositor_pointer_input.md).
- **`wl_output` version 2, no `wp_fractional_scale_v1`.** Which is why the
  output's scale is an integer and is rounded UP. See
  [nested compositor output mode](./nested_compositor_output_mode.md).

## What this does and does not explain

It is a **suspect** for the intermittent `FATAL: GPU process isn't usable.
Goodbye.` — a compositor with no dmabuf is a marginal configuration for
Chrome's GPU process, and the shm path allocates full-frame buffers on a
cadence — but the causal link is **not demonstrated**. Do not record it as the
cause without evidence from the stderr ring buffer added to `chrome_host.py`
on 2026-08-04, which captures the `The GPU process has crashed N time(s)` /
`exit_code=` preamble that the previous three crashes lost.

If it does turn out to be implicated, the principled response is
`--disable-gpu-compositing` — it stops Chrome attempting GPU presentation
while keeping GPU rasterization, which is exactly the split our compositor can
accept. That is a reason, not the superstition that picking a GPU flag off a
list would be.
