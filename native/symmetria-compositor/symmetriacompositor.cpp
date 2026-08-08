// SPDX-License-Identifier: GPL-3.0-or-later
// See symmetriacompositor.h for why this class exists and why it is shaped
// the way it is.

#include "symmetriacompositor.h"

#include "symmetriahostkeys.h"
#include "symmetriatrace.h"

#include <QtCore/QCryptographicHash>
#include <QtCore/QMimeData>
#include <QtCore/QStringList>
#include <QtGui/QClipboard>
#include <QtGui/QGuiApplication>

namespace {

using symmetria::trace;

// Both directions hand us a QMimeData we do not own, with different lifetimes:
// the one from a nested client belongs to the compositor's data device, and
// QClipboard::mimeData() belongs to the clipboard and is invalidated on the
// next selection change. Copying is the only way to hold one safely.
// Only this much of each format is hashed. On Wayland `QClipboard::mimeData()`
// is LAZY: every `data(format)` call is a blocking pipe transfer with the
// owning application, on the GUI thread. Hashing a whole clipboard would mean
// the IDE stalls for as long as it takes some other app to hand over a large
// image — to compute a value we only compare. The size is folded in alongside
// the prefix so two payloads sharing a first chunk still differ.
constexpr qsizetype kFingerprintPrefixBytes = 64 * 1024;

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
        const QByteArray payload = data->data(format);
        hash.addData(QByteArray::number(payload.size()));
        hash.addData(payload.left(kFingerprintPrefixBytes));
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

    // Must happen HERE and not later: the base constructor has, moments ago,
    // installed Qt's own handler over every key event in the process, and the
    // slot it lives in is first-wins. Constructing this displaces it. Nothing
    // about it is browser-local — it is what keeps the terminals, the editor
    // and the IDE chords reading the keyboard the host compositor describes.
    m_hostKeys = new SymmetriaHostKeyHandler(this);

    if (QClipboard *clipboard = QGuiApplication::clipboard()) {
        connect(clipboard, &QClipboard::dataChanged, this,
                &SymmetriaCompositor::pushHostSelectionToClients);
    }
}

SymmetriaCompositor::~SymmetriaCompositor()
{
    // Before ~QWaylandCompositor runs, so the handler it hands the slot back to
    // is still alive.
    delete m_hostKeys;
    m_hostKeys = nullptr;
}

void SymmetriaCompositor::pushSelectionText(const QString &text)
{
    QMimeData payload;
    payload.setText(text);
    trace("pushSelectionText (%d formats)", int(payload.formats().size()));
    overrideSelection(&payload);
}

void SymmetriaCompositor::retainedSelectionReceived(QMimeData *mimeData)
{
    QClipboard *clipboard = QGuiApplication::clipboard();
    if (clipboard == nullptr)
        return;

    // A client releasing or clearing its selection hands us nothing. Cloning
    // that into the host clipboard would WIPE whatever the user had copied
    // elsewhere — a nested browser destroying the desktop's clipboard. Checked
    // before the fingerprint so an empty push cannot poison it either.
    if (mimeData == nullptr || mimeData->formats().isEmpty())
        return;

    const QByteArray incoming = fingerprint(mimeData);
    if (incoming == m_lastBridged)
        return; // our own push, arriving back — see m_lastBridged
    m_lastBridged = incoming;

    trace("client took the selection (%d formats)",
          int(mimeData->formats().size()));
    // QClipboard takes ownership of the QMimeData passed to setMimeData.
    clipboard->setMimeData(cloneMimeData(mimeData));
}

void SymmetriaCompositor::pushHostSelectionToClients()
{
    const QClipboard *clipboard = QGuiApplication::clipboard();
    if (clipboard == nullptr)
        return;

    const QMimeData *hostData = clipboard->mimeData();
    // `overrideSelection` DEREFERENCES its argument, and QClipboard::mimeData()
    // legitimately returns nullptr on an empty or unavailable clipboard — so
    // this is a segfault, not a no-op. Reachable in ordinary use: clear the
    // clipboard after a bridge has run and the fingerprint guard below does
    // not save us, since an empty fingerprint differs from the last one.
    if (hostData == nullptr || hostData->formats().isEmpty()) {
        m_lastBridged.clear();
        return;
    }

    const QByteArray outgoing = fingerprint(hostData);
    if (outgoing == m_lastBridged)
        return; // already on both sides — see m_lastBridged
    m_lastBridged = outgoing;

    trace("host selection changed (%d formats)",
          int(hostData->formats().size()));

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
