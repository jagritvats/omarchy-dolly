import QtQuick
import Quickshell.Io
import qs.Commons as Commons

// Use the shell's live language/catalog support when available. Released 4.0.4
// predates it, so read the plugin's English catalog rather than dropping labels.
Item {
  id: root
  visible: false
  property var directory: ""
  property var nativeStrings: null
  property var english: ({})
  readonly property var shellI18n: typeof Commons.I18n !== "undefined" ? Commons.I18n : null
  readonly property string formatLocale: shellI18n ? shellI18n.formatLocale : Qt.locale().name

  Component.onCompleted: {
    if (typeof Commons.Translations !== "undefined") {
      nativeStrings = Qt.createQmlObject('import qs.Commons as Commons; Commons.Translations {}', root, "DollyNativeTranslations")
      nativeStrings.directory = Qt.binding(function() { return root.directory })
    }
  }

  FileView {
    path: root.nativeStrings ? "" : String(root.directory).replace(/\/+$/, "") + "/en.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try { root.english = JSON.parse(text()) } catch (e) { root.english = ({}) }
    }
  }

  function t(key, fallback) {
    if (nativeStrings) return nativeStrings.t(key, fallback)
    var value = english[key]
    if (value && typeof value === "object") value = value.other
    return typeof value === "string" ? value : (fallback !== undefined ? fallback : key)
  }
}
