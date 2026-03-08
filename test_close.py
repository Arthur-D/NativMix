from PyQt6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QPushButton, QVBoxLayout, QWidget, QSlider
from PyQt6.QtGui import QIcon
from PyQt6.QtCore import Qt
import sys

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

class MyWin(QMainWindow):
    def __init__(self):
        super().__init__()
        w = QWidget()
        l = QVBoxLayout(w)
        btn = QPushButton("Toggle")
        btn.setCheckable(True)
        btn.toggled.connect(self.on_toggled)
        self.slider = QSlider(Qt.Orientation.Vertical)
        self.slider.setValue(50)
        l.addWidget(btn)
        l.addWidget(self.slider)
        self.setCentralWidget(w)
        self.stay_open = False

    def on_toggled(self, checked):
        print("Toggled:", checked)
        self.stay_open = checked

    def closeEvent(self, e):
        print("closeEvent")
        if self.stay_open:
            print("Ignoring close")
            e.ignore()
        else:
            print("Hiding instead of closing")
            e.ignore()
            self.hide()

w = MyWin()
t = QSystemTrayIcon(QIcon.fromTheme('audio-volume-high'))
t.show()
w.show()
sys.exit(app.exec())
