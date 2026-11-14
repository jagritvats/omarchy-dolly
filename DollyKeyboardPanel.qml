import QtQuick
import qs.Ui as Ui

// Keep the native panel (positioning, keyboard focus, styling and animations).
// New shells own lazy content themselves; 4.0.4 needs a Loader in contentItem.
Ui.KeyboardPanel {
  id: root
  property Component body: null
  readonly property bool nativeLazy: "content" in root && "loadedContent" in root
  property bool bodyRequested: false
  readonly property Item bodyItem: nativeLazy ? root.loadedContent : legacyLoader.item
  readonly property real bodyHeight: bodyItem ? bodyItem.implicitHeight : 0

  Component.onCompleted: {
    if (nativeLazy) root.content = Qt.binding(function() { return root.body })
  }
  onOpenChanged: if (open) bodyRequested = true

  Loader {
    id: legacyLoader
    anchors.fill: parent
    focus: true
    active: !root.nativeLazy && (root.open || root.bodyRequested)
    sourceComponent: root.body
    onLoaded: if (root.open) Qt.callLater(function() { if (root.open && item) item.forceActiveFocus() })
  }
}
