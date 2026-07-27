// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriacompositor.h for why this class exists and why it is shaped
// the way it is.

#include "symmetriacompositor.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QMimeData>
#include <QtCore/QStringList>
#include <QtGui/QClipboard>
#include <QtGui/QGuiApplication>

#include <cstdio>
#include <cstdlib>

namespace {

// Traced on stderr rather than through qWarning: the IDE's environment
// installs a Qt message handler that swallows qDebug/qWarning (it eats QML
// console.log too), and a clipboard bridge that silently does nothing is
// exactly the failure this needs to be able to explain. Off unless
// SYMMETRIA_COMPOSITOR_DEBUG is set.
void trace(const char *what, int formatCount)
{
    static const bool enabled = std::getenv("SYMMETRIA_COMPOSITOR_DEBUG") != nullptr;
    if (enabled)
        std::fprintf(stderr, "[symmetria-compositor] %s (%d formats)\n", what,
                     formatCount);
}

// Both directions hand us a QMimeData we do not own, with different lifetimes:
// the one from a nested client belongs to the compositor's data device, and
// QClipboard::mimeData() belongs to the clipboard and is invalidated on the
// next selection change. Copying is the only way to hold one safely.
// Identity of a selection's CONTENT, used to recognise our own data coming
// back around the bridge. Hashing every format (not just text) is what keeps
// it honest for images and rich HTML, where `text()` is empty or identical
// across genuinely different payloads.
QByteArray fingerprint(const QMimeData *data)
{
    if (data == nullptr)
        return {};
    QCryptographicHash hash(QCryptographicHash::Sha1);
    const QStringList formats = data->formats();
    for (const QString &format : formats) {
        hash.addData(format.toUtf8());
        hash.addData(data->data(format));
    }
    return hash.result();
}

QMimeData *cloneMimeData(const QMimeData *source)
{
    auto *copy = new QMimeData;
    if (source == nullptr)
        return copy;
    const QStringList formats = source->formats();
    for (const QString &format : formats)
        copy->setData(format, source->data(format));
    return copy;
}

} // namespace

SymmetriaCompositor::SymmetriaCompositor(QObject *parent)
    : QWaylandQuickCompositor(parent)
{
    // Without retention the compositor drops a nested client's selection the
    // moment that client releases it, and `retainedSelectionReceived` never
    // fires at all — so this one line is what makes the client→host half of
    // the bridge possible.
    setRetainedSelectionEnabled(true);

    if (QClipboard *clipboard = QGuiApplication::clipboard()) {
        connect(clipboard, &QClipboard::dataChanged, this,
                &SymmetriaCompositor::pushHostSelectionToClients);
    }
}

void SymmetriaCompositor::pushSelectionText(const QString &text)
{
    QMimeData payload;
    payload.setText(text);
    trace("pushSelectionText", int(payload.formats().size()));
    overrideSelection(&payload);
}

void SymmetriaCompositor::retainedSelectionReceived(QMimeData *mimeData)
{
    QClipboard *clipboard = QGuiApplication::clipboard();
    if (clipboard == nullptr)
        return;

    const QByteArray incoming = fingerprint(mimeData);
    if (incoming == m_lastBridged)
        return; // our own push, arriving back — see m_lastBridged
    m_lastBridged = incoming;

    trace("client took the selection",
          mimeData != nullptr ? int(mimeData->formats().size()) : -1);
    // QClipboard takes ownership of the QMimeData passed to setMimeData.
    clipboard->setMimeData(cloneMimeData(mimeData));
}

void SymmetriaCompositor::pushHostSelectionToClients()
{
    const QClipboard *clipboard = QGuiApplication::clipboard();
    if (clipboard == nullptr)
        return;

    const QMimeData *hostData = clipboard->mimeData();
    const QByteArray outgoing = fingerprint(hostData);
    if (outgoing == m_lastBridged)
        return; // already on both sides — see m_lastBridged
    m_lastBridged = outgoing;

    trace("host selection changed",
          hostData != nullptr ? int(hostData->formats().size()) : -1);

    // `overrideSelection` takes a `const QMimeData *` and transfers no
    // ownership: it reads the object synchronously to build a data source for
    // the nested clients, so the clipboard's own object is safe to hand over.
    overrideSelection(hostData);
}

// -- container plumbing ---------------------------------------------------

qsizetype SymmetriaCompositor::extensionCount(
    QQmlListProperty<QWaylandCompositorExtension> *list)
{
    return static_cast<SymmetriaCompositor *>(list->data)->extension_vector.size();
}

QWaylandCompositorExtension *SymmetriaCompositor::extensionAt(
    QQmlListProperty<QWaylandCompositorExtension> *list, qsizetype index)
{
    return static_cast<SymmetriaCompositor *>(list->data)->extension_vector.at(index);
}

void SymmetriaCompositor::appendExtension(
    QQmlListProperty<QWaylandCompositorExtension> *list,
    QWaylandCompositorExtension *extension)
{
    extension->setExtensionContainer(static_cast<SymmetriaCompositor *>(list->data));
}

void SymmetriaCompositor::clearExtensions(
    QQmlListProperty<QWaylandCompositorExtension> *list)
{
    static_cast<SymmetriaCompositor *>(list->data)->extension_vector.clear();
}
