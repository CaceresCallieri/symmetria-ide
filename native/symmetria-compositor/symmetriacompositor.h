// SPDX-License-Identifier: GPL-3.0-or-later
//
// `SymmetriaCompositor` — a QWaylandCompositor that shares its clipboard with
// the host session.
//
// WHY THIS EXISTS AT ALL. The IDE hosts real Google Chrome as a nested Wayland
// client so the agentic browser renders inside the IDE window. A nested
// compositor gets its own `wl_data_device`, and Qt does not bridge it: measured
// live, a selection taken inside the nested session is invisible to both the
// host `QClipboard` and Hyprland, and a host selection reads as `''` inside.
// For a browser whose entire purpose is feeding URLs and text into the rest of
// the workflow — the terminal, an agent pane — that is a blocker, not a nit.
//
// WHY C++ AND NOT QML. `retainedSelection` is a Q_PROPERTY, so QML can turn
// retention ON, but that alone bridges nothing: the two calls that actually
// move data are a PROTECTED VIRTUAL (`retainedSelectionReceived`, not a signal,
// so QML cannot receive it) and a plain method taking `const QMimeData *`
// (not Q_INVOKABLE, and QMimeData is not a QML type). PySide6 ships no
// QtWaylandCompositor bindings either — confirmed against 6.11.1's module list.
//
// WHY IT DERIVES FROM QWaylandQuickCompositor AND RE-DECLARES `data`.
// QML's `WaylandCompositor` is NOT the C++ `QWaylandCompositor` — that one is
// exported as the uncreatable `WaylandCompositorBase`. The type QML actually
// instantiates is a third class that adds the `data` default property and the
// `extensions` list, and it lives in a PRIVATE header. Deriving straight from
// `QWaylandCompositor` compiles and registers fine, then fails at RUNTIME with
// "Cannot assign to non-existent default property" the moment a `WaylandOutput`
// is nested inside it (observed, not theorised).
//
// Qt exposes a public macro for generating that container —
// `Q_COMPOSITOR_DECLARE_QUICK_EXTENSION_CONTAINER_CLASS` — but it has ROTTED:
// it still declares the list count/at callbacks taking `int`, while Qt 6.11's
// QQmlListProperty requires `qsizetype`, so it no longer compiles (the private
// header's own copy was updated; the public macro was not). Hence the container
// members below are written out by hand with the correct signatures. They are
// deliberately a faithful copy of Qt's — if a future Qt fixes the macro, this
// can collapse back onto it.
//
// DEGRADATION CONTRACT. This plugin is OPTIONAL. The browser pane falls back to
// the stock `WaylandCompositor` when `Symmetria.Compositor` fails to import, so
// a machine without the package gets a browser with an isolated clipboard —
// never no browser at all. Do not add anything here that the pane depends on
// for rendering, or that contract breaks.

#pragma once

#include <QtCore/QList>
#include <QtQml/QQmlListProperty>
#include <QtQml/qqmlregistration.h>
#include <QtWaylandCompositor/QWaylandCompositorExtension>
#include <QtWaylandCompositor/QWaylandQuickCompositor>

QT_BEGIN_NAMESPACE
class QMimeData;
QT_END_NAMESPACE

class SymmetriaCompositor : public QWaylandQuickCompositor
{
    Q_OBJECT
    Q_PROPERTY(QQmlListProperty<QWaylandCompositorExtension> extensions READ extensions)
    Q_PROPERTY(QQmlListProperty<QObject> data READ data DESIGNABLE false)
    Q_CLASSINFO("DefaultProperty", "data")
    QML_NAMED_ELEMENT(SymmetriaCompositor)

public:
    explicit SymmetriaCompositor(QObject *parent = nullptr);

    // Pushes `text` into the nested clients' selection directly. Exists to
    // test the host→client half in isolation: the ordinary path depends on the
    // host clipboard, which an UNFOCUSED Wayland app cannot read at all, so
    // without this there is no way to tell a broken bridge apart from a
    // clipboard the compositor was never allowed to see.
    Q_INVOKABLE void pushSelectionText(const QString &text);

    // -- container plumbing (see the header comment) ----------------------

    QQmlListProperty<QObject> data()
    {
        return QQmlListProperty<QObject>(this, &m_objects);
    }

    QQmlListProperty<QWaylandCompositorExtension> extensions()
    {
        return QQmlListProperty<QWaylandCompositorExtension>(
            this, this, &appendExtension, &extensionCount, &extensionAt,
            &clearExtensions);
    }

protected:
    // Fires whenever a nested client takes the selection. Retention (enabled in
    // the constructor) is what makes it fire at all.
    void retainedSelectionReceived(QMimeData *mimeData) override;

private Q_SLOTS:
    void pushHostSelectionToClients();

private:
    static qsizetype extensionCount(
        QQmlListProperty<QWaylandCompositorExtension> *list);
    static QWaylandCompositorExtension *extensionAt(
        QQmlListProperty<QWaylandCompositorExtension> *list, qsizetype index);
    static void appendExtension(
        QQmlListProperty<QWaylandCompositorExtension> *list,
        QWaylandCompositorExtension *extension);
    static void clearExtensions(
        QQmlListProperty<QWaylandCompositorExtension> *list);

    QList<QObject *> m_objects;

    // Set while we are the ones writing to the host clipboard, so the
    // dataChanged that write emits does not bounce straight back into the
    // clients that produced the selection.
    bool m_applyingClientSelection = false;
};
