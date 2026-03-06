from PySide6 import QtWidgets, QtCore, QtGui
import os


class TopBanner(QtWidgets.QWidget):
    def __init__(self, title: str, image_path: str | None, height: int = 220, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setObjectName("topBanner")
        self._orig_pm = None
        self._img_path = image_path

        self._img = QtWidgets.QLabel(self)
        self._img.setScaledContents(True)
        # Ensure global QWidget background styles don't paint over the banner
        self._img.setStyleSheet("background: transparent;")
        if image_path:
            pm = self._try_load(image_path)
            if pm is None and os.path.isabs(image_path):
                # try without directories (cwd)
                pm = self._try_load(os.path.basename(image_path))
            if pm is not None:
                self._orig_pm = pm
                self._img.setPixmap(pm)
            # Also set as CSS background in case label fails
            try:
                url = QtCore.QUrl.fromLocalFile(image_path).toLocalFile().replace('\\\\','/')
                if url:
                    self.setStyleSheet(self.styleSheet() + f"#topBanner {{ background-image: url('{url}'); background-position: center; background-repeat: no-repeat; }}")
            except Exception:
                pass

        self._title = QtWidgets.QLabel(title, self)
        self._title.setObjectName("bannerTitle")
        self._title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        # Use a widget-level stylesheet to override app-wide QWidget background rule
        self.setStyleSheet(
            "#topBanner { background: transparent; }\n"
            "#bannerTitle { color: white; font-size: 36px; font-weight: 700; background: transparent; }\n"
            "#bannerDebug { background: transparent; }"
        )

        # Optional debug overlay (hidden by default)
        self._debug = QtWidgets.QLabel("", self)
        self._debug.setObjectName("bannerDebug")
        self._debug.setStyleSheet("#bannerDebug { color: rgba(255,255,255,0.7); font-size: 11px; padding: 6px; }")
        self._debug.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self._debug.hide()

        lay = QtWidgets.QStackedLayout(self)
        lay.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        lay.addWidget(self._img)
        lay.addWidget(self._title)
        lay.addWidget(self._debug)
        lay.setContentsMargins(0, 0, 0, 0)
        # Ensure the debug label is actually visible above the title
        self._title.raise_()
        try:
            self._debug.raise_()
        except Exception:
            pass

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._orig_pm is not None:
            self._img.setPixmap(self._orig_pm.scaled(self.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            QtCore.Qt.TransformationMode.SmoothTransformation))

    def _try_load(self, p: str):
        pm = QtGui.QPixmap(p)
        return None if pm.isNull() else pm

    def paintEvent(self, e):
        # Always paint the background ourselves to avoid QLabel stacking quirks.
        super().paintEvent(e)
        # Lazy-load pixmap if needed
        if self._orig_pm is None and self._img_path:
            pm = QtGui.QPixmap(self._img_path)
            if not pm.isNull():
                self._orig_pm = pm
        painter = QtGui.QPainter(self)
        # Subtle gradient base so the area is visible even without an image
        grad = QtGui.QLinearGradient(0,0,self.width(),0)
        grad.setColorAt(0, QtGui.QColor(32, 0, 0))
        grad.setColorAt(1, QtGui.QColor(64, 0, 0))
        painter.fillRect(self.rect(), QtGui.QBrush(grad))
        # Draw the image if available
        if self._orig_pm is not None:
            scaled = self._orig_pm.scaled(self.size(),
                QtCore.Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                QtCore.Qt.TransformationMode.SmoothTransformation)
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        # DEV overlay hatch to verify painting
        try:
            if os.environ.get("FRY_DEV") == "1":
                pen = QtGui.QPen(QtGui.QColor(255,255,255,30))
                painter.setPen(pen)
                step = 12
                for i in range(0, self.width()+self.height(), step):
                    painter.drawLine(i, 0, 0, i)
        except Exception:
            pass
        painter.end()

    def set_debug(self, text: str | None):
        if text:
            self._debug.setText(text)
            self._debug.show()
            # Keep debug overlay on top even if other widgets change stacking
            try:
                self._debug.raise_()
            except Exception:
                pass
        else:
            self._debug.hide()
