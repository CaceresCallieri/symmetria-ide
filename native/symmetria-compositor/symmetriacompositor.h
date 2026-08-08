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
// FAILURE MODE. This module is REQUIRED for the browser pane, not optional:
// `BrowserPane.qml` imports it unconditionally (QML has no conditional
// imports), so a missing package fails that file and the browser surface is
// gone. Main.qml's Loader keeps the failure from taking the whole IDE with it,
// which is the only degradation there is. An earlier draft of this comment
// promised a fallback to the stock `WaylandCompositor`; it was never
// implemented, and implementing it would mean maintaining two near-identical
// compositor declarations for one optional feature.

#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QList>
#include <QtQml/QQmlListProperty>
#include <QtQml/qqmlregistration.h>
#include <QtWaylandCompositor/QWaylandCompositorExtension>
#include <QtWaylandCompositor/QWaylandQuickCompositor>

QT_BEGIN_NAMESPACE
class QMimeData;
QT_END_NAMESPACE

class SymmetriaHostKeyHandler;

class SymmetriaCompositor : public QWaylandQuickCompositor
{
    Q_OBJECT
    Q_PROPERTY(QQmlListProperty<QWaylandCompositorExtension> extensions READ extensions)
    Q_PROPERTY(QQmlListProperty<QObject> data READ data DESIGNABLE false)
    Q_CLASSINFO("DefaultProperty", "data")
    QML_NAMED_ELEMENT(SymmetriaCompositor)

public:
    explicit SymmetriaCompositor(QObject *parent = nullptr);
    ~SymmetriaCompositor() override;

    // Pushes `text` into the nested clients' selection directly. A DIAGNOSTIC
    // with no production caller — it exists to test the host→client half in
    // isolation, because the ordinary path depends on the host clipboard,
    // which an UNFOCUSED Wayland app cannot read at all; without it there is
    // no way to tell a broken bridge apart from a clipboard the compositor was
    // never allowed to see. Invoke it from a scratch QML binding when
    // diagnosing, alongside SYMMETRIA_COMPOSITOR_DEBUG=1.
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
    // Fingerprint of the last selection that crossed the bridge, in either
    // direction. This is what stops a feedback loop, and it has to be
    // CONTENT-based rather than a "we are currently applying" flag: our own
    // `overrideSelection` comes back to us as `retainedSelectionReceived`, and
    // it arrives through an ASYNCHRONOUS pipe read — long after any synchronous
    // guard would have been cleared. Observed live before this existed: one
    // copy produced 49 full round trips of a 5-format payload.
    QByteArray m_lastBridged;

    // Undoes Qt's process-wide hijack of every key event in the application.
    // Held by raw pointer with only a forward declaration so this header does
    // not drag Qt's private QPA headers into everything that includes it; see
    // symmetriahostkeys.h for what it does and why it has to exist.
    SymmetriaHostKeyHandler *m_hostKeys = nullptr;

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
};
