import sys, os, json, threading, paramiko
from pathlib import Path
from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

SESSIONS_FILE = Path.home() / ".mymoba_sessions.json"

XTERM_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
  <style>
    html, body { height: 100%; margin: 0; background: #141414; overflow: hidden; }
    #terminal { width: 100%; height: 100%; }
  </style>
</head>
<body>
  <div id="terminal"></div>
  <script>
    const term = new Terminal({
      cursorBlink: true, fontSize: 13,
      fontFamily: 'Consolas, monospace',
      theme: { background: '#141414', foreground: '#e0e0e0', cursor: '#00ff88' }
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('terminal'));
    fitAddon.fit();
    window.addEventListener('resize', () => {
      fitAddon.fit();
      if (window.bridge) bridge.resizePty(term.cols, term.rows);
    });
    new QWebChannel(qt.webChannelTransport, function(channel) {
      window.bridge = channel.objects.bridge;
      term.onData(data => bridge.sendData(data));
      window.writeTerm = data => term.write(data);
      bridge.resizePty(term.cols, term.rows);
    });
  </script>
</body>
</html>"""

class TerminalBridge(QtCore.QObject):
    keyPressed = QtCore.Signal(str)
    ptyResized = QtCore.Signal(int, int)

    @QtCore.Slot(str)
    def sendData(self, data): self.keyPressed.emit(data)

    @QtCore.Slot(int, int)
    def resizePty(self, cols, rows): self.ptyResized.emit(cols, rows)

class SessionTab(QtWidgets.QWidget):
    def __init__(self, host, port, user, password, key_path):
        super().__init__()
        self.host, self.port, self.user, self.password, self.key_path = host, port, user, password, key_path
        self.client, self.channel, self.sftp = None, None, None
        self.current_path = "."
        self.init_ui()
        threading.Thread(target=self.connect_ssh, daemon=True).start()

    def init_ui(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        # SFTP Pane
        sftp_pane = QtWidgets.QWidget()
        s_layout = QtWidgets.QVBoxLayout(sftp_pane)
        s_layout.setContentsMargins(4, 4, 4, 4)

        self.path_label = QtWidgets.QLabel("<b>SFTP: /</b>")
        s_layout.addWidget(self.path_label)

        btn_row = QtWidgets.QHBoxLayout()
        btn_up = QtWidgets.QPushButton("⬆ Upload")
        btn_dl = QtWidgets.QPushButton("⬇ Download")
        btn_up.clicked.connect(self.upload_file)
        btn_dl.clicked.connect(self.download_file)
        btn_row.addWidget(btn_up)
        btn_row.addWidget(btn_dl)
        s_layout.addLayout(btn_row)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size"])
        self.tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        s_layout.addWidget(self.tree)
        splitter.addWidget(sftp_pane)

        # Xterm WebEngine Pane
        self.web = QWebEngineView()
        self.bridge = TerminalBridge()
        self.web_channel = QWebChannel()
        self.web_channel.registerObject("bridge", self.bridge)
        self.web.page().setWebChannel(self.web_channel)
        self.web.setHtml(XTERM_HTML, QtCore.QUrl("https://cdn.jsdelivr.net"))

        self.bridge.keyPressed.connect(self.send_ssh)
        self.bridge.ptyResized.connect(self.resize_ssh)
        splitter.addWidget(self.web)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter)

    def connect_ssh(self):
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = {"hostname": self.host, "port": self.port, "username": self.user, "timeout": 10}
            if self.key_path and os.path.exists(self.key_path):
                kwargs["key_filename"] = self.key_path
            else:
                kwargs["password"] = self.password
            self.client.connect(**kwargs)
            self.channel = self.client.invoke_shell(term="xterm-256color", width=100, height=32)
            self.sftp = self.client.open_sftp()
            self.current_path = self.sftp.normalize(".")
            self.refresh_sftp()
            while self.channel and not self.channel.closed:
                if self.channel.recv_ready():
                    data = self.channel.recv(4096).decode("utf-8", errors="replace")
                    QtCore.QMetaObject.invokeMethod(self, "write_js", QtCore.Qt.ConnectionType.QueuedConnection, QtCore.Q_ARG(str, data))
        except Exception as e:
            QtCore.QMetaObject.invokeMethod(self, "write_js", QtCore.Qt.ConnectionType.QueuedConnection, QtCore.Q_ARG(str, f"\r\n[Connection Error: {e}]\r\n"))

    @QtCore.Slot(str)
    def write_js(self, text):
        self.web.page().runJavaScript(f"if(window.writeTerm) window.writeTerm({json.dumps(text)});")

    def send_ssh(self, data):
        if self.channel and not self.channel.closed:
            self.channel.send(data)

    def resize_ssh(self, cols, rows):
        if self.channel and not self.channel.closed:
            try: self.channel.resize_pty(width=cols, height=rows)
            except Exception: pass

    def refresh_sftp(self):
        if not self.sftp: return
        try:
            items = self.sftp.listdir_attr(self.current_path)
            QtCore.QMetaObject.invokeMethod(self, "render_sftp", QtCore.Qt.ConnectionType.QueuedConnection, QtCore.Q_ARG(object, items))
        except Exception: pass

    @QtCore.Slot(object)
    def render_sftp(self, items):
        self.path_label.setText(f"<b>SFTP: {self.current_path}</b>")
        self.tree.clear()
        up_item = QtWidgets.QTreeWidgetItem(self.tree, ["📁 ..", ""])
        up_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, ("..", True))
        for attr in sorted(items, key=lambda x: (not (x.st_mode & 0o040000), x.filename)):
            is_dir = bool(attr.st_mode & 0o040000)
            icon = "📁 " if is_dir else "📄 "
            size = "" if is_dir else f"{attr.st_size:,} B"
            entry = QtWidgets.QTreeWidgetItem(self.tree, [icon + attr.filename, size])
            entry.setData(0, QtCore.Qt.ItemDataRole.UserRole, (attr.filename, is_dir))

    def on_item_double_clicked(self, item):
        name, is_dir = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if is_dir:
            target = os.path.normpath(f"{self.current_path}/{name}").replace("\\", "/")
            self.current_path = self.sftp.normalize(target)
            self.refresh_sftp()

    def download_file(self):
        item = self.tree.currentItem()
        if not item or not self.sftp: return
        name, is_dir = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if is_dir: return
        save_path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save Remote File", name)
        if save_path:
            self.sftp.get(f"{self.current_path}/{name}", save_path)

    def upload_file(self):
        if not self.sftp: return
        local_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select File to Upload")
        if local_path:
            fname = os.path.basename(local_path)
            self.sftp.put(local_path, f"{self.current_path}/{fname}")
            self.refresh_sftp()


class MyMobaPro(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MyMobaTerminal Pro - Unlimited Edition")
        self.resize(1280, 780)
        self.init_ui()

    def init_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Connection & Session Bar
        bar = QtWidgets.QHBoxLayout()
        self.host = QtWidgets.QLineEdit(placeholderText="Host / IP")
        self.port = QtWidgets.QLineEdit("22")
        self.port.setFixedWidth(50)
        self.user = QtWidgets.QLineEdit(placeholderText="Username")
        self.pwd = QtWidgets.QLineEdit(placeholderText="Password")
        self.pwd.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key = QtWidgets.QLineEdit(placeholderText="Optional .pem / SSH Key Path")
        btn_key = QtWidgets.QPushButton("Browse Key")
        btn_key.clicked.connect(self.pick_key)
        btn_conn = QtWidgets.QPushButton("New Tab Connect")
        btn_conn.clicked.connect(self.open_tab)

        for w in [self.host, self.port, self.user, self.pwd, self.key, btn_key, btn_conn]:
            bar.addWidget(w)
        layout.addLayout(bar)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.tabs.removeTab)
        layout.addWidget(self.tabs)

    def pick_key(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select Private Key")
        if path: self.key.setText(path)

    def open_tab(self):
        h = self.host.text().strip()
        p = int(self.port.text().strip() or 22)
        u = self.user.text().strip()
        pw = self.pwd.text()
        k = self.key.text().strip()
        if not h or not u: return
        tab = SessionTab(h, p, u, pw, k)
        idx = self.tabs.addTab(tab, f"{u}@{h}")
        self.tabs.setCurrentIndex(idx)

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = MyMobaPro()
    win.show()
    sys.exit(app.exec())
